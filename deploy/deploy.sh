#!/usr/bin/env bash
#
# Production deploy for school-display.
#
# Runs on the server as the `deploy` user, detached from the SSH session — an
# SSH drop must never leave a release half-applied:
#
#   1. build the release tarball from a merged commit, on your machine
#        git archive --format=tar.gz -o school-display-<sha>.tar.gz <full-sha>
#   2. upload it
#        scp school-display-<sha>.tar.gz deploy@<host>:/home/deploy/releases/
#   3. run this detached, passing the short and full commit
#        cp ~/deploy.sh ~/deploy-<sha>.sh && chmod +x ~/deploy-<sha>.sh
#        nohup ~/deploy-<sha>.sh <sha> <full-sha> > ~/deploy-<sha>.log 2>&1 &
#   4. watch ~/deploy-<sha>.log until "DEPLOY COMPLETE"
#
# The server executes ~/deploy.sh, not this file. This is the reviewed copy:
# after changing it, ship the release and refresh the server's copy from the
# swapped tree, otherwise the two drift silently —
#
#   cp /opt/school-display/app/deploy/deploy.sh ~/deploy.sh && chmod +x ~/deploy.sh
#
# See docs/operations/deployments.md.
#
set -euo pipefail
umask 077
exec 2>&1

if [ "$#" -ne 2 ]; then
  echo "usage: $(basename "$0") <short-sha> <full-sha>" >&2
  exit 64
fi

SHA="$1"; FULL="$2"
if [[ ! "$FULL" =~ ^[0-9a-f]{40}$ || "$SHA" != "${FULL:0:7}" ]]; then
  echo "FATAL: expected a full commit and its seven-character prefix" >&2
  exit 64
fi
ROOT=/opt/school-display
APP=$ROOT/app
BACKUPS=$ROOT/backups
STAGE=/home/deploy/releases/.stage-$SHA
CF=$APP/compose.production.yaml
RELEASE=/home/deploy/releases/school-display-$SHA.tar.gz

# Fail before the backups rather than halfway through: a missing tarball means
# the upload never landed, and there is nothing to deploy.
if [ ! -s "$RELEASE" ]; then
  echo "FATAL: $RELEASE is missing or empty; upload the release first" >&2
  exit 66
fi

echo "=== [1/6] staging ==="
rm -rf "$STAGE"; mkdir -p "$STAGE"
tar -xzf "$RELEASE" -C "$STAGE"
echo "staged $(find "$STAGE" -type f | wc -l) files"

echo "=== [2/6] backups $(date -u +%H:%M:%SZ) ==="
cd "$APP"
# Use the backup helper from the release being deployed. This lets a corrected
# helper safely deploy over an older tree whose helper still has stale paths.
sudo "$STAGE/deploy/backup-postgres.sh" 2>&1 | tail -1
TS=$(date -u +%Y%m%dT%H%M%SZ)
sudo tar -czf /home/deploy/releases/pre-$SHA-$TS.tar.gz \
     --exclude=./.env.production --exclude='./.env.production.bak.*' \
     -C "$APP" . 2>/dev/null
echo "tree snapshot: pre-$SHA-$TS.tar.gz"
D=$(sudo bash -c "ls -1t $BACKUPS/*.dump | head -1")
# Stream the protected dump directly: never create a world-readable copy on
# the host or leave a second copy in the database container after a failure.
RESTORABLE_OBJECTS=$(sudo cat "$D" | sudo docker compose -f "$CF" exec -T postgres \
  pg_restore --list | awk '/^[0-9]+;/ {n++} END {print n+0}')
test "$RESTORABLE_OBJECTS" -gt 0
echo "dump restorable objects: $RESTORABLE_OBJECTS"

echo "=== [3/6] swapping tree ==="
sudo rsync -a --delete \
  --exclude=".env.production" --exclude=".env.production.bak.*" \
  --exclude=".release-commit" \
  "$STAGE"/ "$APP"/
sudo chown -R deploy:deploy "$APP" 2>/dev/null || true
sudo chown root:root "$APP/.env.production" "$APP/.release-commit"
sudo chmod 600 "$APP/.env.production"
sudo find "$APP" -maxdepth 1 -type f -name '.env.production.bak.*' \
  -exec chown root:root {} + -exec chmod 600 {} +
sudo chown -R root:root "$BACKUPS"
rm -rf "$STAGE"
sudo test -s "$APP/.env.production" || { echo "FATAL: env vanished"; exit 1; }
echo "env ok: $(sudo stat -c%s "$APP/.env.production") bytes"

echo "=== [4/6] building images (service untouched) ==="
# Build EVERY service, not just web. The workers run the code baked into their
# own images with no source volume, so building web alone left snapshot-worker,
# wake-scheduler, screen-monitor and the rest on the previous release — a
# half-deployed state that only showed up as stale worker behaviour.
sudo env SOURCE_REVISION="$FULL" docker compose -f "$CF" build 2>&1 | grep -iE "Building|Built|ERROR|FINISHED" | tail -12

echo "=== [5/6] migrate ==="
sudo docker compose -f "$CF" run --rm -T web python manage.py migrate --noinput 2>&1 | grep -E "Applying|No migrations" | tail -5

echo "=== [6/6] recreating services ==="
sudo docker compose -f "$CF" up -d --wait --wait-timeout 180 2>&1 | tail -8
# rsync replaces the bind-mounted Caddyfile inode. A running Caddy container
# keeps the old inode even after a reload, so recreate only this lightweight
# internal static proxy to mount and activate the reviewed release config.
sudo docker compose -f "$CF" up -d --force-recreate --wait --wait-timeout 180 caddy 2>&1 | tail -4
sudo docker compose -f "$CF" exec -T web python manage.py check --deploy
sudo bash -c "printf %s $FULL > $APP/.release-commit"
echo "stamped: $(sudo cat "$APP/.release-commit")"
echo "=== DEPLOY COMPLETE $(date -u +%H:%M:%SZ) ==="
