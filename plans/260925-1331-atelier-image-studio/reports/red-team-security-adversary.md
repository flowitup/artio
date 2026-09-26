# Red team: Security Adversary + Fact Checker

Date: 2026-09-25. Target: `plan.md` and phases 1–8 of `plans/260925-1331-atelier-image-studio`.
Threat model used: a single-owner app on Folio's shared `folio-prod-1`, behind Cloudflare Tunnel and Access. It generates uncensored images, so any leak of pixels or prompts is serious. A GitHub Actions deploy key has root forced-command access, and a local Claude plugin holds an Access service token.
Method: I read every phase file, the contract, the brief, the scouts and the research. I grep-verified the claims against the Modal 1.5.5 SDK source, PyJWT 2.14.0/2.15.0 and mcp 1.30.0 in the uv cache, the Folio and LearnFlow repos, local man pages, and the fetched Cloudflare and Claude Code docs. I also ran one offline runtime check against the installed Modal SDK.
I made no SSH connection, no state-changing API call and no `.env` read, and I did not open `logs/token_new.log`.

---

## Finding 1: The co-tenants' root-equivalent credentials make Atelier's isolation moot, and the plan never lists them
- **Severity:** Critical
- **Location:** Phase 4, sections "Key Insights" and "Security Considerations". Phase 5, section "Security Considerations".
- **Flaw:** The plan's only trust-boundary analysis covers Atelier's own deploy key. It concludes "The GitHub secret therefore cannot open a root shell" and "Folio is untouched". It never asks who **else** already holds root on the host that will store every image.
  - The scout found no non-root login users, and the operator logs in as root. Tailscale is also running.
  - LearnFlow deploys to the same host from GitHub Actions. Its SSH private key goes to a third-party action pinned to a mutable tag. In the same job, `npm ci` first installs 385 packages, which can run install scripts.
  - Nothing shows that this key is restricted (no `rrsync` or forced command), so it is most likely an unrestricted root key.
- **What that key exposes:** anyone with root on the host can read everything Atelier protects:
  - the images, on a plain `mkfs.ext4` volume;
  - `/opt/atelier/.env`, which holds the Modal token;
  - `/opt/atelier/backup.env`, which holds `RESTIC_PASSWORD` and an R2 read/write key. That is enough to decrypt every snapshot, including deleted images, and, with bucket locks off, to destroy them.
  - By contrast, Folio's own backups deliberately use an append-only identity. The plan drops that pattern.
- **Failure scenario:**
  1. A compromised LearnFlow npm dependency (a worm in the style of Shai-Hulud) or a repointed `burnett01/rsync-deployments@v9` tag (the tj-actions/changed-files precedent, CVE-2025-30066) captures `SSH_PRIVATE_KEY`.
  2. The attacker runs `ssh root@folio-prod-1 'tar c /mnt/atelier-data /opt/atelier/.env /opt/atelier/backup.env'` and gets every uncensored PNG, the prompt database, the Modal token and the restic password.
  3. The attacker then runs `restic forget --prune` to wipe the R2 history. No Atelier control notices any of this.
- **Evidence:**
  - LearnFlow's workflow: `learnflow/.github/workflows/deploy.yml:16` (`npm ci`), `:19` (`uses: burnett01/rsync-deployments@v9`) and `:25-26` (`remote_user`/`remote_key` from secrets).
  - `learnflow/package-lock.json` has 385 packages.
  - `scout-02-folio-prod-1-live-state.md:11-12` ("Tailscale is running", "no `deploy`, `folio` or `learnflow` users. The operator alias logs in as root").
  - `brainstorm…hetzner.md:106-107,121` (LearnFlow deploys to this host).
  - `phase-04-container-ci-cd-and-rollout.md:44,480,485`, the isolation claims.
  - `phase-04…:321`, a plain ext4 volume with no at-rest encryption.
  - `phase-05-backups-and-restore.md:34,210,216,300`: locks off, an Object Read & Write token, and the password in the same file on this host.
  - Folio's append-only backup identity: `folio/scripts/backup/pg-dump.sh:3,30,56`.
  - The server-side `authorized_keys` options are [UNVERIFIED], because SSH was forbidden for this review.
- **Suggested fix:** This needs an owner decision, because the contract chose this host and makes LearnFlow changes a non-goal.
  - Add a read-only pre-flight step before step 8 of phase 4. It lists every root `authorized_keys` entry with its options and fingerprint, checks the Tailscale SSH or ACL status, and maps each key to its holder.
  - The owner then picks one:
    - (a) restrict LearnFlow's key to `restrict,command="rrsync -wo /var/www/learnflow"` and SHA-pin its action;
    - (b) host Atelier on its own small VM;
    - (c) accept the risk explicitly in the contract.
  - Whatever the choice, remove the isolation claims from the Security Considerations.

## Finding 2: The deploy path runs any image that anyone with `packages: write` can tag, and "a compromised pipeline can only push an image" misses that the image holds all the data
- **Severity:** High
- **Location:** Phase 4, sections "Key Insights", "Architecture" (`deploy.sh` and the deploy job), "Implementation Steps" 4 and 18.
- **Flaw:** Five gaps:
  - (1) `deploy.sh` pulls a **mutable tag** (`$IMAGE:$sha`). It checks no digest and no provenance.
  - (2) `workflow_dispatch` with `inputs.sha` skips the build and deploys whatever tag exists. The rollback drill shows that a tag pushed from a laptop is enough.
  - (3) Actions are pinned by **major tag**, not by commit SHA. There is no `permissions: {}` default, no `persist-credentials: false`, and no GitHub Environment that limits the SSH key or the Modal secret to `main`. Folio's reference workflow does all of these and explains why. Also, a `GITHUB_TOKEN` with `actions: write` can itself trigger `workflow_dispatch`.
  - (4) The "confined" container still gets `/mnt/atelier-data` read/write, `MODAL_TOKEN_SECRET`, and unrestricted egress, including Hetzner's metadata service at `169.254.169.254/hetzner/v1/userdata` [UNVERIFIED on this host].
  - (5) `read_only: true` does not cover volumes an image declares, and `deploy.sh` checks neither image size nor declared volumes. A bad image can therefore fill the root disk that Folio's Postgres and MinIO use (21 GB free). That breaks "Any single step can be rolled back without touching Folio".
- **Failure scenario:**
  - A repointed `docker/build-push-action@vN` tag in the build job (`packages: write`) pushes a trojan under `:<sha>`.
  - `deploy.sh` pulls it, and `/healthz` reports the right version, so the health gate passes.
  - At start-up the trojan streams `/data/images/**` and the Modal token out through the default bridge.
  - Variant: a dev dependency that runs inside `pytest` reads the `GITHUB_TOKEN` persisted in `.git/config` by `actions/checkout`. If the repo's default token is read/write, it pushes an image and dispatches `deploy.yml` with `sha=<its tag>`.
- **Evidence:**
  - The plan: `phase-04…:44` (the claim), `:87`, `:113-118`, `:127`, `:137`, `:190` (pull by tag), `:220`, `:226` (`inputs.sha || github.sha`), `:240` (build skipped on `inputs.sha`), `:245` ("Pin every action to its current major version"), `:284`, `:388-391` (the drill deploys a hand-pushed FAKE tag).
  - A grep of the plan finds no `persist-credentials`, `environment:` (on a job), digest, attestation or cosign.
  - Folio's reference: `folio/.github/workflows/deploy-backend.yml:58-62` (the rationale), `:87-91` (SHA-pinned checkout, `persist-credentials: false`) and `:149` (SHA-pinned `docker/build-push-action`).
  - `scout-02…:8-9`: 21 GB free on `/`, and 45.6 GB of images already there.
- **Suggested fix:**
  - Pin actions by commit SHA. Set `permissions: {}` at the top level and grant the minimum per job. Use `persist-credentials: false`.
  - Move the SSH key and the Modal CI token into a `production` environment restricted to `main`.
  - Have the build job output the digest, and change the forced-command grammar to `deploy <sha> <sha256:…> <actor>` so that `deploy.sh` pulls `IMAGE@sha256:…`.
  - Allow redeploys only to images already on the server (`current-tag`/`previous-tag`), or to a digest produced by a `main` run.
  - Make `deploy.sh` reject images whose `Config.Volumes` is not empty or that exceed a size cap.
  - With the owner's go-ahead, block egress to `169.254.169.254` (and to the tailnet range) from the `atelier` bridge.

## Finding 3: Third-party code that sees the plaintext images or the secrets is fetched by mutable reference
- **Severity:** High
- **Location:** Phase 5, section "Architecture" (`atelier-backup-common.sh`). Phase 8, section "Architecture" (the PEP 723 header). Phase 4, section "Architecture" (`Dockerfile`).
- **Flaw:**
  - **restic.** The script says `RESTIC_IMAGE=restic/restic:<pinned version>` with the comment "pin by tag, and record the digest in the guide". Recording a digest in a document does not enforce it. Every night that container receives `RESTIC_PASSWORD`, the R2 read/write keys, read access to every image, and the network.
  - **Plugin.** It runs `uv run --script` with only `>=` ranges and no lockfile, in a process whose environment holds `ATELIER_CF_CLIENT_SECRET`. The installed uv can lock scripts (`uv lock --script`), but the plan doesn't use it.
  - **Base images.** `python:3.12-slim` and the uv image are pulled by tag.
- **Failure scenario:**
  - A hijacked or mutated `restic/restic` tag exfiltrates all images plus the restic password at 04:30, and the plan has no detection.
  - For the plugin, a malicious release of any transitive dependency of `mcp[cli]` is resolved on the next cache miss. It reads the service-token ID and secret from `os.environ`. The attacker then pages `/api/v1/images?limit=50&before=…` and downloads every `/file` from anywhere, because service tokens aren't bound to an IP or a device.
- **Evidence:**
  - The plan: `phase-05…:100,110-113`; `phase-08-api-plugin-and-acceptance.md:103-106,118`; `phase-04…:147-148,155`.
  - uv 0.9.26 `uv lock --help` shows `--script <SCRIPT>  Lock the specified Python script` (line 10).
  - This machine's uv cache already holds diverging resolutions: mcp 1.30.0, 2.1.1 and 2.2.0, and pyjwt 2.14.0 and 2.15.0.
  - Folio's plugin uses the same unpinned pattern: `folio/folio-plugin/mcp_servers/folio_mcp/server.py:2-12`.
- **Suggested fix:**
  - Use `RESTIC_IMAGE=restic/restic@sha256:<digest>` in the script itself.
  - Ship `server.py.lock` built with `uv lock --script`, and run the plugin with `["run","--locked","--script",…]` in `.mcp.json`.
  - Pin the base images by digest.
  - Let Dependabot or Renovate propose digest bumps, so updates are reviewed rather than automatic.

## Finding 4: The service identity can use every route and skips the CSRF check everywhere; "exactly one power" is false, and nothing caps spend
- **Severity:** High
- **Location:** Phase 3, section "Architecture" (`access_guard`). Phase 8, sections "Key Insights", "Security Considerations" and "Rollback". Phase 6, section "Security Considerations".
- **Flaw:**
  - `access_guard` admits the service identity to **all** paths. `identity.kind` only decides whether the Origin check runs, and no route checks it. So the plugin token can use HTML routes the contract never granted it: `POST /images/{id}/delete`, `POST /workflows` (upload an arbitrary graph) and `POST /workflows/{id}/delete`, all without an Origin header. It can also bulk-export through the API.
  - Nothing limits requests or GPU spend per identity. `warm` can be re-posted forever, so the "$0.98 at most" claim holds per request, not in total.
  - The rollback for a leaked token says to "rotate … using the grace period". During that grace period, which Cloudflare allows to run from one hour to 30 days, the stolen secret keeps working.
- **Failure scenario:**
  1. The service secret leaks (see Finding 3).
  2. The attacker downloads every image, then deletes the originals through the HTML delete route.
  3. They keep the L40S warm around the clock (about $47 a day) by calling `POST /api/v1/gpu/qwen21-uc/warm` every 25 minutes.
  4. The owner rotates with a grace period, and the attacker keeps access until it ends.
- **Evidence:**
  - Phase 3: `phase-03-web-ui-and-access-auth.md:116-121` (identity), `:132` (only the CSRF check depends on kind) and `:65` (delete route).
  - Phase 7: `phase-07-prompt-library-and-workflows.md:66-68`.
  - Phase 6: `phase-06-gpu-status-warm-stop.md:75` and `:259` ("can cost at most the chosen window").
  - Phase 8: `phase-08…:46`, `:58-60`, `:64`, `:272`, `:276` ("exactly one power") and `:279`.
  - The contract's plugin scope, which excludes delete and upload: `brainstorm…:67`.
  - Cloudflare's service-token page: the grace period runs from one hour to 30 days.
- **Suggested fix:**
  - Authorize by route. The service identity gets only an allowlist of `/api/v1/*` endpoints (criterion 10's list plus warm and stop), and 403 everywhere else.
  - Enforce a server-side daily budget: warm-minutes per day, jobs per day, and a cumulative dollar cap shown in the header.
  - On a suspected leak, revoke immediately with no grace period. Pick a short token duration and settle open question 6 (`plan.md:101`) now.

## Finding 5: The plugin moves image content outside the controlled store: to Anthropic, into local transcripts, to arbitrary paths and into git
- **Severity:** High
- **Location:** Phase 8, sections "Key Insights", "Requirements", "Architecture" (the `generate` sketch), "Security Considerations" and "Acceptance record". Phase 1, step 3.
- **Flaw:**
  - **Inline thumbnails.** `generate` (and `get_image` and `run_workflow`) returns a 256 px WebP thumbnail by default. It goes to Anthropic's API and stays in plaintext in Claude Code transcripts. This machine keeps transcripts in `~/.claude/projects/` (101 project folders), and `~/.claude/settings.json` sets no `cleanupPeriodDays`, so the default retention applies. These copies sit outside the volume, the backups and any delete.
  - **`save_to`.** This is a model-controlled tool argument with no containment check. The default `save_dir` is `~/Downloads/atelier`.
  - **Acceptance evidence.** The Acceptance record asks for screenshots as evidence. It lives in a plan file that phase 1 commits and phase 4 pushes to GitHub, and nothing in `.gitignore` excludes images under `plans/`.
- **Failure scenario:**
  - Text injected into the same Claude session (a web page or a file) makes Claude call `get_image(id=…, save_to="<iCloud Drive or a git working tree>")`, and the PNGs sync or get pushed.
  - Thumbnails of every generated image stay in the transcript JSONL for the retention period.
  - A screenshot of an uncensored image page becomes permanent history in `flowitup/atelier`.
- **Evidence:**
  - The plan: `phase-08…:31,39-40,70-71,131-142,217,226,278` ("Saved files go only to the chosen directory" is not enforced anywhere); `phase-01-start.md:97` (commits `plans/`).
  - `research-02…:161-167`: tool images are counted as base64 in the transcript.
  - Locally: `/Users/sweet-home/.claude/projects` exists, and `grep cleanupPeriodDays ~/.claude/settings.json` finds nothing.
- **Suggested fix:** The owner decides, because brief §8 chose thumbnails.
  - Don't return inline images by default: make the thumbnail opt-in per call, or a `userConfig` toggle that defaults to false.
  - Resolve `save_to` and require `is_relative_to(save_dir)`. Default `save_dir` to a folder that isn't synced and has mode 0700.
  - Make acceptance evidence text-only, and add `plans/**/*.{png,jpg,jpeg,webp}` to `.gitignore`.
  - Document transcript retention (`cleanupPeriodDays`).

## Finding 6: The Modal tokens are workspace-wide, live in the internet-facing container and in a CI job that runs third-party code, and have no spend limit
- **Severity:** High
- **Location:** Phase 4, step 9, step 4 (`deploy-modal.yml`) and the compose `environment`. Phase 6, section "Security Considerations". `plan.md`, "Unresolved questions" 1.
- **Flaw:**
  - The default is "two dedicated personal tokens". Each has workspace-wide authority: it can deploy or stop any app, `modal container exec` into the GPU container, and start any GPU workload.
  - `deploy-modal.yml` exposes the CI token to `uv sync` and `pytest`, which is third-party code, before `modal deploy`.
  - The workflow can be dispatched from any branch, which deploys that branch's backend to production.
  - No Modal workspace budget or spend limit is set, even though Modal supports both.
  - The backend's old `api` web endpoint stays deployed. It is a second generator that bypasses Access, guarded only by workspace proxy-auth tokens, and the plan never inventories or rotates those tokens.
- **Failure scenario:**
  - Either token leaks: from the container, from host root (Finding 1), or from a malicious test dependency in CI.
  - The attacker redeploys `qwen21-uc` with a hook that copies every graph and PNG, so all future uncensored output goes to them. Atelier's parity test only checks the repo copy, so nothing flags it.
  - Alternatively, they run GPU jobs billed without any cap.
- **Evidence:**
  - The plan: `phase-04…:127,287,314,338`; `plan.md:96`; `research-02…:115-125` (Service Users exist only on team workspaces).
  - The SDK: `modal/cli/container.py:293` (`exec`) and `modal/cli/app.py:548` (`stop`).
  - The backend: `qwen21_uc_app.py:152-157` (`fastapi_endpoint(... requires_proxy_auth=True)`); `README.md:4,22-23` (workspace `yaiba2307` and the endpoint URL).
  - `phase-06…:257`: the CLI inherits the full token.
  - A grep of the plan finds no budget or spend limit.
- **Suggested fix:**
  - Answer question 1 before phase 4. Without Service Users, use a **separate Modal workspace** that holds only Atelier.
  - Set a workspace budget and spend limit, with alerts (modal.com/docs/guide/budgets).
  - Split `deploy-modal.yml` into a test job with no secrets and a deploy job (environment `production`, `main` only) that runs only `modal deploy`, with the tokens set at step level.
  - Inventory and revoke the proxy-auth tokens. Whether to remove `api` is the owner's decision, because the contract makes the Modal script off-limits.

## Finding 7: `TRANSIENT` includes `grpclib.GRPCError`, which swallows `AuthError`, `PermissionDeniedError` and `NotFoundError`, so a revoked credential or an undeployed app looks like "queued"
- **Severity:** Medium
- **Location:** Phase 2, section "Architecture" (the gateway poll mapping and the spawn paragraph).
- **Flaw:** In modal 1.5.5, `AuthError`, `PermissionDeniedError`, `NotFoundError`, `InvalidError` and others subclass `_GRPCErrorWrapper(grpclib.GRPCError)`. With the plan's tuple, `except TRANSIENT` therefore catches them:
  - `spawn` backs off and retries forever, and the job stays queued;
  - `poll` returns "pending" until the 1800 s timeout.

  The plan's own statement that "`NotFoundError` when the app is not deployed marks the job failed" is false. Criterion 4 ("errors shown") also fails for these cases.
- **Failure scenario:** During incident response the owner revokes a leaked Modal token. Every job silently stays "queued" or "running", with no error or banner. The owner can't tell whether the revocation took effect or whether the backend is misconfigured, and submitted jobs later fail with a misleading "last transient error".
- **Evidence:**
  - The plan: `phase-02-engine.md:179-180,192-193,199,216-217`.
  - The SDK: `modal/exception.py:66` (`class _GRPCErrorWrapper(grpclib.GRPCError)`), `:141` AuthError, `:161` NotFoundError, `:165` PermissionDeniedError, and the mapping at `:22,24,33`.
  - A runtime check against the installed SDK: `isinstance(AuthError('x'), TRANSIENT)` is True, as are PermissionDeniedError, NotFoundError, InvalidError and ResourceExhaustedError.
- **Suggested fix:**
  - Catch `(AuthError, PermissionDeniedError, NotFoundError, InvalidError)` first. Fail the job and raise a "Modal credentials or deployment" banner.
  - Narrow `TRANSIENT` to `modal.exception.ConnectionError`, `modal.exception.ServiceError` and `grpclib.exceptions.StreamTerminatedError`.
  - Test with a real instance of each class.

## Finding 8: Delete doesn't really delete: prompts, call IDs and pixels survive in `jobs`, on Modal, in R2 and in the plugin's copies
- **Severity:** Medium
- **Location:** Phase 3, "Requirements" (Image delete) and "Implementation Steps" 6. Phase 5, "Architecture" (retention). Phase 2, "Architecture" (schema).
- **Flaw:**
  - `delete_image` removes only the `images` row and its files. The `jobs` row keeps `params_json` (prompt, negative, seed), `graph_json` and `call_id`, and stays visible in the queue and in `/api/v1/jobs`.
  - Modal keeps the output for 7 days, retrievable by that stored `call_id` with any workspace token.
  - R2 keeps the PNG for up to about 6 months (`--keep-monthly 6`). There is no purge procedure (`restic rewrite --exclude … --forget`, then `prune`).
  - Plugin copies and transcript thumbnails (Finding 5) are untouched.
  - The brief admits the backup retention, but the UI and the runbook say nothing.
- **Failure scenario:** The owner deletes a sensitive image and believes it is gone. The prompt stays visible in the live queue and API. A later host compromise (Finding 1), or a restore after an incident, brings the pixels back.
- **Evidence:**
  - Phase 3: `phase-03…:69,218`.
  - Phase 2: `phase-02…:140-147` (the `jobs` columns) and `:150` (`images.job_id` has no cascade to jobs); `:187` ("results are kept 7 days").
  - Phase 5: `phase-05…:85,129`.
  - Research and brief: `research-02…:26-28`; `architecture-brief.md:69`.
- **Suggested fix:**
  - On delete, clear the job's prompt, graph and `call_id` as well, or delete the job row.
  - State the retention in the `hx-confirm` text.
  - Add a "purge from backups" runbook using `restic rewrite --exclude <path> --forget` followed by `prune`.

## Finding 9: The key and token hygiene steps are wrong: `rm -P` does nothing on this Mac, and the drill sends the owner's master GitHub token to a root script on the shared host
- **Severity:** Medium
- **Location:** Phase 4, "Implementation Steps" 13–14 and 18.
- **Flaw:**
  - **Deploy key.** It is generated without a passphrase (`-N ''`), and the plan then relies on `rm -P "$d/atelier_deploy"` so that "GitHub holds the only copy". On this machine (macOS 27.0), `man rm` says `-P` "has no effect". The root-forced-command key therefore stays recoverable on the laptop's disk, and in any APFS or Time Machine snapshot.
  - **Drill token.** Step 18 pipes `gh auth token` into `/opt/atelier/deploy.sh` as root on folio-prod-1. That is the owner's long-lived OAuth token, whose scopes reach every flowitup repository (Folio and LearnFlow included), plus `write:packages`. `docker login` writes it to `$cfg/config.json` for the duration of the pull.
  - **Laptop capability.** The drill also relies on a laptop token that can push GHCR tags. That is exactly the capability Finding 2 shows can deploy arbitrary code.
- **Failure scenario:**
  - Any root-equivalent on the host (Finding 1) watching `/tmp` captures the owner's master GitHub token during the drill, and with it gets write access to every flowitup repository.
  - Separately, laptop malware, or a lost Time Machine disk, gives up a working deploy key.
- **Evidence:**
  - The plan: `phase-04…:341,348,389-391`, and `:188` (`docker login` writes its config).
  - `man rm` (macOS 27.0, 26A428) at lines 36–37: "-P This flag has no effect. It is kept only for backwards compatibility with 4.4BSD-Lite2."
- **Suggested fix:**
  - Drop the "only copy" claim. Either store the key deliberately in the password manager, or generate it inside a throwaway CI job that writes the secret straight to GitHub.
  - Run the drill through CI: a job with `packages: write` retags the image (`docker buildx imagetools create`), then `workflow_dispatch` with `sha=<FAKE>`. Only the ephemeral `GITHUB_TOKEN` then reaches the server, and `gh auth token` is never used.

---

## Fact-check results

`VERIFIED (file:line)` | `FAILED (reason)` | `UNVERIFIED (reason)`. In these citations, "SDK" means `~/.local/share/uv/tools/modal/lib/python3.12/site-packages/modal`, "jwt" means PyJWT 2.14.0 in `~/.cache/uv/archive-v0/LYDdaXmDx3788p1ClTMrp/jwt`, and "mcp" means mcp 1.30.0 in `~/.cache/uv/archive-v0/oU4kYrXC3ewiBgu53BgD8/mcp`.

**Phase 1**
1. `build_workflow` at :85-96: VERIFIED (qwen21_uc_app.py:85-96)
2. Class config `timeout=1800`, `max_containers=1`, `max_inputs=4`, `scaledown_window=60`: VERIFIED (qwen21_uc_app.py:99-100)
3. `run_workflow` at :147-150: VERIFIED (qwen21_uc_app.py:147-150)
4. The smoke test writes to `Path(__file__).parent / "out"`: VERIFIED (test_deployed.py:9)
5. The one-line `import time, modal`: VERIFIED (test_deployed.py:1)
6. The README command block, with deploy on line 8 and the smoke test on line 10: VERIFIED (README.md:7-13)
7. `@modal.enter()` runs once per container: VERIFIED (SDK/_partial_function.py:589)
8. `modal/` without `__init__.py` can't shadow the installed package: VERIFIED (CPython stdlib importlib/_bootstrap_external.py:1376-1403, namespace portions are used only when no regular package is found)
9. Token prefixes `ak-`/`as-` (the plan marks this [UNVERIFIED]): VERIFIED (SDK/config.py:25-26)
10. `modal>=1.5.5,<1.6` resolves: VERIFIED (site-packages/modal-1.5.5.dist-info)
11. `modal app list --json` gives `state`: VERIFIED (SDK/cli/app.py:99-133)
12. `modal container list --json`: VERIFIED (SDK/cli/container.py:40-77)
13. The deploy uses `~/.modal.toml`: VERIFIED (SDK/config.py:120)
14. The cold ping costs about $0.07: VERIFIED (128 s × $0.000542, research-02:129)
15. Step 2's `git check-ignore -q logs/ out/` proves both paths are ignored: FAILED (git-check-ignore(1) returns 0 when *one or more* paths are ignored; the Verification block's two separate calls are correct)

**Phase 2**
1. `TimeoutError` at :189: VERIFIED (SDK/exception.py:189)
2. `FunctionTimeoutError(TimeoutError)`: VERIFIED (SDK/exception.py:209)
3. `OutputExpiredError(TimeoutError)`: VERIFIED (SDK/exception.py:229)
4. `poll_function` raises expired or pending: VERIFIED (SDK/_functions.py:319,332,334)
5. `FunctionTimeoutError` on a container timeout: VERIFIED (SDK/_utils/function_utils.py:534)
6. `from_id` does no I/O, and `.aio` is deprecated: VERIFIED (SDK/_functions.py:2271-2273)
7. `cancel(terminate_containers=False)`: VERIFIED (SDK/_functions.py:2257)
8. Transport errors become Modal's `ConnectionError`: VERIFIED (SDK/_utils/grpc_utils.py:28,458-461)
9. A `TRANSIENT` tuple with `GRPCError` matches only network faults: FAILED (SDK/exception.py:66,141,161,165, plus the runtime check)
10. "`NotFoundError` … marks the job failed": FAILED (it is caught by `TRANSIENT` first)
11. `generate()` hides the seed: VERIFIED (qwen21_uc_app.py:143-145)
12. `_run` raises `RuntimeError`, with the messages: VERIFIED (qwen21_uc_app.py:123-124,130-131)
13. `FunctionCall.object_id`: VERIFIED (SDK/_object.py:307)
14. `spawn` and `get`: VERIFIED (SDK/_functions.py:2035,2180)
15. `StreamTerminatedError` exists: VERIFIED (SDK/_utils/grpc_utils.py:25)
16. Results are kept 7 days: UNVERIFIED (a server policy; research-02:26-28 cites the docs only)
17. FTS5 is present in `python:3.12-slim`: UNVERIFIED (the image was not pulled; the plan has a fallback)

**Phase 3**
1. The `PyJWKClient` defaults `cache_jwk_set=True` and `lifespan=300`: VERIFIED (jwt/jwks_client.py:39-40); in 2.15.0 they move to :40-41, and `>=2.9` may lock 2.15
2. `fetch_data` at :146: VERIFIED (jwt/jwks_client.py:146)
3. `get_signing_key_from_jwt` at :269: VERIFIED (jwt/jwks_client.py:269)
4. `PyJWKClientError` is a `PyJWTError`, so fetch failures return 403: VERIFIED (jwt/exceptions.py:96)
5. A list `aud` is accepted: VERIFIED (jwt/api_jwt.py:552-553)
6. `RSAAlgorithm.to_jwk` exists for the fixture: VERIFIED (jwt/algorithms.py:264)
7. Refetches on unknown key IDs are rate-limited: VERIFIED (jwt/jwks_client.py:44 `cooldown_duration=30`, :258)
8. The service JWT has `common_name` and no `email`: VERIFIED (research-01:23)
9. "Access's SameSite cookie stops cross-site POSTs at the edge": FAILED as an unconditional claim (Cloudflare's authorization-cookie docs say SameSite is "Admin choice (Default: None)"; it holds only after phase 4 step 8)
10. "The service identity sends no cookies": UNVERIFIED (httpx keeps response cookies: httpx/_client.py:211,1737)
11. `uvicorn --factory`: VERIFIED (uvicorn/main.py:389)
12. A 286 response stops htmx polling: VERIFIED (research-02:183-186)
13. The 125 s proxy read timeout: VERIFIED (Cloudflare's error-524 doc, fetched 2026-09-25)
14. The header and cookie names: VERIFIED (research-01:19)
15. "Two identities only" gives least privilege: FAILED (phase-03:132 has no route authorization; see Finding 4)

**Phase 4**
1. The SHA whitelist: VERIFIED (folio/scripts/deploy/deploy-runner.sh:22-23)
2. Health polling: VERIFIED (folio/scripts/deploy/wait-healthy.sh:17-33)
3. "Don't verify through the live URL": VERIFIED (learnflow/.claude/skills/learnflow/SKILL.md:154-156)
4. `actions/checkout@v7`: VERIFIED (learnflow/.github/workflows/deploy.yml:11)
5. Modal reads only `~/.modal.toml`, or `MODAL_CONFIG_PATH`: VERIFIED (SDK/config.py:120)
6. `python -m modal` works: VERIFIED (SDK/__main__.py)
7. `up --wait-timeout`: VERIFIED locally (Compose v5.5.1 `up --help`); UNVERIFIED on the server's v2.40.3
8. `restrict` disables forwarding and PTY: VERIFIED (man sshd:414-418)
9. `rm -P` leaves GitHub with the only copy: FAILED (man rm:36-37, "no effect")
10. "A compromised pipeline can then only push an image" (implying it's harmless): FAILED (phase-04:118,127 mount the data and the token)
11. Pinning actions by major version is safe enough: FAILED against the reference (folio deploy-backend.yml:87,149 pin by commit SHA)
12. The registry token never lands on disk after the run: VERIFIED with a caveat (it is on disk during the run, phase-04:188; the EXIT trap removes it)
13. The 125 s timeout: VERIFIED (the Cloudflare doc)
14. GHCR pull with `github.token`: UNVERIFIED (research-02, unresolved)
15. `cloudflared tunnel ingress validate/rule` syntax: UNVERIFIED (the plan marks it)
16. Server facts (21 GB free, port 8090 free, Compose v2.40.3): UNVERIFIED (not re-probed; SSH was forbidden)

**Phase 5**
1. Stratified exit codes: VERIFIED (folio/scripts/backup/pg-dump.sh:8-11)
2. `logger -t`: VERIFIED (pg-dump.sh:18)
3. `verify-latest-dump.sh` exists: VERIFIED (folio/scripts/backup/verify-latest-dump.sh)
4. Folio's backup identity is append-only: VERIFIED (pg-dump.sh:3,30,56); the plan doesn't carry it over
5. restic `--hostname` through `docker --hostname`: VERIFIED (research-01:94)
6. `check --read-data-subset=5%`: VERIFIED (research-01:106)
7. `unlock` removes only stale locks, and a lock failure exits 11: VERIFIED (research-01:108)
8. `Connection.backup()` is WAL-safe: VERIFIED (research-01:122)
9. A bucket-scoped Object Read & Write token: VERIFIED (research-01:110)
10. Bucket locks are incompatible with prune: VERIFIED (research-01:112)
11. The `restic ls --json` node format: UNVERIFIED (the plan marks it)
12. "Pin by tag, record the digest" pins the restic image: FAILED (phase-05:100,112 pull by tag)
13. `docker exec` runs as uid 10001: VERIFIED (phase-04:112 `user: "10001:10001"`)
14. Secrets live "only in backup.env and the password manager": VERIFIED as designed, but the file sits on a shared host with root access (Finding 1)
15. `OnCalendar=… UTC` syntax: UNVERIFIED (not runnable on macOS; the plan has a `systemd-analyze calendar` step)

**Phase 6**
1. Bound methods share the class's service function: VERIFIED (SDK/cls.py:90)
2. `get_current_stats` returns `FunctionStats`: VERIFIED (SDK/_functions.py:2080; SDK/types.py:135-141)
3. The app state texts: VERIFIED (SDK/cli/app.py:41-50)
4. `app list --json`: VERIFIED (SDK/cli/app.py:99-133)
5. The JSON keys are snake_case: VERIFIED (SDK/cli/utils.py:132)
6. `container list --app-id --json`: VERIFIED (SDK/cli/container.py:40-77)
7. `container stop` is non-graceful by default: VERIFIED (SDK/cli/container.py:308-338)
8. Without `--yes` on a non-TTY stdin, the command aborts: VERIFIED (SDK/cli/utils.py:189-199)
9. A deploy resets the autoscaler: VERIFIED (SDK/_functions.py:1262)
10. `modal app stop` exists: VERIFIED (SDK/cli/app.py:548)
11. `terminate_containers=True` reschedules sibling inputs: VERIFIED (SDK/_functions.py:2257-2266)
12. The CLI reads `MODAL_TOKEN_*` from the environment: VERIFIED (SDK/config.py:9)
13. "Can cost at most … $0.98": FAILED (repeated warm POSTs extend the window without limit; phase-06:75,259)
14. Each CLI call takes about 1 s of CPU and about 150 MB: UNVERIFIED
15. `input_headroom` semantics: UNVERIFIED (the plan marks it)

**Phase 7**
1. Only the first image is returned: VERIFIED (qwen21_uc_app.py:135-138)
2. `ComfyUI rejected workflow:`: VERIFIED (qwen21_uc_app.py:123-124)
3. The error messages: VERIFIED (qwen21_uc_app.py:130-131)
4. `run_workflow`: VERIFIED (qwen21_uc_app.py:147-150)
5. The `KSampler` input is `seed`: VERIFIED (qwen21_uc_app.py:92-93)
6. `KSamplerAdvanced` and `RandomNoise` use `noise_seed`: UNVERIFIED (no ComfyUI source on this machine)
7. The CHECK allows a NULL model only for workflows: VERIFIED (phase-02:147)
8. `workflow_id ON DELETE SET NULL`: VERIFIED (phase-02:143)
9. The tag FTS triggers: VERIFIED (phase-02:166-171)
10. The `python-multipart` dependency: VERIFIED (phase-01:41)
11. "Size-capped before parsing": FAILED in substance (`len(raw)` at phase-07:95 runs after the multipart body has been received and spooled; the plan sets no request-body limit)
12. The graph runs in a container with no Atelier secrets: VERIFIED (qwen21_uc_app.py:99, no `secrets=`)
13. "The app remains owner-only behind Access": FAILED (the service identity can call `POST /workflows`: phase-03:132, phase-07:66)
14. FTS5 quoting neutralizes operators: VERIFIED (a local sqlite 3.54.0 run of the plan's `fts_query` on `a" OR -b NEAR(`, `"`, `*` and `-` raised no error)
15. The "Workflow → Export (API)" label: UNVERIFIED (the plan marks it)

**Phase 8**
1. The FastMCP exports: VERIFIED (mcp/server/fastmcp/__init__.py)
2. `Image(format="webp")` has MIME type `image/webp`: VERIFIED (mcp/server/fastmcp/utilities/types.py:28-31)
3. List results are flattened: VERIFIED (mcp/server/fastmcp/utilities/func_metadata.py:526-551)
4. `report_progress`: VERIFIED (mcp/server/fastmcp/server.py:1197)
5. `call_tool`: VERIFIED (mcp/server/fastmcp/server.py:359)
6. `tool()` returns the function unchanged: VERIFIED (mcp/server/fastmcp/server.py:520)
7. Folio's `plugin.json` fields: VERIFIED (folio/folio-plugin/.claude-plugin/plugin.json)
8. Folio's `.mcp.json` uses `uv run --script`: VERIFIED (folio/folio-plugin/.mcp.json)
9. Folio's PEP 723 header pins `mcp[cli]>=1.2,<2`: VERIFIED (folio_mcp/server.py:2-12)
10. A `sensitive` value goes to secure storage: VERIFIED (code.claude.com plugins reference, "Where values are stored")
11. `${user_config.KEY}` is substituted in the MCP `env`: VERIFIED (the same reference, "Reference a saved value")
12. The validator may reject a `type: directory` default (a risk row): FAILED (the reference lists `directory` as a valid type)
13. "The service token has exactly one power": FAILED (Finding 4)
14. "Saved files go only to the chosen directory": FAILED (`save_to` is a tool argument, phase-08:70, with no containment check)
15. uv can lock PEP 723 scripts, but the plan doesn't: VERIFIED (uv 0.9.26 `uv lock --help`: `--script`)
16. httpx doesn't follow redirects by default: VERIFIED (httpx/_client.py:197)

## Unresolved questions
1. Which user and options does LearnFlow's `SSH_PRIVATE_KEY` have in root's `authorized_keys` on folio-prod-1? Is Tailscale SSH enabled? Either needs a read-only check by the owner.
2. Is the Modal workspace `yaiba2307` personal? That decides whether a separate workspace is the only way to scope the tokens.
3. Does Hetzner cloud-init user data on folio-prod-1 hold any secret? It is reachable from containers through the metadata service.
4. Will the owner accept inline thumbnails reaching Anthropic, or should they be opt-in? This reopens a brief-level decision.
