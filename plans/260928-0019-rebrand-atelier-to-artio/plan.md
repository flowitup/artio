---
title: "Rebrand Atelier to Artio"
description: "Full rename of the app, its URL, repo, image, server layout, plugin and local folder from atelier to artio."
status: completed
priority: P2
effort: 2h
branch: main
tags: [rename, infra, plugin]
created: 2026-09-28
---

# Rebrand Atelier to Artio

## Outcome

The owner chose a full rebrand to **artio** (2026-09-28), matching the family naming of folio and motio. After this plan:

- the app answers at `https://artio.flowitup.com`, and `atelier.flowitup.com` no longer resolves;
- the repo is `flowitup/artio`, the image is `ghcr.io/flowitup/artio`, and the server uses `/opt/artio`, `/mnt/artio-data`, `artio-egress.service` and an `artio-deploy` key;
- the plugin is `artio@artio-local`, and the checkout is `~/Works/artio`;
- every image, job, tag and preset is kept, and the Access AUD, owner email and service token Client ID are unchanged.

**Constraints:**
- No data loss: the database is copied before it is renamed.
- Downtime is limited to the server cutover, about 10 minutes.
- Folio, cdn and learn stay at their baselines.
- No secret value passes through the conversation.

**Non-goals:**
- The Modal app (`qwen21-uc`) keeps its name.
- The Modal tokens keep their `atelier-*` names, because Modal cannot rename tokens.
- Historical plan, report and journal text keeps the old product name.

## Status

| # | Step | Owner gate | Status |
|---|---|---|---|
| 1 | In-repo rename on branch `rebrand-artio`: package, env vars (`ATELIER_*` to `ARTIO_*`), UI, CI, deploy files, plugin, docs. Lint and 585 tests pass; plugin and marketplace validate. | none | done (`2bbe393`) |
| 2 | Cloudflare, with no downtime: add the `artio.flowitup.com` tunnel ingress and DNS route; add the hostname to the existing Access app (AUD unchanged); rename the app to Artio and the service token to `artio-plugin`. | yes | done |
| 3 | GitHub: rename the repo to `flowitup/artio` and repoint the local remote. Create an `artio-deploy` key and store it as the `production` secret `ARTIO_DEPLOY_SSH_KEY` straight from a file, never displayed. | yes | done |
| 4 | Server cutover (downtime): see "Server cutover" below. | yes | done |
| 5 | Merge `rebrand-artio` into `main` and push, so CI builds `ghcr.io/flowitup/artio` and deploys through `/opt/artio/deploy.sh`. | yes | done (after one fix, below) |
| 6 | Verify: `/healthz` reports the new version; the gallery shows every existing image; one live render (about $0.03); neighbours at baseline; `atelier.flowitup.com` still reaches Access until step 7. | yes (GPU) | done |
| 7 | Cleanup: remove the `atelier` tunnel rule, DNS record and Access hostname; delete the old GitHub secret, the old server images and the pre-rename copy on the server. | yes | done |
| 8 | Plugin: uninstall `atelier@atelier-local`, add the `artio-local` marketplace, install `artio@artio-local`. The owner enters the Client ID and secret; the same secret still works. Optionally move `~/Atelier` to `~/Artio`. | yes (secret) | done |
| 9 | Local: rename `~/Works/atelier` to `~/Works/artio`, repair worktrees, repoint the marketplace path, rebuild `.venv`, update absolute paths in plans. | none | done |
| 10 | Owner-only leftovers: delete the old GHCR package `flowitup/atelier` (a permanent deletion, so the owner does it); rename the Hetzner volume `atelier-data` in the console (a label only). | owner | pending |

## Server cutover (step 4)

This runs as root on folio-prod-1 from one script, stopping at the first error.

1. Copy `/opt/atelier` (including `.env`) to `/root/atelier-pre-rename-<ts>/` (0700), and back up `/etc/fstab` and root's `authorized_keys`.
2. Run `/opt/atelier/deploy.sh stop`: the container stops and the old compose network is removed.
3. Copy `atelier.db` (and any `-wal`/`-shm` files) to `/mnt/atelier-data/backup/pre-rename-<ts>/`, then rename them to `artio.db*`.
4. Unmount `/mnt/atelier-data`. In fstab, change only the mount point (same UUID and options). Create `/mnt/artio-data`, mount it, remove the empty old mount point, and set the filesystem label with `e2label` to `artio-data`.
5. Move `/opt/atelier` to `/opt/artio`, then install the new `compose.yaml` and `deploy.sh` from the branch.
   - Rewrite the `.env` key names from `ATELIER_` to `ARTIO_` and set `ARTIO_PUBLIC_ORIGIN=https://artio.flowitup.com`.
   - Remove `current-tag`, `previous-tag` and `maintenance`: the old images read the old variable names, so they can't be rolled back to.
6. Stop and disable `atelier-egress`, then install and enable `artio-egress`. The rules are identical, on the same subnet.
7. In `authorized_keys`, replace the `atelier-deploy` line with `restrict,command="/opt/artio/deploy.sh" … artio-deploy`.

**Rollback before step 5 succeeds:**
- Restore `fstab`, the mount, `authorized_keys` and `/opt/atelier` from the copies, and rename `artio.db` back.
- Re-enable `atelier-egress`, then run `deploy.sh start` with the old tag.
- Keep `main` unpushed, or revert the merge.

## Risks

| Risk | Signal | Response |
|---|---|---|
| The CI push to the new GHCR package is refused | The build job fails on push | Allow the repo to write the package in the org's package settings (owner), then re-run. |
| The first deploy fails its health check | The deploy job fails, and `/healthz` is down | There's no previous artio image to fall back to. Read the container log, fix, and push again, or run the rollback above. |
| The cloudflared restart blips folio, cdn and learn | A short 502 | This was measured at about 2 s during the original cutover. Check the baselines right after. |
| The plugin secret isn't in the password manager | The owner can't re-enter it | Rotate it in Zero Trust, then configure the plugin with the new value. |

## Result (2026-09-28)

- **Cutover:** Artio went down at 22:39Z and was serving `ae7c4bf` again by about 22:45Z, roughly 6 minutes.
- **Data:** everything came through: 18 images (36 files), 24 jobs, 1 workflow and 2 tags.
- **Neighbours:** folio 200, cdn 403 and learn 302, unchanged.
- **Gap found during the cutover:** the first deploy crash-looped with "production requires the volume sentinel at /data/.artio-volume". The volume's marker file was still named `.atelier-volume`, and the cutover script had renamed the database but not the marker.
  - Fix: rename the file on the volume, remove the crash-looping container, and run `deploy.sh apply` for the same, already-verified image.
  - The CI run for `ae7c4bf` therefore reads as failed; the server-side apply succeeded.
- **Verified live:**
  - the pages say "Artio" and the gallery shows all images;
  - a POST (star and unstar) passes the origin check on the new hostname;
  - the plugin `artio@artio-local` rendered image 19 into `~/Artio`;
  - `atelier.flowitup.com` no longer routes to Artio: its tunnel rule, DNS record and Access hostname are removed, so it now falls to the zone's `*.flowitup.com` wildcard like any unused name.
- **Cleaned up:** the old images, the pre-rename copy and the staging files on the server, and the `ATELIER_DEPLOY_SSH_KEY` secret.
- **Kept:** the database copy at `/mnt/artio-data/backup/pre-rename-20260927T223910Z/`, which the owner can remove once satisfied.
- **Owner leftovers:**
  - delete the GHCR package `flowitup/atelier`;
  - rename the Hetzner volume `atelier-data` in the console;
  - optionally move the older saved images from `~/Atelier` to `~/Artio`.

