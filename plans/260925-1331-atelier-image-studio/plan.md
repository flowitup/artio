---
title: "Atelier — private multi-model image studio on Hetzner"
description: "Private FastAPI + HTMX image studio on folio-prod-1 that drives Modal GPU backends, with Access auth, async jobs, GPU controls and a Claude plugin."
status: completed
priority: P2
effort: 68h
branch: main
tags: [feature, backend, frontend, infra]
blockedBy: []
blocks: []
created: 2026-09-25
---

# Atelier — private multi-model image studio on Hetzner

## Overview

<!-- Updated: Validation Session 1 - answers applied -->

Atelier is the owner's private image studio at `atelier.flowitup.com`: one Python service (FastAPI, HTMX, SQLite) in a confined container on `folio-prod-1`. The owner picks a model, runs single or batch generations on Modal GPU backends, follows the queue, and browses, searches, tags, remixes and deletes results. They can control GPU warm-up and stop, run uploaded ComfyUI workflows, and drive Atelier from Claude through a plugin. Pushing to `main` deploys a digest-pinned image automatically. There are no backups: the owner dropped them on 2026-09-27, so the data volume holds the only copy. Eight phases were planned; seven remain after that decision. The red-team fixes, the owner's decisions D1–D3 and the Validation Session 1 answers are applied. Every step that touches production, Cloudflare, R2, GitHub or Modal is marked **[OWNER-GATED]** and waits for the owner's explicit go-ahead.

## Phases

<!-- Updated: Validation Session 1 - effort recomputed -->

| # | Phase | Effort | Depends on |
|---|---|---|---|
| 1 | [Repo bootstrap & Modal backend](./phase-01-start.md) | 3h | none |
| 2 | [Engine](./phase-02-engine.md) | 12h | 1 |
| 3 | [Web UI & Access auth](./phase-03-web-ui-and-access-auth.md) | 11h | 2 |
| 4 | [Container, CI/CD & first production rollout](./phase-04-container-ci-cd-and-rollout.md) | 12h | 3 |
| 5 | ~~[Backups & restore](./phase-05-backups-and-restore.md)~~ (dropped by the owner on 2026-09-27: Atelier has no backups) | — | — |
| 6 | [GPU status, warm-up & stop](./phase-06-gpu-status-warm-stop.md) | 8h | 4 |
| 7 | [Prompt library & custom workflows](./phase-07-prompt-library-and-workflows.md) | 6h | 6 |
| 8 | [JSON API, Claude plugin & final acceptance](./phase-08-api-plugin-and-acceptance.md) | 9h | 7 |

Phases run strictly in order because they share `main.py`, `jobs.py`, `worker.py` and the templates. Live status: `ak plan status plans/260925-1331-atelier-image-studio`.

## Key decisions

<!-- Updated: Red Team 2026-09-25 - F1 poll pending signal -->
<!-- Updated: Red Team 2026-09-25 - F2 backup pipeline -->
<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F5 cancel semantics -->
<!-- Updated: Red Team 2026-09-25 - F7 tunnel cutover -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - F12 Modal tokens -->
<!-- Updated: Red Team 2026-09-25 - F15 GPU status model -->
<!-- Updated: Validation Session 1 - Modal script extras allowed -->
<!-- Updated: Validation Session 1 - storage defaults, presets and token lifetime -->

- **Stack and registry.** One service with a single uvicorn worker; the GPU stays on Modal. A model-neutral registry supplies the graph template, and the app calls the backend's existing `run_workflow`.
- **Modal script (contract amendment 8).** Exactly three changes:
  - `ping()` probes ComfyUI's `/system_stats`, answering `"ok"` only on HTTP 200;
  - the legacy `api` web endpoint is removed;
  - `fastapi[standard]` leaves the image.

  The model, workflow and sampler are unchanged.
- **Async jobs.**
  - `spawn()` returns a call ID stored in SQLite and polled with `get(timeout=0)`. The pending signal is Python's **builtin** `TimeoutError`; `OutputExpiredError` and `FunctionTimeoutError` are failures.
  - Transient errors are an allowlist. Permanent Modal errors fail the job and show a banner.
  - At most 4 jobs are in flight per backend, and the app picks every seed.
- **Cancel semantics.**
  - A user cancel marks the job cancelled in the DB only. A Modal cancel would SIGINT the synchronous backend's container and restart its sibling renders; the late result is discarded instead.
  - Only Stop cancels on Modal: every call at once, then container stop, then checks until nothing runs.
- **Access and authorization.** The origin verifies the Access JWT (signature, `aud`, `iss`, `exp`) and accepts the owner's email or the plugin's `common_name`. HTML routes are owner-only. The service identity reaches only criterion 10's nine `/api/v1` endpoints, so **the plugin has no warm or stop**.
- **Deploy.**
  - The image is deployed by the digest the `main` build produced, with its revision label checked, and the change runs detached from SSH.
  - The registry token and the actor's name arrive on stdin.
  - Health means the right version plus live worker loops; on failure, automatic rollback.
  - `deploy.sh rollback | stop | start | status` is the only manual path.
  - Actions are pinned by commit SHA, and deploy secrets live in a `production` environment limited to `main`.
- **Tunnel.** One ingress rule, added by a replica cutover in separately approved sub-steps. This is a deliberate, research-backed deviation from brief §6's plain restart. The Access app is created before the DNS route.
- **GPU control.**
  - Status is computed on read from 10 s and 60 s caches. Pings run only while a warm window is open, and there is no `min_containers`.
  - "Warm" is shown only after a successful ping. A failed ping marks the backend unhealthy and feeds the circuit breaker, which recycles a backend whose ComfyUI died.
- **Data.**
  - A 50 GB Hetzner Volume with a sentinel file, a 40 GB image cap and a 5 GB free-space floor (validated defaults).
  - **No backups** (owner decision, 2026-09-27). The volume holds the only copy of the images and the database. The planned restic-to-R2 backups, weekly verify, restore runbook and purge procedure were dropped with phase 5.
- **Presets and token (validated).**
  - Qwen-Image 2.1 UC offers 9:16 1088×1920 (default), 16:9 1920×1088 and 1:1 1328×1328, plus custom sizes in multiples of 16.
  - The Access service token lasts 1 year, with its expiry recorded and a calendar reminder set, and is revoked immediately on a leak.
- **D1: LearnFlow's deploy key.** After a read-only audit of root's keys and Tailscale SSH, LearnFlow's key is restricted to `rrsync -wo /var/www/learnflow`, with its workflow updated to match. This **amends the contract non-goal "don't change LearnFlow"**. Host root can still read Atelier's data: an accepted residual risk.
- **D2: Modal, same workspace.** Dedicated runtime and CI tokens, both workspace-wide because the workspace has no Service Users, plus a workspace spend limit. The legacy endpoint's proxy-auth tokens are revoked after its removal.
- **D3: plugin thumbnails off by default.** The tools take `include_thumbnail: bool = False`. Full PNGs are saved only under `save_dir` (`~/Atelier`, mode 0700).
- **Tests.** Fakes sit only at the Modal gateway, apart from three narrow stand-ins accepted in the red-team review: the SDK's output RPC in the poll-path test, stub binaries in the hermetic deploy-script test, and an Access-edge emulator in the plugin test. Auth tests use real RS256 JWTs, and one opt-in `pytest -m live` test runs real generations.

## Acceptance-criteria map (contract criteria 1–14)

<!-- Updated: Validation Session 1 - Modal script extras allowed -->

| # | Criterion | Proven in phases (success criteria) |
|---|---|---|
| 1 | Access redirect and origin 403 | 3 (JWT and route tests), 4 (live), 8 (record) |
| 2 | Job lifecycle, metadata, persistence and restart | 2, 3, 4 (cold start, restart mid-job), 8 |
| 3 | Determinism | 2 (graph parity, fresh-container live test), 8 |
| 4 | Errors, retry and cancel | 2, 3, 7 (ComfyUI validation text), 8 |
| 5 | Model registry, filter and second model with no migration | 2, 3, 8 |
| 6 | GPU status, warm-up and stop | 1 (`ping` probe), 6, 8 |
| 7 | Batch, variations and remix | 2, 3, 8 |
| 8 | Presets, stars, tags and search | 7, 8 |
| 9 | Custom workflows | 7, 8 |
| 10 | Claude plugin with service-token auth | 3 (route authorization), 8 |
| 11 | ~~Backups, weekly verify, restore test and UI status~~ | Dropped by the owner on 2026-09-27 |
| 12 | Disk guard and usage | 2, 3, 4 (volume), 8 |
| 13 | Deploy, health check, neighbours unaffected, `modal deploy` | 4, 8 |
| 14 | No credential in the repo, image or pages | 1, 3, 4, 8 |

## Owner actions by phase

<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F12 Modal tokens -->
<!-- Updated: Validation Session 1 - owner actions updated -->

From phase 4 onwards, every push to `main` deploys to production and needs the owner's go-ahead.

- **Phase 1:** approve `modal deploy` from `modal/`, which also removes the legacy web endpoint, and one ping smoke call (about $0.07).
- **Phase 2:** approve the live generation test (about $0.10).
- **Phase 4:**
  - Approve the read-only root-key and Tailscale audit. Restrict LearnFlow's key (D1), including its workflow change and a deploy check, and decide on any other unrestricted CI key the audit finds.
  - Create the Access app (email Allow plus Service Auth, cookie SameSite=Lax), the AUD tag, and the `atelier-plugin` service token. The token lasts 1 year: record its expiry date and set a calendar reminder to rotate it.
  - Create the Modal runtime and CI tokens, and set the workspace spend limit on the Usage & Billing page, plus usage alerts if offered. Revoke the proxy-auth tokens made unused by the endpoint removal (D2).
  - Create and attach the 50 GB Volume in Folio's Hetzner project. Create the GitHub repo, the `production` environment and its secrets, and grant the repo read access to the GHCR package if the first pull is denied.
  - Approve the volume mount, `/opt/atelier`, `.env`, the egress unit, the restricted key, tunnel sub-steps (a)–(g), the live checks and the `deploy-modal` run.
- **Phase 5:** none; the phase was dropped on 2026-09-27.
- **Phase 6:** approve the live GPU checks (about $1), including `deploy.sh stop` and `modal app stop --yes`.
- **Phase 7:** approve the live workflow run (about $0.10).
- **Phase 8:** install the plugin (desktop app, or Claude Code through the local marketplace) with the service token, and approve the final acceptance run (about $1.50) and the temporary cap change.

## Links

- [Accepted contract (brainstorm), with amendments](../reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md)
- [Architecture brief](./reports/architecture-brief.md)
- [Scout: Folio and LearnFlow patterns](./reports/scout-01-folio-learnflow-patterns.md) · [Scout: folio-prod-1 live state](./reports/scout-02-folio-prod-1-live-state.md)
- [Research: Access, tunnel, restic, R2](./research/researcher-01-cloudflare-access-tunnel-restic-r2.md) · [Research: Modal SDK, HTMX, plugin, GHCR](./research/researcher-02-modal-sdk-app-plugin.md)
- Red team: [failure modes](./reports/red-team-failure-mode-analyst.md) · [assumptions](./reports/red-team-assumption-destroyer.md) · [security](./reports/red-team-security-adversary.md) · [scope](./reports/red-team-scope-complexity-critic.md)

## Red Team Review

### Session — 2026-09-25
**Findings:** 15 (15 accepted, 0 rejected)
**Severity breakdown:** 3 Critical, 10 High, 2 Medium

Every load-bearing SDK claim was re-verified against modal 1.5.5 before it was applied.

| # | Finding | Severity | Disposition | Applied To |
|---|---------|----------|-------------|------------|
| 1 | Poll pending signal is the builtin `TimeoutError` | Critical | Accept | Phase 2, Phase 6 |
| 2 | Backup verify, config independence, restore runbook, restic throttling | Critical | Accept | Phase 5, Phase 2 |
| 3 | Co-tenant root keys (D1 audit and LearnFlow restriction, residual risk) | Critical | Accept | Phase 4, Phase 5, plan |
| 4 | Error taxonomy: transient allowlist, permanent errors, queue age, poller isolation | High | Accept | Phase 2, Phase 3, Phase 6 |
| 5 | Cancel semantics: DB-only user cancel; Stop gather, stop and converge | High | Accept | Phase 2, Phase 3, Phase 6, plan |
| 6 | Locks held across spawn; persisted ping call ID | High | Accept | Phase 2, Phase 6 |
| 7 | Tunnel cutover split into separately approved sub-steps | High | Accept | Phase 4, plan |
| 8 | Deploy: rollback subcommand, loop health, detached apply, hermetic tests (drill dropped) | High | Accept | Phase 4, Phase 2, Phase 3 |
| 9 | Supply chain: SHA pins, environment, digest deploy, egress block, locks | High | Accept | Phase 4, Phase 5, Phase 8 |
| 10 | Service identity allowlist; no plugin warm or stop; body limit; CSRF | High | Accept | Phase 3, Phase 4, Phase 6, Phase 7, Phase 8, plan |
| 11 | Image data control: D3 thumbnails, save confinement, delete, purge | High | Accept | Phase 8, Phase 3, Phase 5, Phase 1, Phase 2 |
| 12 | Modal tokens (D2), spend limit, proxy tokens | High | Accept | Phase 4, Phase 6, Phase 1, plan |
| 13 | Plugin install routes and `userConfig` | High | Accept | Phase 8 |
| 14 | Interfaces and tests: API by ID and offset, no bm25, HTML 200, route crawl | Medium | Accept | Phase 2, Phase 3, Phase 6, Phase 7, Phase 8 |
| 15 | GPU status on read, missing app means stopped, circuit breaker | Medium | Accept | Phase 6, Phase 1 |

The minor fixes are also applied: README line 9, two separate `git check-ignore` checks, and a full removal that deletes only the Atelier `authorized_keys` line.

### Whole-Plan Consistency Sweep
- Files reread: plan.md, phase-01-start.md, phase-02-engine.md, phase-03-web-ui-and-access-auth.md, phase-04-container-ci-cd-and-rollout.md, phase-05-backups-and-restore.md, phase-06-gpu-status-warm-stop.md, phase-07-prompt-library-and-workflows.md, phase-08-api-plugin-and-acceptance.md
- Decision deltas checked: 28 (the 15 fixes, D1–D3, and the minor fixes, split into their distinct changes)
- Stale-term greps: 35 terms. These include `modal.exception.TimeoutError` as the pending signal, `GRPCError`, `gpu_warm`, `gpu_stop`, `bm25`, `drill`, `rm -P`, `inputs.sha`, `input_headroom`, `gpu_status_tick` and `sibling`. The rest cover the older status loop, the 409 and 422 HTML codes, relevance ordering, the "$0.98 at most" claim, "exactly one power", "only copy", `before=`, `{name}`, `docker restart` and "sends no cookies". Every remaining hit is a correct reference to the new design.
- Cross-phase references checked: `route_ids`, `deploy.sh stop | start`, `worker.paused`/`worker.alerts`/`worker.status`, `SERVICE_ROUTES`, `DB_FILENAME`, `ping_call_id`, the `/healthz` loops field, `include_thumbnail` and `save_dir`. Each is defined in one phase and used consistently in the others.
- Reconciled stale references: 2. The phase 8 acceptance row 11 wording could be read as the removed drill. Route path-parameter names are now explicit in phases 3 and 8, so the route-enumerating crawl can fill every GET route.
- Unresolved contradictions: 0 within the plan. There was 1 with the amended contract (amendments 8–9 not yet applied to phases 1, 4 and 6), which Validation Session 1 resolved.

## Validation Log

### Session 1 — 2026-09-25
**Trigger:** post-red-team validation interview.
**Questions asked:** 7

Questions 1–3 were asked right after the red team, and 4–7 in this session. Options and answers are recorded as relayed by the coordinator; the question wording is reconstructed from that relay.

#### Questions & Answers

1. **[Risks]** Anyone with root on `folio-prod-1` can read Atelier's images and secrets, and LearnFlow's GitHub Actions deploy key is probably unrestricted root. How should Atelier handle the other root-capable keys on the shared host?
   - Options: Restrict LearnFlow's key (Recommended) | Move Atelier to its own VM | Accept the risk
   - **Answer:** Restrict LearnFlow's key (Recommended)
   - **Rationale:** It shrinks the set of credentials that can read Atelier's data without a new server. It amends the contract non-goal "don't change LearnFlow" (D1).
2. **[Architecture]** The Modal workspace has no Service Users, so every token is workspace-wide. How should Atelier's Modal access be scoped?
   - Options: Same workspace + spend limit (Recommended) | Separate Modal workspace
   - **Answer:** Same workspace + spend limit (Recommended)
   - **Rationale:** It keeps the existing backend and its model volume, and a workspace spend limit caps what a leaked token can cost (D2).
3. **[Tradeoffs]** Inline plugin thumbnails send image content to Anthropic and keep it in local Claude transcripts. Should the plugin return thumbnails by default?
   - Options: Off by default (Recommended) | On by default
   - **Answer:** Off by default (Recommended)
   - **Rationale:** Image content leaves Atelier's store only when a call asks for it (D3).
4. **[Scope]** Beyond adding `ping()`, which changes to the Modal script are allowed? (multi-select)
   - Options: ping() checks ComfyUI (Recommended) | Remove legacy 'api' endpoint (Recommended) | Neither, only add ping()
   - **Answer:** ping() checks ComfyUI (Recommended); Remove legacy 'api' endpoint (Recommended)
   - **Rationale:** "Warm" then means ComfyUI is ready, and a dead ComfyUI is caught during warm windows. Removing the endpoint closes the only generation path outside Access, and `fastapi[standard]` leaves the image.
5. **[Assumptions]** What storage defaults should Atelier's data volume use?
   - Options: 50 GB, cap 40, floor 5 (Recommended) | 100 GB, cap 90, floor 5 | 30 GB, cap 24, floor 3
   - **Answer:** 50 GB, cap 40, floor 5 (Recommended)
   - **Rationale:** It leaves 10 GB on the volume for the database, backup snapshots, the restic cache and restore staging, at about €2.85 a month.
6. **[Assumptions]** Which size presets should Qwen-Image 2.1 UC offer?
   - Options: 9:16, 16:9 and 1:1 (Recommended) | Also add 3:4 and 4:3 | Only 9:16 for now
   - **Answer:** 9:16, 16:9 and 1:1 (Recommended)
   - **Rationale:** It covers the benchmarked portrait default plus landscape and square. Custom sizes in multiples of 16 stay available.
7. **[Risks]** How long should the Cloudflare Access service token for the Claude plugin last?
   - Options: 1 year + reminder (Recommended) | 90 days | 30 days
   - **Answer:** 1 year + reminder (Recommended)
   - **Rationale:** Few rotations suit a single-owner tool. A recorded expiry date and a calendar reminder prevent a surprise outage, and a leak is answered by immediate revocation.

#### Confirmed Decisions
- **Server keys:** restrict LearnFlow's deploy key after the root-key audit, which shrinks root access to Atelier's data (D1).
- **Modal access:** same workspace, with dedicated runtime and CI tokens and a workspace spend limit, so the backend doesn't move (D2).
- **Plugin thumbnails:** off by default and opt-in per call, so image content leaves only on request (D3).
- **Modal script:**
  - `ping()` probes ComfyUI's `/system_stats`;
  - the legacy `api` endpoint and `fastapi[standard]` are removed;
  - the model, workflow and sampler are unchanged (contract amendment 8).
- **Storage:** a 50 GB volume, a 40 GB cap and a 5 GB floor, as the settings defaults.
- **Presets:** 9:16 1088×1920 (default), 16:9 1920×1088 and 1:1 1328×1328, plus custom multiples of 16.
- **Service token:** 1 year, with its expiry recorded, a calendar reminder, and immediate revocation on a leak.

#### Action Items
- [x] Phase 1:
  - ping probe, endpoint removal, `fastapi[standard]` dropped;
  - README HTTP section deleted;
  - tests, verification, risks and security updated.
- [x] Phase 2: storage defaults and presets marked as validated.
- [x] Phase 4:
  - proxy-auth tokens revoked after the endpoint removal;
  - 1-year service token with recorded expiry and reminder;
  - verified facts applied: registry user on stdin, package-access step, cloudflared metrics placement with a journal-first readiness check, gitleaks, Modal spend limit.
- [x] Phase 5: purge procedure updated to restic's documented `rewrite` flags, with a `--dry-run` preview.
- [x] Phase 6:
  - "warm" only after a successful ping;
  - failed pings mark the backend unhealthy and feed the breaker;
  - a "running" label for containers busy outside a warm window;
  - runbook entry extended.
- [x] Phase 8: verified uv script lock and plugin marketplace commands; token lifetime and reminder in the docs.

#### Impact on Phases
- **Phase 1 (3h, was 2h):** the Modal script gets the ComfyUI probe, loses the web endpoint and `fastapi[standard]`, and the README loses its HTTP section. The diff-based success criterion now expects exactly these changes.
- **Phase 2:** settings defaults (40 GB, 5 GB) and the three presets are the validated values. No code change.
- **Phase 4:**
  - the proxy-auth revocation follows the endpoint removal;
  - the Access token is created for 1 year with a recorded expiry;
  - `deploy.sh` reads the registry user from stdin;
  - the tunnel replica passes on its journal registration.
- **Phase 5:** the purge runbook uses the verified `rewrite --dry-run`, then `--forget`, then `prune`.
- **Phase 6 (8h, was 7h):** the display states, the pinger and the breaker now depend on the probe's result.
- **Phase 8:** the plugin lock, marketplace file and install commands are verified; the docs record the token's lifetime and reminder.
- **Phases 3 and 7:** no change.

### Verification Results
- **Tier:** Full (8 phases). The guard applied: the Red Team Review already holds verification evidence, so this pass resolved the remaining `[UNVERIFIED]` tags only.
- **Claims checked:** 25
- **Verified:** 15 | **Failed:** 0 | **Unverified:** 10
- **Failures:** none.

#### Verified
1. `uv lock --script` writes `<script>.lock`. Evidence: a hermetic run with uv 0.9.26.
2. `uv run --locked --script` runs when the lock matches. Evidence: the same run.
3. `uv run --locked --script` refuses after an unrelocked metadata change. Evidence: the same run ("needs to be updated, but `--locked` was provided").
4. `uv run --frozen --script` runs without the check. Evidence: the same run.
5. `claude plugin marketplace add` takes a URL, path or GitHub repo. Evidence: Claude Code 2.1.282 `--help`.
6. `claude plugin validate` checks a marketplace directory. Evidence: `--help` and the marketplace docs.
7. `.claude-plugin/marketplace.json` needs `name`, `owner` and `plugins`, and each entry needs `name` and a relative `source` without `..`. Evidence: code.claude.com marketplace docs.
8. restic `rewrite` supports the `--exclude` family, `--forget` drops the original snapshots, and `prune` must follow. Evidence: restic stable docs.
9. `GITHUB_TOKEN` can pull a private package only if the repository is granted read access. Evidence: GitHub Container registry docs.
10. `cloudflared tunnel --metrics <addr> run` is the flag placement, and the default metrics range is 20241–20245. Evidence: Cloudflare metrics docs.
11. `ghcr.io/gitleaks/gitleaks` is official, and `git` and `--redact` are current v8 options. Evidence: gitleaks README.
12. Modal's workspace spend limit is set on the "Usage & Billing" page by Owners and Managers, and stops billable workloads at the limit. Evidence: modal.com budgets docs.
13. `fastapi` is used only by the legacy endpoint. Evidence: `qwen21_uc_app.py:53` and `:152-157`.
14. `requests` is still needed. Evidence: `qwen21_uc_app.py:104-137`.
15. `/system_stats` is already ComfyUI's readiness endpoint in `start()`. Evidence: `qwen21_uc_app.py:111-117`.

#### Unverified (each has a fallback in its phase)
1. **GHCR login username:** the docs don't say whether any username is accepted, so the design now sends the actor's name on stdin, the documented pattern.
2. **cloudflared `/ready`:** documented only by third-party sources; the journal's "Registered tunnel connection" check is primary.
3. **Modal budget alerts:** not in the budget docs; the spend limit is the documented control.
4. **`systemd-run --wait --collect --pipe` on Ubuntu 26.04:** server-side, and the man page fetch returned 403.
5. **`cloudflared tunnel ingress validate` and `rule` syntax:** confirmed with `--help` on the server.
6. **The rrsync-relative `remote_path` for LearnFlow:** server-side.
7. **The Tailscale SSH status command:** server-side.
8. **ComfyUI's API-export menu label:** cosmetic.
9. **Modal image-layer reuse on redeploy:** costs time only.
10. **Action commit SHAs:** looked up at implementation, by design.

### Whole-Plan Consistency Sweep
- Files reread: plan.md and phase-01 through phase-08 (9 files).
- Decision deltas checked: 13. Five are the answers: V1a (ping probe), V1b (endpoint and `fastapi` removal), V2, V3 and V4. Eight come from the verification pass:
  - the registry user on stdin;
  - the package-access step;
  - the cloudflared metrics placement with a journal-first readiness check;
  - gitleaks;
  - the Modal spend limit;
  - the restic `rewrite` flags;
  - the uv script lock;
  - the plugin marketplace.
- Stale-term greps: 20 terms. They include "no-op", "never touches ComfyUI", "contracted change", `fastapi`, "stays deployed", "stays until", "pending the validation", "validation interview", "settled in the validation", "pending question", "open question" and "pong". They also cover "budget alerts", the fixed registry username, the old "warm if runners" rule, alternative preset sizes, the cap and floor variables, the token duration, and every `[UNVERIFIED]` tag.
- Reconciled stale references: 2. Phase 4's Key Insight and phase 6's Security section still promised "budget alerts"; they now state the documented spend-limit behavior. Every remaining `[UNVERIFIED]` tag matches an item in Verification Results.
- Unresolved contradictions: 0. The plan and the amended contract (amendments 8–9) agree.

## Unresolved questions

<!-- Updated: Validation Session 1 - settled questions removed -->

Resolved so far: the Modal workspace (D2), and in Validation Session 1 the size presets, the storage defaults, the service-token lifetime and the Modal script extras.

1. **GHCR access:** can the repo's `GITHUB_TOKEN` pull the new private package on the first deploy? GitHub's docs require the repository to have read access, and granting it is a pre-decided owner step (phase 4, step 18).
2. **cloudflared:** the exact `tunnel ingress validate` and `rule` syntax, and the `/ready` endpoint, are confirmed on the server at execution time and are [UNVERIFIED] until then. The journal check is the primary pass signal.
3. **Other server-side checks** are [UNVERIFIED] until execution, each with its fallback: `systemd-run --wait --collect --pipe` on Ubuntu 26.04, the rrsync-relative `remote_path` for LearnFlow, and the Tailscale SSH status command.
4. **Modal usage alerts:** the spend limit is verified, but whether Modal also offers budget alerts is not in its docs ([UNVERIFIED]).
5. **Minor, non-blocking [UNVERIFIED] items:** ComfyUI's API-export menu label (phase 7 hint text), Modal image-layer reuse on redeploy (costs time only), and the action commit SHAs (looked up at implementation).

<!-- slug: atelier-image-studio -->
