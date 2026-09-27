---
phase: 5
title: "Backups & restore"
status: cancelled
priority: P1
effort: "7h"
dependencies: [4]
---

> **Dropped by the owner on 2026-09-27:** "don't need to make a backup for Atelier". Nothing in this phase was implemented, and no R2 bucket, token or restic password was created. The data volume holds the only copy of Atelier's images and database. The empty `/mnt/atelier-data/backup` directory made during the volume setup is unused.

# Phase 5: Backups & restore

## Context Links

<!-- Updated: Red Team 2026-09-25 - F2 backup pipeline -->

- Contract: the backup constraint (04:30 UTC, restic to R2, 7/4/6 retention, weekly restore test, status in the UI, password kept off the server) and criterion 11. See the [brainstorm report](../reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md).
- Brief §7 (backup job, weekly verification, credentials, runbook): [architecture brief](./reports/architecture-brief.md)
- restic to R2, with no bucket locks, `--hostname`, the image's `NICE`/`IONICE_*` variables, `forget --prune`, `check --read-data-subset`, lock exit code 11 and `unlock`; `sqlite3.Connection.backup()`: [research-01 §5–6](./research/researcher-01-cloudflare-access-tunnel-restic-r2.md)
- Red-team evidence: [failure modes](./reports/red-team-failure-mode-analyst.md) (Findings 7, 8), [assumptions](./reports/red-team-assumption-destroyer.md) (Findings 4, 6), [security](./reports/red-team-security-adversary.md) (Findings 1, 3, 8), [scope](./reports/red-team-scope-complexity-critic.md) (Finding 6).
- restic `cmd/restic/cmd_ls.go` (fetched by the red team): with a directory filter, `ls` lists only direct children unless `--recursive` is given.
- Host facts (no `restic` or `sqlite3` binaries; Folio's jobs at 03:00 and 03:30; Sunday scrubs around 03:10): [scout-02](./reports/scout-02-folio-prod-1-live-state.md)
- Folio patterns, re-verified:
  - `folio/scripts/backup/pg-dump.sh:8-11` stratifies its exit codes.
  - `pg-dump.sh:19` logs through `/usr/bin/logger -t <tag>`.
  - `verify-latest-dump.sh` restores weekly into a sidecar.
- `DB_FILENAME` comes from `atelier/db.py` (phase 2). The `deploy.sh stop | start` subcommands come from phase 4.

## Overview

When this phase is done, a systemd timer backs Atelier up every night at 04:30 UTC:
- a consistent SQLite snapshot plus the image tree;
- sent encrypted and deduplicated to a Cloudflare R2 bucket with restic, using the official Docker image pinned by digest and throttled so it never starves Folio;
- retention of 7 daily, 4 weekly and 6 monthly snapshots.

A Sunday 05:00 UTC job checks the repository, restores the database into a temporary directory and verifies it against the snapshot's images and the live database. The UI header shows the last backup and verify status and turns red on failure. A two-path restore runbook is documented, and its DB-only path is rehearsed once. A purge-from-backups procedure exists for deleted images. Priority P1, for data safety.

## Key Insights

<!-- Updated: Red Team 2026-09-25 - F2 backup pipeline -->
<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->

- **R2 bucket locks stay off.** They block the deletes and rewrites that `forget --prune` needs (research-01 §5), and that decision is final.
- **The snapshot is taken inside the container.**
  - The host has no `sqlite3`, so the snapshot runs as `docker exec atelier python -m atelier.backup_db snapshot …` through `sqlite3.Connection.backup()`.
  - That call is WAL-safe and gives a bit-exact copy as of its start, with no extra package (research-01 §6).
- **Only two paths are backed up:** `/data/images` (PNGs and WebP thumbnails) and `/data/backup/atelier.db`. The live database files are excluded simply because they are never listed.
- **The verify must list recursively, and it must understand deletions.**
  - Images live at `images/YYYY/MM/job-<id>.png`, and `restic ls` with a directory filter lists only direct children. So the listing is `restic ls --json --recursive latest /data/images`.
  - A listing with no PNG node at all, while the snapshot DB has images, is an error ("listing contains no PNG nodes"), never "N missing".
  - Images created between the DB snapshot and restic's file walk are extra files and are only reported.
  - A referenced PNG missing from the snapshot is checked against the **live** DB. If its row is gone, it was deleted after the snapshot, which is a warning. Only a PNG whose row still exists is a real failure.
  - The verify therefore runs through `docker exec` in the live container, which sees the restored snapshot on the volume and the live DB.
- **The backup CLIs never load `Settings`.** They read only `ATELIER_DATA_DIR` and import `DB_FILENAME` from `atelier/db.py`, so they run with an empty environment and never need a production secret.
- **The fixtures come from restic itself.** They are captured from a real run of the pinned image against a throwaway local repository. Hand-written fixtures hid the recursion bug.
- **restic runs throttled.** The unit's `Nice=` and I/O class don't reach a process started by `docker run`. `restic_run` therefore passes `NICE=10`, `IONICE_CLASS=2`, `IONICE_PRIORITY=7` and `GOMAXPROCS=1` into the container, and limits it with `--cpus 1 --memory 1g`.
- **The restic image is pinned by digest in the script itself.** That container receives the restic password and the R2 keys every night.
- **Backup and verify never overlap.** Both take `flock /run/lock/atelier-backup.lock`. A long first backup can then never collide with the Sunday verify, since `prune` needs an exclusive restic lock.
  - Each run starts with `restic unlock`, which removes only stale locks left by dead processes, to recover from a crashed night.
- **Nothing lands on the root disk.** The restic cache and the temporary restore directories live on the volume (`/mnt/atelier-data/.restic-cache`, `.verify.*` and `.restore.*`), not on Folio's `/`.
- **Status is written by the app's own module.** `docker exec atelier python -m atelier.backup_status record …` writes the file, so the format that the header reads has a single owner. If the container is down, the record fails, the job logs it to journald, and the badge shows "stale" after 36 h.
- **The password is unrecoverable if lost.** The owner stores it in the password manager **before** `restic init`.
- **Host root can read and destroy these backups.** `backup.env` holds the restic password and an R2 read-write key on the shared host. Root on `folio-prod-1` can therefore decrypt every snapshot and, with bucket locks off, delete the history. Folio's append-only backup identity is not replicated here. Phase 4's key audit and LearnFlow restriction reduce who holds root.

## Requirements

<!-- Updated: Red Team 2026-09-25 - F2 backup pipeline -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->

Functional:
- **`atelier/backup_db.py`** is a CLI with three subcommands. It imports `DB_FILENAME` from `atelier/db.py`, reads only `ATELIER_DATA_DIR` (default `/data`), and never loads `Settings`.
  - `snapshot DEST` makes an online backup of `<data dir>/<DB_FILENAME>` to `DEST.tmp`, runs `PRAGMA integrity_check`, renames to `DEST` and prints the image count.
  - `verify SNAPSHOT_DB --pngs-from-stdin --live-db LIVE_DB`:
    - opens both databases as `file:…?mode=ro&immutable=1` and runs the integrity check on the snapshot DB;
    - reads `restic ls --json --recursive` lines from stdin and keeps nodes with `type == "file"` and a `.png` name. restic lists `/data/<rel>` and the DB stores `<rel>` (for example `images/2026/09/job-12.png`), so it strips the `/data/` prefix before comparing;
    - errors with "listing contains no PNG nodes" when the listing has none while the snapshot DB has images;
    - reports DB images, snapshot PNGs, extra PNGs, **deleted-after-snapshot** paths (missing from the snapshot and absent from the live DB) as warnings, and **missing** paths (missing from the snapshot but still in the live DB) as failures;
    - prints one JSON summary line and exits 0 only when integrity is `ok` and nothing is missing.
  - `verify-restore --db DB --root DATA_ROOT [--report-missing]` runs the full restore check: integrity, then every image row's PNG and thumbnail exist under `DATA_ROOT` and their sha256 matches `images.sha256`. With `--report-missing` it lists the missing and mismatched paths, for the DB-only restore path.
- **`atelier/backup_status.py`** also reads only `ATELIER_DATA_DIR`:
  - `record --event backup|verify --ok [--images N] [--warnings N]` and `record --event backup|verify --error MSG` write `/data/backup-status.json` atomically.
  - `read_status(data_dir)` returns a state:
    - `failed` if the latest backup or verify run errored;
    - `stale` if the last backup success is older than 36 h (or there is none), or the last verify success is older than 8 days;
    - `ok` otherwise, with any verify warnings shown as a count.
- **Scripts:** `deploy/backup/atelier-backup-common.sh` (sourced), `atelier-backup.sh` and `atelier-backup-verify.sh`.
  - Exit codes are 0 ok, 1 config missing, 2 volume, container or lock unavailable, 3 snapshot or verification failed, and 4 restic failure.
  - Everything logs through `logger -t`.
- **systemd:** `atelier-backup.service` and `.timer` (`*-*-* 04:30:00 UTC`, `Persistent=true`), plus `atelier-backup-verify.service` and `.timer` (`Sun *-*-* 05:00:00 UTC`, `Persistent=true`).
- **UI:** the header-status partial gets a backup badge showing state, age, verify warnings, and the error text on failure.
- **Runbook:** a "Backups and restore" section in `docs/deployment-guide.md` covering:
  - scope, schedule, credential locations, and manual restic commands;
  - weekly verify, and what a "deleted after snapshot" warning means;
  - restore path (a) DB-only and path (b) full, as in Architecture;
  - the purge-from-backups procedure;
  - the dates of the rehearsals.

Non-functional:
- restic runs at nice 10, best-effort I/O priority 7, one Go thread, one CPU and 1 GB of memory, so it never starves Folio. The unit's own `Nice=10` and I/O class cover the script and the Docker client.
- R2 cost stays inside or near the free tier (research-01 §7).
- Secrets live only in `/opt/atelier/backup.env` (0600 root) and the owner's password manager.
- Every compose action in the runbook goes through `/opt/atelier/deploy.sh stop | start`, so `ATELIER_TAG` always comes from `current-tag`.

## Architecture

<!-- Updated: Red Team 2026-09-25 - F2 backup pipeline -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Validation Session 1 - verified restic rewrite flags -->

```
04:30 UTC timer ─► atelier-backup.sh ─► preflight (env, volume sentinel, container running) ─► flock
   ─► docker exec atelier python -m atelier.backup_db snapshot /data/backup/atelier.db   (prints image count)
   ─► restic unlock ─► restic backup --tag nightly /data/images /data/backup/atelier.db
   ─► restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
   ─► docker exec atelier python -m atelier.backup_status record --event backup --ok --images N
Sun 05:00 UTC ─► atelier-backup-verify.sh ─► flock ─► restic unlock ─► restic check --read-data-subset=5%
   ─► restic restore latest --include /data/backup/atelier.db --target <volume tmp, seen as /data/.verify.X>
   ─► restic ls --json --recursive latest /data/images
        | docker exec -i atelier python -m atelier.backup_db verify /data/.verify.X/data/backup/atelier.db
                                                                --pngs-from-stdin --live-db /data/atelier.db
   ─► record --event verify --ok [--warnings N] | --error MSG
Header partial ─► backup_status.read_status() ─► badge (ok / stale / failed)
```

`deploy/backup/atelier-backup-common.sh`:

```bash
# Sourced by the Atelier backup and verify jobs. Owns paths, logging, locking and the restic wrapper.
ENV_FILE=${ATELIER_BACKUP_ENV:-/opt/atelier/backup.env}
DATA=/mnt/atelier-data
RESTIC_IMAGE=restic/restic@sha256:<digest>           # pinned by digest; the tag it came from is noted in the guide
log()      { /usr/bin/logger -t "$LOG_TAG" -s -- "$*"; }
record()   { docker exec atelier python -m atelier.backup_status record "$@" || log "could not record status: $*"; }
fail()     { log "$2"; record --event "$EVENT" --error "$2"; exit "$1"; }
preflight() {
  [[ -r "$ENV_FILE" ]] || { log "config: $ENV_FILE not readable"; exit 1; }
  mountpoint -q "$DATA" && [[ -f "$DATA/.atelier-volume" ]] || { log "volume not mounted"; exit 2; }
  [[ "$(docker inspect -f '{{.State.Running}}' atelier 2>/dev/null)" == "true" ]] || { log "container not running"; exit 2; }
}
take_lock() { exec 9>/run/lock/atelier-backup.lock; flock -w 3600 9 || { log "another backup job holds the lock"; exit 2; }; }
restic_run() {   # throttled so it never starves Folio: the unit's Nice= doesn't reach a docker-run process
  docker run --rm -i --hostname atelier-backup --env-file "$ENV_FILE" \
    -e NICE=10 -e IONICE_CLASS=2 -e IONICE_PRIORITY=7 -e GOMAXPROCS=1 --cpus 1 --memory 1g \
    -v "$DATA:/data:ro" -v "$DATA/.restic-cache:/cache" "$RESTIC_IMAGE" --cache-dir /cache "$@"
}
```

`deploy/backup/atelier-backup.sh`:

```bash
#!/usr/bin/env bash
# Nightly Atelier backup: consistent SQLite snapshot and images to the restic repository on R2.
# Exit codes: 0 ok, 1 config missing, 2 volume/container/lock unavailable, 3 snapshot failed, 4 restic failed.
set -euo pipefail
LOG_TAG=atelier-backup EVENT=backup
source /opt/atelier/backup/atelier-backup-common.sh
preflight; take_lock
images=$(docker exec atelier python -m atelier.backup_db snapshot /data/backup/atelier.db) || fail 3 "database snapshot failed"
restic_run unlock >/dev/null 2>&1 || true
restic_run backup --tag nightly /data/images /data/backup/atelier.db || fail 4 "restic backup failed"
restic_run forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune || fail 4 "restic retention failed"
record --event backup --ok --images "$images"
log "ok: snapshot with $images images"
```

`atelier-backup-verify.sh` follows the same shape with `EVENT=verify`:
1. Create the temporary directory: `tmp=$(mktemp -d "$DATA/.verify.XXXXXX"); chmod 0755 "$tmp"; trap 'rm -rf "$tmp"' EXIT`. Inside the container it appears as `/data/$(basename "$tmp")`.
2. `restic_run unlock` (errors ignored), then `restic_run check --read-data-subset=5%` (fail 4).
3. A restore run with `-v "$tmp:/restore"`: `restore latest --target /restore --include /data/backup/atelier.db` (fail 4).
4. `restic_run ls --json --recursive latest /data/images | docker exec -i atelier python -m atelier.backup_db verify "/data/$(basename "$tmp")/data/backup/atelier.db" --pngs-from-stdin --live-db /data/atelier.db` (fail 3). Capture the warnings count from its JSON summary.
5. `record --event verify --ok --warnings <count>`.

Timer (the verify timer is the same with `OnCalendar=Sun *-*-* 05:00:00 UTC`):

```ini
[Unit]
Description=Nightly Atelier backup at 04:30 UTC (after Folio's 03:00 and 03:30 jobs)

[Timer]
OnCalendar=*-*-* 04:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

The service is `Type=oneshot`, `After=network-online.target docker.service`, `Requires=docker.service`, `Nice=10`, `IOSchedulingClass=best-effort`, `IOSchedulingPriority=7`, `TimeoutStartSec=4h` and `ExecStart=/opt/atelier/backup/atelier-backup.sh`.

`backup-status.json` shape:
`{"backup": {"last_run": ts, "last_success": ts, "error": null, "images": N}, "verify": {"last_run": ts, "last_success": ts, "error": null, "warnings": N}}`.

**Restore runbook.** Every step runs as root on the server, and every compose action goes through `deploy.sh`.
- **Path (a), DB-only.** Use it for a damaged or lost database while the images are intact.
  1. Run `/opt/atelier/deploy.sh stop`.
  2. Restore the snapshot DB into a new temporary directory on the volume: `restore latest --target /restore --include /data/backup/atelier.db`.
  3. Move the damaged `atelier.db`, `-wal` and `-shm` aside with a timestamp suffix. Install the restored DB as `/mnt/atelier-data/atelier.db` (owner 10001, mode 0600).
  4. Run `verify-restore --db /data/atelier.db --root /data --report-missing` against the live images, in a one-off `docker run --rm --network none` of the current image with the volume mounted.
  5. For each missing PNG or thumbnail, run `restore latest --target /restore --include /data/<rel path>` and install the file into place with owner 10001.
  6. Run `/opt/atelier/deploy.sh start`.
- **Path (b), full.** Use it for a lost volume, and only onto a fresh or empty volume.
  1. Mount the new volume by the phase 4 procedure and create the sentinel.
  2. Restore everything into a staging directory on it: `restore latest --target /restore`.
  3. Move the files into place without nesting and without doubling space: `rsync -a --remove-source-files <staging>/data/images/ /mnt/atelier-data/images/`. Then install `<staging>/data/backup/atelier.db` as `/mnt/atelier-data/atelier.db`.
  4. `chown -R 10001:10001` the tree, remove the staging directory, run `verify-restore`, and start with `/opt/atelier/deploy.sh start`.

**Purge from backups,** for an image the owner deleted and wants gone from R2:
1. Take the lock (`source …/atelier-backup-common.sh; take_lock`).
2. Preview with `restic_run rewrite --dry-run --exclude /data/images/<YYYY>/<MM>/job-<id>.png --exclude /data/images/<YYYY>/<MM>/job-<id>.webp`.
3. Run the same command with `--forget` in place of `--dry-run`, then `restic_run prune`. restic's documentation confirms three things:
   - `rewrite` supports the `--exclude` family;
   - `--forget` removes the original snapshots at once;
   - `rewrite` frees no data until `prune` runs.
4. Caveats, which the runbook states:
   - The database snapshots inside older backups keep that image's row, including its prompt and seed, until they age out (at most 6 months). Rewriting the database file out of every snapshot would destroy the database backups.
   - Modal keeps the output for 7 days; Atelier deleted the call ID with the job row.

## Related Code Files

<!-- Updated: Red Team 2026-09-25 - F2 backup pipeline -->

Create:
- `/Users/sweet-home/Works/artio/atelier/backup_db.py`
- `/Users/sweet-home/Works/artio/atelier/backup_status.py`
- `/Users/sweet-home/Works/artio/deploy/backup/atelier-backup-common.sh`
- `/Users/sweet-home/Works/artio/deploy/backup/atelier-backup.sh`
- `/Users/sweet-home/Works/artio/deploy/backup/atelier-backup-verify.sh`
- `/Users/sweet-home/Works/artio/deploy/systemd/atelier-backup.service`
- `/Users/sweet-home/Works/artio/deploy/systemd/atelier-backup.timer`
- `/Users/sweet-home/Works/artio/deploy/systemd/atelier-backup-verify.service`
- `/Users/sweet-home/Works/artio/deploy/systemd/atelier-backup-verify.timer`
- `/Users/sweet-home/Works/artio/tests/fixtures/restic-ls-recursive.jsonl`: captured from a real run of the pinned restic image against a throwaway local repository.
- `/Users/sweet-home/Works/artio/tests/test_backup_db.py`
- `/Users/sweet-home/Works/artio/tests/test_backup_status.py`

Modify:
- `/Users/sweet-home/Works/artio/atelier/routes/pages.py`: the header-status handler adds `backup_status.read_status()`.
- `/Users/sweet-home/Works/artio/atelier/templates/partials/header_status.html`: the backup badge.
- `/Users/sweet-home/Works/artio/docs/deployment-guide.md`: the "Backups and restore" section, with restore paths (a) and (b) and the purge procedure.

Delete: none.

## Implementation Steps

<!-- Updated: Red Team 2026-09-25 - F2 backup pipeline -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->

**A. Repository work.**

1. **`backup_db.py`.** Implement the three subcommands with `argparse`, importing `DB_FILENAME` and reading only `ATELIER_DATA_DIR`. All output is one JSON line or one integer, never file contents.
2. **`backup_status.py`.** Implement `record` (write to a temporary file, then `os.replace`, as uid 10001) and `read_status` with the thresholds from Requirements.
3. **Backup badge.** Add it to `header_status.html`.
   - `ok` shows "Backup ✓ 5 h ago · verified 2 d ago", plus "N deleted after snapshot" when verify warned.
   - `stale` is amber and shows the age.
   - `failed` is red and shows the escaped error text.
   - Extend the pages route to pass the status in.
4. **Scripts and units.** Write the common, backup and verify scripts and the four unit files as sketched.
   - Pin `RESTIC_IMAGE` by digest in the common script. Look it up with `docker buildx imagetools inspect restic/restic:<version>`.
   - Run `shellcheck deploy/backup/*.sh`, which the CI `find` already covers.
5. **Real fixtures.** On the laptop, capture the fixture from the pinned restic image and a throwaway repository. No R2 is needed.

   ```bash
   T=$(mktemp -d); mkdir -p "$T/data/images/2026/09" "$T/data/backup"
   python3 -c "from PIL import Image; Image.new('RGB',(8,8)).save('$T/data/images/2026/09/job-1.png')"
   touch "$T/data/images/2026/09/job-1.webp" "$T/data/backup/atelier.db"
   R="docker run --rm -e RESTIC_PASSWORD=fixture -v $T/repo:/repo -v $T/data:/data restic/restic@sha256:<digest> -r /repo"
   $R init && $R backup /data/images /data/backup/atelier.db
   $R ls --json --recursive latest /data/images > tests/fixtures/restic-ls-recursive.jsonl
   ```

   Keep the captured file as it is. Tests derive their variants (a missing PNG, no PNG at all) from it in code.
6. **Tests.**
   - **`tests/test_backup_db.py`:**
     - `test_snapshot_is_consistent_while_writer_is_active`: a thread inserts rows during the snapshot; the result passes the integrity check and holds a row count the writer actually reached.
     - `test_verify_passes_on_a_real_recursive_listing`: uses the captured fixture.
     - `test_verify_errors_when_the_listing_has_no_png_nodes`: uses the fixture minus its PNG lines, as a non-recursive listing would produce.
     - `test_verify_fails_when_a_referenced_png_is_missing_but_still_live`.
     - `test_verify_warns_when_a_missing_png_was_deleted_after_the_snapshot`.
     - `test_verify_reports_but_accepts_extra_pngs`.
     - `test_verify_runs_with_an_empty_environment`: run `python -m atelier.backup_db verify …` in a subprocess with `env={}` and the repo root as working directory; it exits 0.
     - `test_verify_restore_detects_sha256_mismatch`, `test_verify_restore_reports_missing_files` and `test_verify_restore_detects_corrupt_database`.
   - **`tests/test_backup_status.py`:** never, ok, ok with warnings, stale at 37 h, failed backup, failed verify and verify stale at 9 days, plus the header partial rendering each state through the app client.
7. **Lint, test and commit.** Run `uv run ruff check` and `uv run pytest -q`, then commit as `feat: nightly restic backups to R2 with weekly restore verification`. Push to `main`, which is **[OWNER-GATED]** because it deploys.

**B. Owner-gated setup. Ask the owner before each step, and stop on any unexpected output.**

8. **[OWNER-GATED] R2.**
   - Create the bucket `atelier-backups` with **no bucket lock rules**.
   - Create an R2 API token with Object Read & Write scoped to that bucket only. Note the Access Key ID, the Secret and the account ID for the endpoint.
9. **[OWNER-GATED] restic password.**
   - Generate it with `openssl rand -base64 32` on the laptop.
   - The owner saves it in the password manager **first**.
10. **[OWNER-GATED] Credentials file on the server.** Create it with `install -m 0600 /dev/null /opt/atelier/backup.env`, then fill it in with an editor:
    - `RESTIC_REPOSITORY=s3:https://<ACCOUNT_ID>.r2.cloudflarestorage.com/atelier-backups`
    - `RESTIC_PASSWORD`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
    - `AWS_DEFAULT_REGION=auto`
11. **[OWNER-GATED] Install the scripts and initialize.**
    - Run `install -d -m 0750 /opt/atelier/backup` and copy the three scripts there as root with mode 0750.
    - Run `install -d -m 0700 /mnt/atelier-data/.restic-cache`.
    - Run `docker pull restic/restic@sha256:<digest>`, then `source /opt/atelier/backup/atelier-backup-common.sh && restic_run init`.
12. **[OWNER-GATED] Install the units.**
    - Copy the four units to `/etc/systemd/system/`.
    - Run `systemd-analyze verify /etc/systemd/system/atelier-backup*.{service,timer}` and `systemd-analyze calendar '*-*-* 04:30:00 UTC' 'Sun *-*-* 05:00:00 UTC'`.
    - Run `systemctl daemon-reload && systemctl enable --now atelier-backup.timer atelier-backup-verify.timer`.
13. **[OWNER-GATED] First backup.**
    - Run `systemctl start atelier-backup.service`.
    - While it runs, check the throttling on the restic process: `ps -o pid,ni,comm -C restic` shows nice 10, and `ionice -p <pid>` shows best-effort priority 7.
    - Afterwards, check `journalctl -t atelier-backup -n 30 --no-pager` and `restic_run snapshots` for one snapshot tagged `nightly`. The header badge turns green.
14. **[OWNER-GATED] First verify.** Run `systemctl start atelier-backup-verify.service`. The journal ends with ok, and the badge shows the verify time.
15. **[OWNER-GATED] Restore rehearsal, once, non-destructively.**
    - **Path (a), rehearsed without touching the live DB.**
      1. Restore only the snapshot DB into a temporary directory on the volume.
      2. Run `verify-restore --db <that copy> --root /data --report-missing` against the live images, in a one-off `docker run --rm --network none` of the current image. It reports nothing missing.
      3. Restore one PNG with `--include` into the temporary directory and check that its sha256 equals the live file's.
    - **Full-data check,** while the data is still small: restore the whole snapshot into an empty temporary directory and run `verify-restore` on it. Every sha256 must match.
    - Remove both temporary directories, and record the date and result in the guide.
16. **[OWNER-GATED] Failure display.**
    - Copy `backup.env` to `/opt/atelier/backup-broken.env` (0600) with `RESTIC_REPOSITORY` pointing at a non-existent bucket path.
    - Run `ATELIER_BACKUP_ENV=/opt/atelier/backup-broken.env /opt/atelier/backup/atelier-backup.sh`. It must exit 4, and the badge turns red with the message.
    - Delete the broken file, re-run the real service, and the badge turns green again.
17. **Runbook.** Write the "Backups and restore" section with restore paths (a) and (b) and the purge procedure from Architecture. Every compose action goes through `deploy.sh stop | start`.

## Todo List

- [ ] `backup_db.py` with `snapshot`, `verify` (recursive listing, live-DB check) and `verify-restore`; no `Settings`; tests including the empty-environment run
- [ ] Fixture captured from a real restic run against a throwaway repository
- [ ] `backup_status.py` with `record` and `read_status`, the header badge with warnings, and its tests
- [ ] Backup, verify and common scripts with the digest-pinned, throttled restic image; `shellcheck` clean
- [ ] systemd services and timers at 04:30 UTC daily and Sunday 05:00 UTC
- [ ] [OWNER-GATED] R2 bucket without lock and a bucket-scoped token
- [ ] [OWNER-GATED] restic password in the password manager, `backup.env` 0600, `restic init`
- [ ] [OWNER-GATED] Units installed and timers enabled; first backup (with throttling checked) and first verify green
- [ ] [OWNER-GATED] Restore rehearsal of path (a) and the full-data check recorded; failure shown in the UI and cleared
- [ ] Runbook with restore paths (a) and (b) and the purge procedure

## Success Criteria

<!-- Updated: Red Team 2026-09-25 - F2 backup pipeline -->

**Criterion 11:**
- `restic snapshots` lists the nightly snapshot, and the next timer run at 04:30 UTC adds another (`systemctl list-timers 'atelier-*'`).
- The verify job restores the DB into a temporary directory, lists the snapshot's images recursively, confirms that every referenced PNG is present or was deleted after the snapshot, and compares the DB image count with the snapshot's PNG count.
- The restore rehearsal of path (a) and the full-data check both pass, and their date is recorded in `docs/deployment-guide.md`.
- A deliberately broken run turns the header badge red with its message.
- The unit tests for `backup_db` (on a real restic fixture, and with an empty environment) and `backup_status` are green.

## Verification

```bash
cd /Users/sweet-home/Works/artio
uv run ruff check && uv run pytest -q tests/test_backup_db.py tests/test_backup_status.py
shellcheck deploy/backup/*.sh
# [OWNER-GATED] on folio-prod-1
ssh folio-prod 'systemctl list-timers "atelier-*" --no-pager'
ssh folio-prod 'systemctl start atelier-backup.service; journalctl -t atelier-backup -n 20 --no-pager'
ssh folio-prod 'pid=$(pgrep -x restic | head -1); [ -n "$pid" ] && ps -o pid,ni,comm -p "$pid" && ionice -p "$pid"'   # during a backup
ssh folio-prod 'source /opt/atelier/backup/atelier-backup-common.sh; restic_run snapshots --latest 3'
ssh folio-prod 'systemctl start atelier-backup-verify.service; journalctl -t atelier-backup-verify -n 20 --no-pager'
ssh folio-prod 'docker exec atelier python -c "import json;print(json.load(open(\"/data/backup-status.json\")))"'
```

## Risk Assessment

<!-- Updated: Red Team 2026-09-25 - F2 backup pipeline -->
<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->

| Risk | Likelihood × impact | Observable signal | Pre-decided response |
|---|---|---|---|
| The restic password is lost | Low × Critical | None until a restore is attempted | Prevention only: the password manager entry is stored before `restic init`, and the runbook names where it lives. |
| Host root reads or destroys the backups | Low × Critical | A snapshot count drop or unexpected `forget` activity in R2 metrics | Accepted with the shared host. Detection is the badge plus the owner's R2 dashboard; phase 4's key audit and LearnFlow restriction reduce who holds root. |
| The R2 token is scoped wrongly | Medium × Low | `restic init` fails with 403 or AccessDenied | **[OWNER-GATED]** Recreate the token with Object Read & Write on `atelier-backups`. |
| A stale lock follows a crashed run | Low × Low | Exit code 11 in the journal | `restic unlock` at the start of each run clears it. If it persists, the owner runs `restic_run unlock --remove-all` after confirming that no restic container is running (`docker ps`). |
| The first backup is long and overlaps the verify | Medium × Low | The verify journal shows it waited on flock | This is expected, because flock serializes the two jobs. No action. |
| restic still competes with Folio for CPU or I/O | Low × Medium | Folio latency around 04:30, or `ps`/`ionice` in step 13 not showing nice 10 and best-effort 7 | Confirm the image honours `NICE`/`IONICE_*` (research-01 §5). If not, keep `--cpus 1` and add `--blkio-weight 100`, then re-check. The owner may also move the timer to 05:30 UTC. |
| The real `restic ls` format changes with a restic upgrade | Low × Medium | The verify exits 3 with "listing contains no PNG nodes" right after an image-digest bump | Recapture the fixture with the new image before bumping the digest, and fix the parser against it. |
| Restore temporary space fills the volume | Low × Medium | The disk guard trips during the full-data check | The full-data check runs once, while data is small. Weekly verifies restore only the DB, and restore path (b) moves files with `--remove-source-files` so space is never doubled. |

**Rollback:**
- Run `systemctl disable --now atelier-backup.timer atelier-backup-verify.timer` and remove the units and `/opt/atelier/backup/`. The app keeps working, and the badge shows "stale".
- The R2 bucket and its snapshots stay as they are, and the token can be revoked.
- `git revert` removes the badge code.

## Security Considerations

<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->

- restic encrypts on the server before upload. The R2 token can reach only `atelier-backups`, and it and the password exist only in `backup.env` (0600 root) and the password manager.
- **Residual risk:** anyone with root on `folio-prod-1` can read `backup.env`, decrypt every snapshot and, with bucket locks off, delete the history. This comes with the shared host; see phase 4's residual-risk statement.
- The restic image is pinned by digest in the script, so a moved tag can't change what receives the password.
- The restic container mounts the data read-only, except in the restore steps, which write only to a temporary directory on the volume. The verify's DB check runs in the live container, and the one-off `verify-restore` runs with `--network none`.
- Nothing in the scripts prints environment values, and status and error text never include credentials.
- Deleting an image doesn't remove it from existing snapshots. The purge procedure removes the files; database rows age out within 6 months.
- Only Atelier paths, Atelier timers and the Atelier container are touched. Folio's cron, `/etc/cron.d/folio-backups` and GCS are untouched.

## Next Steps

Phase 6 adds GPU status, warm-up and stop, and their badge in the same header partial.
