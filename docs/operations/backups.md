# Production backup and restore runbook

## Backup layers

1. `deploy/backup-postgres.sh` creates a PostgreSQL custom-format dump every day.
2. Hetzner server backups protect the complete VM.
3. The optional Restic job uploads the database dumps to an independent storage provider.

Restic encrypts repository contents before upload. The repository password and storage credentials must only exist in `.env.production` or a dedicated secret file on the server.

Release validation streams the protected dump into `pg_restore --list` over stdin.
It must never copy a dump to a predictable or world-readable path under `/tmp`.
Deployment archives and logs are created with `umask 077`.

## Configure off-site storage

Set at minimum:

```dotenv
RESTIC_REPOSITORY=s3:https://s3.example.com/school-display-backups
RESTIC_PASSWORD=use-a-long-random-backup-password
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

Restic also supports SFTP, Backblaze B2, Azure and local repositories. Do not use the production server itself as the off-site destination.

Run and verify manually before installing the cron entry:

```bash
cd /opt/school-display
docker compose -f compose.production.yaml --profile operations run --rm offsite-backup
```

Then install `deploy/school-display-offsite-backup.cron.example` as a cron definition.

Install `deploy/school-display.logrotate` under `/etc/logrotate.d/school-display`
so cron output cannot grow without a limit. Incremental Restic backup runs daily;
the more expensive prune and repository check run only on
`RESTIC_MAINTENANCE_WEEKDAY` (Sunday by default).

## Monthly restore drill

1. Restore the latest snapshot into an empty temporary directory with Restic.
2. Validate the dump using `pg_restore --list`.
3. Restore it into a temporary PostgreSQL database, never over production.
4. Run `python manage.py check` and verify school, screen and subscription row counts.
5. Record the date, snapshot ID, duration and result of the drill.

Never perform a production restore without confirming the exact target database and taking a new pre-restore dump.
