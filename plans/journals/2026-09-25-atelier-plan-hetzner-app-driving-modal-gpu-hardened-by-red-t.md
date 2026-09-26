---
title: "Atelier plan: Hetzner app driving Modal GPU, hardened by red team"
date: 2026-09-25
summary: "Brainstormed, researched and planned Atelier (8 phases, 68h); red team caught a poll bug that would have failed every job and a shared-host root-key exposure."
---

# Atelier plan: Hetzner app driving Modal GPU, hardened by red team

## What happened
The owner asked to turn the Modal script `qwen21_uc_app.py` (Qwen-Image 2.1 UC on ComfyUI, L40S, scales to zero) into an application on Hetzner, like LearnFlow or Folio, to manage image generation. The session went from brainstorm to a contract, then research, then a plan, a red team review and a validation interview. The result is the validated plan `plans/260925-1331-atelier-image-studio/plan.md`, with 8 phases, 68h and 78 tasks. `ak plan validate` passes.

Scouting changed several assumptions:
- Hetzner Cloud offers no GPU server types (`hcloud server-type list`). Dedicated GEX44 servers cost €184–234 a month, while Modal costs about $0.009 per warm image, so the GPU stays on Modal and Hetzner hosts only the management app.
- Folio production moved from GCP to Hetzner on 2026-07-15, but its README still describes GCP. LearnFlow now shares the same box (`folio-prod-1`), behind one locally-managed Cloudflare Tunnel.
- `folio-prod-1` has 21 GB free on a 75 GB root disk shared with Folio's Postgres and MinIO. Atelier's images (about 4 MB each) therefore get a dedicated 50 GB Hetzner Volume.

## Decision
- **App.** Atelier is one FastAPI + HTMX + SQLite container, bound to 127.0.0.1 behind the existing tunnel and Cloudflare Access, with in-app JWT verification.
- **Jobs.** Jobs run asynchronously through `spawn()` plus call-ID polling, and polling resumes after a restart.
- **Models.** A model-neutral registry is in place, because more models are coming.
- **GPU warm-up.** Warm-up sends `ping()` calls instead of setting `min_containers`, so it fails safe if Atelier dies.
- **Backups.** Nightly restic backups go to R2.
- **Claude plugin.** The plugin is limited to an allowlist of `/api/v1` endpoints.

The red team (four lenses, 36 findings merged into 15, all accepted) caught real defects:
- **Poll bug.** `FunctionCall.get(timeout=0)` raises Python's builtin `TimeoutError` while a job is pending (`modal/_functions.py:334`), not `modal.exception.TimeoutError`. The planned catch order would have failed every job on its first poll.
- **Swallowed errors.** A `TRANSIENT` tuple containing `grpclib.GRPCError` would have swallowed `AuthError` and `NotFoundError` as if they were network blips.
- **Cancel.** `FunctionCall.cancel()` on synchronous methods under `@modal.concurrent` sends SIGINT to the whole container. Cancel is now recorded in the database only.
- **Operations.** Other defects were an unheld Stop lock, a non-recursive `restic ls`, an ungated tunnel cutover and rollback gaps.
- **Shared host.** Other projects' root-capable CI keys (LearnFlow's) can read Atelier's data on the shared host.

Owner decisions:
- Restrict LearnFlow's deploy key with rrsync after a read-only key audit.
- Keep the same Modal workspace, add a spend limit and use dedicated tokens.
- Plugin thumbnails are off by default.
- `ping()` probes ComfyUI `/system_stats`.
- Remove the legacy `api` endpoint.
- Storage: 50 GB volume, 40 GB image cap, 5 GB free-space floor.
- Presets: 9:16, 16:9 and 1:1.
- The service token lasts 1 year.

These are recorded as amendments in the contract `plans/reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md`.

## Next steps
1. Run `/ak:cook /Users/sweet-home/Works/qwen21-uc-modal/plans/260925-1331-atelier-image-studio/plan.md`, starting with phase 1 (repo bootstrap and Modal backend changes).
2. Every step touching folio-prod-1, Cloudflare, R2, GitHub or Modal is owner-gated.
3. Ten claims remain unverified until run on real systems, each with a fallback in its phase: systemd-run flags, cloudflared ingress syntax, the rrsync path form, the Tailscale SSH check, GHCR package access for `GITHUB_TOKEN`, and Modal budget alerts.
4. Unrelated follow-ups: the SSH alias `dev-deploy` presents a changed host key, and Folio's repo copy of the cloudflared config is missing the `learn.flowitup.com` rule.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
