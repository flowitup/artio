# Scout: folio-prod-1 live state (read-only probe, 2026-09-25 ~13:35 UTC)

Access was over the SSH alias `folio-prod` as root, with BatchMode on. Only read-only commands ran. No env files or secrets were read.

## Host
- **Hostname and OS:** `folio-prod-1`, Ubuntu 26.04 LTS.
- **Resources:** 4 vCPU; 7.7 GB RAM, of which 5.8 GB is available.
- **Disk:** one disk, with `/dev/sda1` of 75 GB mounted at `/`. **52 GB is used and 21 GB free (72%).** No Hetzner Volume is attached, and nothing is mounted under `/mnt`.
- **Docker:** Docker 29.1.3 and Compose v2.40.3, both from the Ubuntu packages. Images take 45.6 GB across 189 images, of which 5 are active and about 16.9 GB is reclaimable. Local volumes take 6.4 GB.
- **Tools:** there is no `restic`, `rclone`, `gcloud` or `sqlite3` binary on the host. `caddy` is at `/usr/bin/caddy` and `cloudflared` at `/usr/local/bin/cloudflared`.
- **Firewall:** ufw is inactive. The only public listener is SSH on port 22. Tailscale is running (100.64.x.x and fd7a:115c:a1e0:: listeners).
- **Users:** there are no `deploy`, `folio` or `learnflow` users. The operator alias logs in as root.

## Running services
- **systemd:** `cloudflared`, `caddy` and `docker` are active. `nginx` is inactive.
- **Containers** (compose project `folio`):
  - `folio-frontend-1` on 127.0.0.1:3000.
  - `folio-api-1` on 127.0.0.1:5000.
  - `folio-worker-1`.
  - `folio-minio-1` on 127.0.0.1:9000 and 9001.
  - `folio-db-1` (postgres:16-alpine).
  - `folio-redis-1`.
  - Images are pulled from GCP Artifact Registry (`europe-west1-docker.pkg.dev/flowitup-folio-prod/folio/*`).
- **Ports already in use on localhost:** 2019 (Caddy admin), 3000, 5000, 8080 (Caddy site for LearnFlow, root `/var/www/learnflow`), 9000, 9001, 20241 (cloudflared metrics) and 33161. **Port 8090 is free.** Pick Atelier's port from the free ones and re-check before binding.

## Cloudflare Tunnel ingress (live `/etc/cloudflared/config.yml`, hostname and service lines only)
```
- hostname: folio.flowitup.com   path: ^/health$   service: http://localhost:5000
- hostname: folio.flowitup.com   path: ^/api/.*    service: http://localhost:5000
- hostname: folio.flowitup.com                     service: http://localhost:3000
- hostname: cdn.flowitup.com                       service: http://localhost:9000
- hostname: learn.flowitup.com                     service: http://localhost:8080
- service: http_status:404
```
The Folio repo copy (`~/Works/folio/infra/cloudflare/cloudflared-config.yml`) is missing the `learn.flowitup.com` rule. Always edit the live file, never re-copy from the repo.

## Scheduled jobs
- **Folio backups:** `/etc/cron.d/folio-backups` runs `0 3 * * * /usr/local/bin/pg-dump.sh` and `30 3 * * * /usr/local/bin/minio-mirror.sh`. Both logged `ok` in journald on 2026-09-25 (pg dump and a mirror of 6451 objects to `gs://flowitup-folio-prod-backups`). The host scripts differ from the repo copies, because gcloud is not installed on the host.
- **Other timers:** only the stock Ubuntu ones (sysstat, apt, logrotate, e2scrub, xfs_scrub). The heavy weekly ones run on Sunday around 03:10 UTC.

## Consequences for Atelier
1. **Data must not live on `/`.** With 21 GB free, at about 4 MB per PNG, roughly 5,000 images would fill the disk that Folio's Postgres and MinIO use. The contract's fallback applies: put Atelier's data on a dedicated Hetzner Volume attached to this server, mounted at a fixed path by UUID, with the app's disk guard pointed at it. Creating the volume is an owner action in Folio's Hetzner project, which is not in the local hcloud contexts.
2. **Atelier's images should stay small**, and deploys should prune old Atelier image tags (keep 3). That way Atelier doesn't add to the 45.6 GB of Docker images on the root disk.
3. **Ingress:** add one rule `atelier.flowitup.com → http://localhost:<port>` before the catch-all. No Caddy change is needed.
4. **Backup schedule:** run after Folio's 03:00 and 03:30 UTC jobs and away from Sunday's 03:10 scrubs. 04:30 UTC is fine.
5. **Binaries:** restic and sqlite3 aren't on the host. Run restic from its Docker image, and take the SQLite online backup from inside the Atelier container with Python's `sqlite3` module.
6. **Deploy access:** the only operator access is root over SSH. The Atelier deploy key should be a separate key restricted to a forced command (`restrict,command="/opt/atelier/deploy.sh"`), so that a leaked GitHub secret cannot open a root shell on Folio's production server.
