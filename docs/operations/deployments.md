# Production deployment runbook

The canonical repository is
`https://github.com/xmansx2030-lgtm/school_display`. Its `production` environment owns
the SSH credentials used by `.github/workflows/deploy.yml`.

Deployments are manual and tarball-based. There is no CD pipeline: CI on `main`
runs tests and builds the image, but nothing ships on its own.

The production application directory is intentionally not a Git checkout.
GitHub Actions archives the exact approved commit and uploads it over SSH, so
repository migrations must update the workflow environment credentials rather
than adding a Git remote under `/opt/school-display/app`.

`deploy/deploy.sh` is the reviewed copy of the script; the server executes
`~/deploy.sh`. Keep the two in sync — see [Keeping the script in sync](#keeping-the-script-in-sync).

## Deploy a release

Only ever deploy a commit that is merged into `main` and green in CI.
The deployment workflow checks the latest push CI result for the exact requested
commit and refuses a failed, pending or missing result.

```bash
# 1. build the release tarball from the merged commit
FULL=$(git rev-parse origin/main)
SHA=$(git rev-parse --short=7 origin/main)
git archive --format=tar.gz -o "school-display-$SHA.tar.gz" "$FULL"

# 2. upload it, then confirm the checksums match
scp "school-display-$SHA.tar.gz" deploy@<host>:/home/deploy/releases/
sha256sum "school-display-$SHA.tar.gz"
ssh deploy@<host> "sha256sum /home/deploy/releases/school-display-$SHA.tar.gz"

# 3. run it detached — an SSH drop must not leave a release half-applied
ssh deploy@<host> "cp ~/deploy.sh ~/deploy-$SHA.sh && chmod +x ~/deploy-$SHA.sh && \
  nohup ~/deploy-$SHA.sh $SHA $FULL > ~/deploy-$SHA.log 2>&1 &"

# 4. watch until "DEPLOY COMPLETE"
ssh deploy@<host> "tail -f ~/deploy-$SHA.log"
```

The script refuses to start if the tarball is missing, so a failed upload costs
nothing.

## What the script does

| step | action |
|------|--------|
| 1 | Unpack the release into a staging directory |
| 2 | PostgreSQL dump + a tarball snapshot of the current tree, then `pg_restore --list` to prove the dump is readable |
| 3 | `rsync --delete` into `/opt/school-display/app`, preserving `.env.production`, its backup copies, and `.release-commit` |
| 4 | Build **every** service image |
| 5 | `migrate --noinput` |
| 6 | `up -d`, recreate the internal Caddy so its bind-mounted config is current, then stamp `.release-commit` with the full SHA |

Service recreation waits up to 180 seconds for configured health checks. The
release stamp is written only after services are healthy and `check --deploy`
has completed. `/health/` returns uncached JSON with database, cache read/write,
and independent Channels Redis readiness; a failed dependency returns HTTP 503
without disclosing connection strings. The Docker web check calls this endpoint
with the internal host and HTTPS proxy headers, so a redirect cannot pass it.

Telegram health requires a recent successful processing scan, no terminal
delivery failures and no pending/processing alerts overdue by five minutes.
When a failure is terminal, inspect the sanitized error in TelegramAlert admin
and use the selected failed-alert retry action after correcting the cause;
successful alerts are never requeued by that action. Intentionally disabled
Telegram alerts are reported explicitly as disabled.

The service keeps serving during steps 1-4. Only step 6 recreates containers.

## Build every service, not just `web`

The workers have **no source volume** — they run the code baked into their own
images. `compose.production.yaml` gives each service its own image built from the
same context, so `docker compose build web` leaves `snapshot-worker`,
`wake-scheduler`, `screen-monitor`, `telegram-alert-worker`,
`email-notification-worker` and `moyasar-reconciliation-worker` on the previous
release.

That is a half-deployed state with no obvious symptom: the site serves the new
code while the workers quietly run the old one. It bit us on `277b5b5`, where
the database was migrated but `screen-monitor` still held the previous
`core/screen_monitoring.py`. Step 4 now builds all services.

To check after any deploy:

```bash
# every school-display image should be as recent as the deploy
docker images --format '{{.Repository}} {{.CreatedSince}}' | grep school-display | sort
```

## Verify a deployment

```bash
# the stamp must equal the commit you shipped
ssh deploy@<host> "sudo cat /opt/school-display/app/.release-commit"

# containers up and healthy
ssh deploy@<host> "docker ps --format '{{.Names}}|{{.Status}}'"

# nothing left to migrate, anywhere
ssh deploy@<host> "cd /opt/school-display/app && sudo docker compose -f compose.production.yaml \
  exec -T web python manage.py showmigrations | grep -c '\[ \]'"     # expect 0

# no errors since the release
ssh deploy@<host> "cd /opt/school-display/app && sudo docker compose -f compose.production.yaml \
  logs --since 10m | grep -icE 'traceback|exception|critical'"        # expect 0
```

Then from outside, against the live domain:

```bash
for p in /health/ /dashboard/login/ /subscriptions-page/ /tv/; do
  curl -s -o /dev/null -w "$p %{http_code}\n" "https://school-display.com$p"
done
```

Static assets sit behind Cloudflare with `immutable, max-age=31536000`. Probing a
new asset path *before* it exists caches the 404 for a few minutes — check with a
cache-buster (`?cb=$(date +%s)`) if a fresh file appears to be missing.

## Migrations

Read the migration before deploying it. Additive changes — `AddField` with a
default or `null=True`, `AddIndex` on a small table — apply to a populated
database without rewriting rows. Anything that drops, renames, or runs
`RunPython` over a large table needs a plan of its own and should not ride along
with a routine release.

Step 1 dumps the database before step 5 touches it, and the dump is verified
readable, so a bad migration is recoverable — see
[backups.md](backups.md) for the restore path.

## Keeping the script in sync

The server runs `~/deploy.sh`. `deploy/deploy.sh` in this repository is the
reviewed source; a release only puts it at
`/opt/school-display/app/deploy/deploy.sh`. After shipping a change to it:

```bash
ssh deploy@<host> "cp ~/deploy.sh ~/deploy.sh.bak-\$(date -u +%Y%m%dT%H%M%SZ) && \
  cp /opt/school-display/app/deploy/deploy.sh ~/deploy.sh && chmod +x ~/deploy.sh && bash -n ~/deploy.sh"

# confirm they match
ssh deploy@<host> "sha256sum ~/deploy.sh /opt/school-display/app/deploy/deploy.sh"
```

Drift here is silent and was how the `build web` bug survived: the script lived
only on the server, so no review ever saw it.

## Rollback

Every deploy leaves a tree snapshot and a database dump under
`/home/deploy/releases/` and `/opt/school-display/backups/`. To go back to a
known commit, deploy its tarball again — the procedure is identical. Restore the
database only if the release included a migration you need to undo; see
[backups.md](backups.md).
