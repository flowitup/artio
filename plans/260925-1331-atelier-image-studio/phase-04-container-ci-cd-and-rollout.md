---
phase: 4
title: "Container, CI/CD & first production rollout"
status: pending
priority: P1
effort: "12h"
dependencies: [3]
---

# Phase 4: Container, CI/CD & first production rollout

## Context Links

<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F7 tunnel cutover -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->

- Contract: shared-server constraints, secrets, and criteria 1, 2, 12, 13 and 14. See the [brainstorm report](../reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md).
- Brief §6 (compose, server layout, deploy identity, CI, tunnel runbook): [architecture brief](./reports/architecture-brief.md)
- Live server state (21 GB free on `/`, port 8090 free, live ingress list, root-only operator access, Tailscale running): [scout-02](./reports/scout-02-folio-prod-1-live-state.md)
- Red-team evidence: [security](./reports/red-team-security-adversary.md) (Findings 1, 2, 3, 6, 9), [failure modes](./reports/red-team-failure-mode-analyst.md) (Findings 5, 6), [assumptions](./reports/red-team-assumption-destroyer.md) (Finding 7, lifetime items 10–12), [scope](./reports/red-team-scope-complexity-critic.md) (Finding 7).
- Reused Folio and LearnFlow patterns, re-verified:
  - `folio/scripts/deploy/deploy-runner.sh:22-23` (SHA whitelist) and `wait-healthy.sh:17-33` (health polling).
  - `folio/.github/workflows/deploy-backend.yml:58-62`, `:87-91` and `:149`: SHA-pinned actions and `persist-credentials: false`, the reference for this phase's supply-chain rules.
  - `learnflow/.claude/skills/learnflow/SKILL.md:153-157`: never verify through the live URL, because Access returns a 302.
  - `learnflow/.github/workflows/deploy.yml:11`, `:16`, `:19` and `:25-26`: `actions/checkout@v7`, `npm ci`, `burnett01/rsync-deployments@v9` by mutable tag, and the SSH key passed to it.
- Tunnel replica cutover, the Access-before-DNS order and the 125 s proxy timeout: [research-01 §3–4](./research/researcher-01-cloudflare-access-tunnel-restic-r2.md)
- GHCR pull with the job's short-lived `GITHUB_TOKEN`: [research-02 §8](./research/researcher-02-modal-sdk-app-plugin.md)
- Modal CLI inside the container: `modal/config.py:120` only reads `~/.modal.toml`, which is written solely by `modal token set`, so env tokens work on a read-only root filesystem. `modal/__main__.py` makes `python -m modal` available.

## Overview

When this phase is done, pushing to `main` does four things:
1. It tests the code.
2. It builds `ghcr.io/flowitup/atelier` and records the image digest.
3. It deploys that exact digest to `folio-prod-1` through a forced-command SSH key, after checking the image's provenance.
4. It passes a health check on the server (version plus live worker loops), rolling back automatically if the check fails.

Changes under `modal/` also run `modal deploy`, from a job that holds the Modal token only for that one step. Atelier runs confined:
- non-root, read-only root filesystem, no capabilities, bound to 127.0.0.1:8090;
- on its own bridge subnet, with egress to cloud metadata and the tailnet blocked;
- data on a dedicated 50 GB Hetzner Volume at `/mnt/atelier-data`;
- published at `atelier.flowitup.com` through the existing tunnel behind Cloudflare Access.

Before any Atelier data lands on the host, the owner audits every root SSH key and restricts LearnFlow's deploy key (decision D1). Folio, cdn and LearnFlow keep serving throughout. Priority P1. Almost every step here is **[OWNER-GATED]**.

## Key Insights

<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F7 tunnel cutover -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F12 Modal tokens -->
<!-- Updated: Validation Session 1 - verified gitleaks, cloudflared metrics and Modal spend limit -->
<!-- Updated: Validation Session 1 - proxy-auth tokens revoked after endpoint removal -->

- **Host root is outside Atelier's control, so shrink who holds it.**
  - Everything Atelier protects sits on a host where any root-capable credential can read it: the images, `.env` with the Modal token, and `backup.env` with the restic password.
  - LearnFlow deploys to this host from GitHub Actions, through a third-party action pinned to a mutable tag, after `npm ci`. Its key is most likely unrestricted root.
  - So the first server steps are a read-only audit of every root key and of Tailscale SSH, then restricting LearnFlow's key to `rrsync` (D1). Any other unrestricted CI key goes to the owner as a decision.
- **CI must never be able to write root-run config.** `compose.yaml` and `deploy.sh` are installed on the server by the owner and are never shipped by CI or inside the image. If the image carried its own compose file, a leaked secret could mount `/` and take over Folio's server.
- **The image itself still sees the data, so its provenance is checked.**
  - A malicious image would run with Atelier's data and the runtime Modal token. So `deploy.sh` accepts only the digest that the `main` build produced, pulled as `ghcr.io/flowitup/atelier@sha256:…`.
  - The image's `org.opencontainers.image.revision` label must equal the SHA, it may declare no volumes, and it must fit under a size cap.
  - Actions are pinned by commit SHA, base images by digest, and deploy secrets live in a `production` environment that only `main` can use.
- **The GHCR login uses a throwaway Docker config.** `docker --config "$(mktemp -d)"` keeps it isolated. Root's `~/.docker/config.json`, which Folio's Artifact Registry pulls may rely on, is never read or written. `compose up` then uses the local image, with `pull_policy: never`.
- **Health means the engine runs, not just the web server.** `deploy.sh` accepts a release only when `/healthz` reports the requested version **and** `"loops":"ok"` (both worker loops ticked in the last 30 s). A release whose job loop is broken therefore rolls back automatically.
- **A dropped SSH session can't interrupt a deploy half-way.** The forced command validates and pulls, then runs the change-and-verify step in a transient systemd unit (`systemd-run --wait --collect --pipe`). That step ignores SIGPIPE and logs to journald, so its rollback still runs if CI disconnects.
- **There is one documented manual path.** The owner runs `deploy.sh rollback | stop | start | status` from a root shell. `rollback` swaps `current-tag` and `previous-tag`, and every manual compose action goes through these subcommands, so the tag files never drift from what runs. The CI key can only run `deploy`.
- **Only Atelier's own images are pruned.** Never run `docker image prune`, `docker system prune` or any other global prune on this host; that would delete Folio's images.
- **The volume must never silently fall back to `/`.** It is mounted by UUID with `nofail`, so a detached volume can't block Folio's boot. But an empty `/mnt/atelier-data` directory would then sit on `/`. The app therefore refuses to start in production without the sentinel `/mnt/atelier-data/.atelier-volume`, which exists only on the volume.
- **The tunnel change is a replica cutover, applied in separately approved sub-steps.**
  - This is a deliberate, research-backed deviation from brief §6's plain restart. Cloudflare advises replicas for config changes, and a restart's drop window is undocumented (research-01 §3).
  - Each sub-step has its own pass/fail check, and the health check reads only the restarted process's journal (`--since "$T_RESTART"`). The replica is stopped only after the external checks match their baselines.
  - The Access application must exist before the DNS route, so the hostname is never public without Access.
- **Modal tokens are workspace-wide (D2).** The workspace has no Service Users, so the runtime token and the CI token can each deploy, stop or exec into any app. A workspace spend limit bounds the cost of a leak; per Modal's docs it stops billable workloads when reached. The legacy web endpoint's proxy-auth tokens are revoked once phase 1 has removed it.
- **Verification never goes through the live URL.** Deploy success is checked through the job result and the server-side health check, because Access answers the live URL with a 302 (LearnFlow `SKILL.md:153-157`).
- **Proxy timeout is not a problem.** Cloudflare's documented proxy read timeout is 125 s. Every Atelier request returns quickly: a job POST only inserts rows, and the queue polls every 2 s.

## Requirements

<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F12 Modal tokens -->
<!-- Updated: Validation Session 1 - registry user on stdin -->
<!-- Updated: Validation Session 1 - service token 1 year with reminder -->
<!-- Updated: Validation Session 1 - proxy-auth tokens revoked after endpoint removal -->

Functional:
- **Image.** `Dockerfile` is multi-stage, with both base images pinned by digest: uv installs the locked runtime deps into `/app/.venv`, and the runtime stage is `python:3.12-slim@sha256:…`.
  - It runs as uid 10001 with `HOME=/tmp` and `ATELIER_VERSION` taken from a build argument.
  - `CMD` is `uvicorn --factory atelier.main:create_app --host 0.0.0.0 --port 8000 --workers 1`.
  - `.dockerignore` is an allowlist containing only `pyproject.toml`, `uv.lock` and `atelier/`.
- **Compose.** `compose.yaml` defines project `atelier` with service `atelier`, `container_name: atelier`, `pull_policy: never`, the settings from brief §6, and its own network on the fixed subnet `172.30.90.0/24` (sketched in Architecture).
- **Deploy script.** `deploy/deploy.sh`:
  - As the forced command, it accepts only `deploy <40-hex sha> sha256:<64-hex digest>`, with the registry token and the registry user (the workflow's actor) on stdin, one per line.
  - It pulls by digest into a throwaway Docker config, then checks the revision label, declared volumes and size, and tags the image locally as `:<sha>`.
  - It runs `apply` in a transient systemd unit: take the lock, `up --wait`, and require version and loop health. On failure it rolls back to the previous tag and exits 1; on success it writes both tag files and prunes local Atelier images down to 3.
  - From a root shell, it accepts `rollback`, `stop`, `start` and `status`. `rollback` swaps `current-tag` and `previous-tag` after a healthy start of the previous image.
- **Workflows.** Every action is pinned by full commit SHA with a version comment (the SHAs are [UNVERIFIED] until implementation). Each workflow sets `permissions: {}` at the top level and grants the minimum per job. Every checkout uses `persist-credentials: false`.
  - `ci.yml` runs on PRs and non-main pushes, and as a reusable workflow: `uv sync --frozen`, `ruff check`, `pytest`, `shellcheck` and a secret scan of git history.
  - `deploy.yml` runs on pushes to `main` and on `workflow_dispatch` from `main` only, and always builds.
    - Its jobs run test, then build (`packages: write`, outputs the digest), then deploy (`packages: read`, environment `production`).
    - It sets `concurrency: {group: deploy-atelier, cancel-in-progress: false}`.
  - `deploy-modal.yml` runs on `modal/**` changes to `main` and on dispatch from `main`. It has a test job with no secrets, then a deploy job (environment `production`) whose only step with the Modal token runs `modal deploy modal/qwen21_uc_app.py`.
- **GitHub environment.** `production` is limited to the `main` branch and holds `HETZNER_HOST`, `HETZNER_KNOWN_HOSTS`, `ATELIER_DEPLOY_SSH_KEY`, `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`. There are no repository-level secrets.
- **Server.**
  - D1: the root-key and Tailscale audit is recorded, and LearnFlow's key is restricted to `restrict,command="rrsync -wo /var/www/learnflow"`.
  - The 50 GB volume is mounted at `/mnt/atelier-data`, owned by 10001, with the sentinel file present.
  - `/opt/atelier` (0750 root) holds `compose.yaml` (0644), `deploy.sh` (0755), `.env` (0600), `current-tag` and `previous-tag`.
  - The restricted Atelier key sits in root's `authorized_keys`.
  - `atelier-egress.service` blocks the Atelier subnet from `169.254.169.254` and `100.64.0.0/10`.
- **Edge.**
  - An Access application for `atelier.flowitup.com` with an owner-email Allow policy, a Service Auth policy for the service token `atelier-plugin`, and its cookie set to SameSite=Lax.
  - The service token lasts 1 year. Its expiry date is recorded in the guide, with a calendar reminder to rotate it.
  - An ingress rule `atelier.flowitup.com → http://localhost:8090` before the catch-all.
  - A DNS route created with `cloudflared tunnel route dns`.
- **Modal (D2).** Two dedicated tokens: `atelier-runtime` in `/opt/atelier/.env` only, and `atelier-ci` in the `production` environment only.
  - A workspace spend limit, set on the "Usage & Billing" page, which stops billable workloads when reached. Usage alerts too, if Modal offers them.
  - Phase 1's redeploy removed the legacy `api` endpoint, so all proxy-auth tokens are revoked.
- **Runbook.** `docs/deployment-guide.md` covers:
  - one-time setup, the key audit and the LearnFlow restriction, the `.env` variables;
  - deploy, and the `deploy.sh` subcommands, the only manual path;
  - the tunnel runbook with its rollback;
  - the egress unit, and routine operations;
  - the service token's expiry date and rotation reminder.

Non-functional:
- The image stays small (about 300 MB) and contains no credential.
- Any single step can be rolled back without touching Folio.
- Every server, Cloudflare, GitHub and Modal step waits for the owner's explicit go-ahead at execution time.

## Architecture

<!-- Updated: Red Team 2026-09-25 - F7 tunnel cutover -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Validation Session 1 - registry user on stdin -->

```
push main ─► deploy.yml ─► test (ci.yml) ─► build+push ghcr, output digest (packages: write)
          ─► deploy job (environment production, packages: read):
             printf token | ssh -i restricted-key root@host "deploy <sha> sha256:<digest>"
                 ▼ authorized_keys: restrict,command="/opt/atelier/deploy.sh"
          deploy.sh (forced command): validate ─► docker --config <tmp> login/pull @digest/logout
             ─► revision label == sha, no declared volumes, size ≤ cap ─► docker tag @digest :sha
             ─► systemd-run --wait --collect --pipe deploy.sh apply <sha>
          apply (transient unit): flock ─► compose up --wait (ATELIER_TAG=sha)
             ─► /healthz version==sha and loops ok ? write tags, prune to 3 : up previous-tag, exit 1
owner at a root shell ─► deploy.sh rollback | stop | start | status
push main touching modal/** ─► deploy-modal.yml: test (no secrets) ─► deploy (production env): modal deploy
Browser/plugin ─► Access (owner email | service token) ─► tunnel ─► localhost:8090 ─► container :8000
```

`compose.yaml`:

```yaml
name: atelier
services:
  atelier:
    image: ghcr.io/flowitup/atelier:${ATELIER_TAG:?required}   # a local tag that deploy.sh bound to a verified digest
    container_name: atelier
    pull_policy: never                      # images arrive only through deploy.sh
    restart: unless-stopped
    user: "10001:10001"
    read_only: true
    tmpfs: ["/tmp:size=256m"]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    ports: ["127.0.0.1:8090:8000"]
    volumes: ["/mnt/atelier-data:/data"]
    env_file: [/opt/atelier/.env]
    environment:
      ATELIER_ENV: production
      ATELIER_DATA_DIR: /data
      ATELIER_CF_AUD: ${ATELIER_CF_AUD:?required}
      ATELIER_OWNER_EMAIL: ${ATELIER_OWNER_EMAIL:?required}
      ATELIER_PLUGIN_CLIENT_ID: ${ATELIER_PLUGIN_CLIENT_ID:?required}
      MODAL_TOKEN_ID: ${MODAL_TOKEN_ID:?required}
      MODAL_TOKEN_SECRET: ${MODAL_TOKEN_SECRET:?required}
    mem_limit: 768m
    cpus: 1.0
    healthcheck:
      test: ["CMD", "python", "-c", "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 20s
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}
    networks: [atelier]
networks:
  atelier:
    ipam:
      config:
        - subnet: 172.30.90.0/24            # fixed, so the egress rules can name it; checked for collisions first
```

Compose reads `/opt/atelier/.env` automatically for `${…:?required}` interpolation, because it sits in the project directory. So a missing critical variable fails `up` before any container changes.

`Dockerfile`:

```dockerfile
FROM python:3.12-slim@sha256:<digest> AS build
COPY --from=ghcr.io/astral-sh/uv:<version from `uv --version`>@sha256:<digest> /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY atelier ./atelier

FROM python:3.12-slim@sha256:<digest>
ARG ATELIER_VERSION=dev
ENV PATH=/app/.venv/bin:$PATH HOME=/tmp PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ATELIER_VERSION=$ATELIER_VERSION
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin atelier
WORKDIR /app
COPY --from=build /app /app
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "--factory", "atelier.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

`deploy/deploy.sh`, covering validation, digest checks, detached apply, rollback and pruning:

```bash
#!/usr/bin/env bash
# Atelier deploy entry point.
#   Forced command of the CI key:  SSH_ORIGINAL_COMMAND="deploy <40-hex sha> sha256:<64-hex digest>",
#                                   with a short-lived registry token and the registry user on stdin.
#   Owner at a root shell:         deploy.sh rollback | stop | start | status
# The step that changes the running service runs in a transient systemd unit, so a dropped SSH
# session cannot interrupt it half-way. Nothing outside /opt/atelier and Atelier's images is touched.
set -euo pipefail
umask 077
readonly DIR=${ATELIER_DEPLOY_DIR:-/opt/atelier} IMAGE=ghcr.io/flowitup/atelier KEEP=3 MAX_IMAGE_BYTES=1500000000
log()    { /usr/bin/logger -t atelier-deploy -- "$*"; printf '%s\n' "$*" >&2 2>/dev/null || true; }
reject() { log "rejected: $1"; exit 2; }
lock()   { exec 9>"$DIR/.deploy.lock"; flock -n 9 || { log "another deploy is running"; exit 75; }; }
up()     { ATELIER_TAG="$1" docker compose up -d --wait --wait-timeout 60 >/dev/null 2>&1; }
healthy() {
  local body; body=$(curl -fsS --max-time 5 http://127.0.0.1:8090/healthz 2>/dev/null) || return 1
  [[ "$body" == *"\"version\":\"$1\""* && "$body" == *"\"loops\":\"ok\""* ]]
}
write_tags() {   # $1 = new current, $2 = new previous; each file is replaced by an atomic rename
  printf '%s\n' "$2" > previous-tag.new && printf '%s\n' "$1" > current-tag.new
  mv -f previous-tag.new previous-tag && mv -f current-tag.new current-tag
}
prune() {        # keep current, previous and the newest other Atelier image; never touch another repository
  docker image ls "$IMAGE" --format '{{.Tag}}' | grep -E '^[0-9a-f]{40}$' \
    | awk -v c="$1" -v p="$2" -v k="$KEEP" '$0 != c && $0 != p { if (++n > k - 2) print }' \
    | while read -r old; do
        refs=$(docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE:$old" | grep "^$IMAGE@" || true)
        docker image rm "$IMAGE:$old" $refs >/dev/null && log "pruned $old"
      done
}

if [[ -n "${SSH_ORIGINAL_COMMAND:-}" ]]; then        # the CI key may only deploy
  read -r verb sha digest extra <<<"$SSH_ORIGINAL_COMMAND"
  [[ "$verb" == "deploy" && -z "${extra:-}" ]] || reject "unexpected command"
else
  verb=${1:-}; sha=${2:-}
  [[ "$verb" =~ ^(rollback|stop|start|status|apply)$ ]] || reject "unexpected command"
fi

case "$verb" in
  deploy)
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || reject "sha must be 40 lowercase hex"
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || reject "digest must be sha256:<64 hex>"
    IFS= read -r -t 15 token || reject "no registry token on stdin"
    IFS= read -r -t 5 user || reject "no registry user on stdin"
    [[ "$user" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$ ]] || reject "bad registry user"
    cfg=$(mktemp -d); trap 'rm -rf "$cfg"' EXIT      # throwaway docker config; root's ~/.docker is never touched
    printf '%s' "$token" | docker --config "$cfg" login ghcr.io -u "$user" --password-stdin >/dev/null
    unset token
    docker --config "$cfg" pull --quiet "$IMAGE@$digest" >/dev/null
    docker --config "$cfg" logout ghcr.io >/dev/null 2>&1 || true
    rev=$(docker image inspect "$IMAGE@$digest" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
    [[ "$rev" == "$sha" ]] || reject "image revision label is not $sha"
    vols=$(docker image inspect "$IMAGE@$digest" --format '{{json .Config.Volumes}}')
    [[ "$vols" == "null" || "$vols" == "{}" ]] || reject "image declares volumes"
    size=$(docker image inspect "$IMAGE@$digest" --format '{{.Size}}')
    (( size <= MAX_IMAGE_BYTES )) || reject "image is larger than the size cap"
    docker tag "$IMAGE@$digest" "$IMAGE:$sha"
    rm -rf "$cfg"; trap - EXIT
    exec systemd-run --unit=atelier-deploy --wait --collect --pipe --quiet "$DIR/deploy.sh" apply "$sha"
    ;;
  apply)                                             # runs inside the transient unit
    trap '' PIPE                                     # a vanished SSH session must not stop the rollback
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || reject "sha must be 40 lowercase hex"
    lock; cd "$DIR"; prev=$(cat current-tag 2>/dev/null || true)
    if up "$sha" && healthy "$sha"; then
      if [[ -n "$prev" && "$prev" != "$sha" ]]; then write_tags "$sha" "$prev"; else printf '%s\n' "$sha" > current-tag; fi
      prune "$sha" "$prev"; log "deployed $sha"; exit 0
    fi
    log "health check failed for $sha"
    if [[ -n "$prev" ]] && up "$prev" && healthy "$prev"; then log "rolled back to $prev"
    else log "rollback impossible or failed; previous=${prev:-none}"; fi
    exit 1
    ;;
  rollback)
    lock; cd "$DIR"; cur=$(cat current-tag); prev=$(cat previous-tag 2>/dev/null || true)
    [[ -n "$prev" ]] && docker image inspect "$IMAGE:$prev" >/dev/null 2>&1 || { log "no previous image on this host"; exit 1; }
    if up "$prev" && healthy "$prev"; then write_tags "$prev" "$cur"; log "rolled back to $prev"; exit 0; fi
    log "rollback to $prev failed its health check; restoring $cur"; up "$cur" && healthy "$cur"; exit 1
    ;;
  stop)   lock; cd "$DIR"; ATELIER_TAG=$(cat current-tag) docker compose down ;;
  start)  lock; cd "$DIR"; t=$(cat current-tag); up "$t" && healthy "$t" ;;
  status) cd "$DIR"; echo "current=$(cat current-tag) previous=$(cat previous-tag 2>/dev/null || true)"
          ATELIER_TAG=$(cat current-tag) docker compose ps; curl -fsS --max-time 5 http://127.0.0.1:8090/healthz || true ;;
esac
```

- `ATELIER_DEPLOY_DIR` exists only for the hermetic test. The forced command can't set it: `restrict` and sshd's default `PermitUserEnvironment no` drop client environment, and the transient unit gets a clean environment.
- The `systemd-run` flags are [UNVERIFIED]; confirm them with `systemd-run --help` on the server.
- The registry user is the workflow's actor, sent on stdin after the token. GitHub documents `docker login ghcr.io -u <username>` with the account's username and doesn't say that any value is accepted, so the plan follows the documented pattern instead of a fixed name.

`deploy.yml`, the build and deploy jobs (actions pinned by full commit SHA; the version comments are illustrative):

```yaml
permissions: {}
concurrency: {group: deploy-atelier, cancel-in-progress: false}
jobs:
  build:
    needs: test
    runs-on: ubuntu-latest
    permissions: {contents: read, packages: write}
    outputs:
      digest: ${{ steps.build.outputs.digest }}
    steps:
      - uses: actions/checkout@<full commit sha> # v7
        with: {persist-credentials: false}
      - uses: docker/login-action@<full commit sha> # vN
        with: {registry: ghcr.io, username: "${{ github.actor }}", password: "${{ github.token }}"}
      - id: build
        uses: docker/build-push-action@<full commit sha> # vN
        with:
          push: true
          tags: ghcr.io/flowitup/atelier:${{ github.sha }},ghcr.io/flowitup/atelier:latest
          build-args: ATELIER_VERSION=${{ github.sha }}
          labels: |
            org.opencontainers.image.revision=${{ github.sha }}
            org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment: production
    permissions: {packages: read}
    steps:
      - name: Deploy through the restricted key
        env:
          SHA: ${{ github.sha }}
          DIGEST: ${{ needs.build.outputs.digest }}
          HOST: ${{ secrets.HETZNER_HOST }}
          SSH_KEY: ${{ secrets.ATELIER_DEPLOY_SSH_KEY }}
          KNOWN_HOSTS: ${{ secrets.HETZNER_KNOWN_HOSTS }}
          REGISTRY_TOKEN: ${{ github.token }}
        run: |
          [[ "$SHA" =~ ^[0-9a-f]{40}$ && "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "bad sha or digest"; exit 1; }
          install -d -m 700 ~/.ssh
          printf '%s\n' "$SSH_KEY" > ~/.ssh/atelier_deploy && chmod 600 ~/.ssh/atelier_deploy
          printf '%s\n' "$KNOWN_HOSTS" > ~/.ssh/known_hosts
          printf '%s\n%s\n' "$REGISTRY_TOKEN" "$GITHUB_ACTOR" | ssh -i ~/.ssh/atelier_deploy -o IdentitiesOnly=yes \
            -o StrictHostKeyChecking=yes -o BatchMode=yes "root@$HOST" "deploy $SHA $DIGEST"
```

The `test` job calls `./.github/workflows/ci.yml` with `permissions: {contents: read}` and `if: github.ref == 'refs/heads/main'`, so a dispatch from another branch runs nothing. `ci.yml` triggers on `pull_request`, on `push` with `branches-ignore: [main]` and on `workflow_call`, so `main` runs the tests only once, inside `deploy.yml`.

`deploy/systemd/atelier-egress.service`:

```ini
[Unit]
Description=Block Atelier container egress to cloud metadata and the tailnet
Requires=docker.service
After=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for d in 169.254.169.254/32 100.64.0.0/10; do iptables -C DOCKER-USER -s 172.30.90.0/24 -d "$d" -j DROP 2>/dev/null || iptables -I DOCKER-USER -s 172.30.90.0/24 -d "$d" -j DROP; done'
ExecStop=/bin/sh -c 'for d in 169.254.169.254/32 100.64.0.0/10; do iptables -D DOCKER-USER -s 172.30.90.0/24 -d "$d" -j DROP 2>/dev/null || true; done'

[Install]
WantedBy=multi-user.target
```

The ingress rule, saved as `deploy/cloudflared-ingress-rule.yml` and inserted immediately before `- service: http_status:404`:

```yaml
  - hostname: atelier.flowitup.com
    service: http://localhost:8090
```

## Related Code Files

<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->

Create:
- `/Users/sweet-home/Works/qwen21-uc-modal/Dockerfile`
- `/Users/sweet-home/Works/qwen21-uc-modal/.dockerignore`
- `/Users/sweet-home/Works/qwen21-uc-modal/compose.yaml`
- `/Users/sweet-home/Works/qwen21-uc-modal/deploy/deploy.sh`
- `/Users/sweet-home/Works/qwen21-uc-modal/deploy/cloudflared-ingress-rule.yml`
- `/Users/sweet-home/Works/qwen21-uc-modal/deploy/systemd/atelier-egress.service`
- `/Users/sweet-home/Works/qwen21-uc-modal/.github/workflows/ci.yml`
- `/Users/sweet-home/Works/qwen21-uc-modal/.github/workflows/deploy.yml`
- `/Users/sweet-home/Works/qwen21-uc-modal/.github/workflows/deploy-modal.yml`
- `/Users/sweet-home/Works/qwen21-uc-modal/docs/deployment-guide.md`
- `/Users/sweet-home/Works/qwen21-uc-modal/tests/test_deploy_script.py`: hermetic. It writes stub `docker`, `curl`, `systemd-run`, `flock` and `logger` executables into a temporary `bin/`, puts them first on `PATH`, points `ATELIER_DEPLOY_DIR` at a temporary directory, and runs `bash deploy/deploy.sh`.

Modify (LearnFlow repository, D1, **[OWNER-GATED]** push):
- `/Users/sweet-home/Works/learnflow/.github/workflows/deploy.yml`:
  - pin `burnett01/rsync-deployments` by full commit SHA;
  - change `remote_path` to the path relative to rrsync's directory;
  - add a guard step that fails the job if the deploy key can open a shell.

Modify (server, not repository files): root's `authorized_keys` (one LearnFlow line restricted, one Atelier line added), `/etc/fstab` (one line), `/etc/cloudflared/config.yml` (one rule), and `/etc/systemd/system/atelier-egress.service`.

Delete: none.

## Implementation Steps

<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F7 tunnel cutover -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F10 service identity and scope -->
<!-- Updated: Red Team 2026-09-25 - F12 Modal tokens -->
<!-- Updated: Validation Session 1 - registry user on stdin -->
<!-- Updated: Validation Session 1 - service token 1 year with reminder -->
<!-- Updated: Validation Session 1 - proxy-auth tokens revoked after endpoint removal -->
<!-- Updated: Validation Session 1 - verified gitleaks, cloudflared metrics and Modal spend limit -->
<!-- Updated: Validation Session 1 - storage defaults 50/40/5 -->

**A. Repository work (local, no owner gate).**

1. **Image files.** Write `Dockerfile` with both base images pinned by digest (look the digests up with `docker buildx imagetools inspect`), and the allowlist `.dockerignore` (`*`, `!pyproject.toml`, `!uv.lock`, `!atelier/`).
2. **Deploy files.** Write `compose.yaml`, `deploy/deploy.sh`, `deploy/cloudflared-ingress-rule.yml` and `deploy/systemd/atelier-egress.service` as in Architecture. Run `chmod +x deploy/deploy.sh`.
3. **Hermetic deploy-script tests.** Write `tests/test_deploy_script.py`, using only the stub executables and a temporary deploy directory:
   - `test_rejects_malformed_commands`: `""`, `"deploy"`, a 39-hex SHA, an uppercase SHA, a digest without `sha256:`, `"deploy <sha> <digest> extra"`, `"deploy <sha>; id"` and `"rollback …"` sent as the forced command all exit 2 before any `docker` call.
   - `test_failed_health_rolls_back_and_keeps_both_tags`: the stub `curl` reports the wrong version for the new SHA. The script exits 1 and `up`s the previous tag, and `current-tag` and `previous-tag` are unchanged.
   - `test_successful_deploy_moves_current_to_previous`.
   - `test_rejects_image_with_wrong_revision_label_or_declared_volumes`.
   - `test_rejects_a_missing_or_malformed_registry_user`: a token without a second stdin line, or a user with forbidden characters, exits 2 before any `docker login`.
   - `test_rollback_swaps_the_tags`: after a healthy start of the previous image, the two files are swapped.
   - `test_survives_closed_stdout_and_stderr`: runs the deploy with `>&-` and `2>&-` and still gets the correct exit code and tag files.
4. **Workflows.** Write the three workflow files as in Requirements and Architecture.
   - Pin every action by full commit SHA with a version comment. Look each SHA up at implementation time with `gh api repos/<owner>/<action>/git/ref/tags/<tag>`; they are [UNVERIFIED] until then.
   - `ci.yml` checks out with `fetch-depth: 0` and `persist-credentials: false`, and adds:
     - a `shellcheck` step: `find deploy -name '*.sh' -print0 | xargs -0 -r shellcheck`;
     - a secret scan of git history only, never untracked files such as `logs/`: `docker run --rm -v "$PWD:/repo" ghcr.io/gitleaks/gitleaks@sha256:<digest> git /repo --redact`. The gitleaks README confirms `ghcr.io/gitleaks/gitleaks` is an official image and that `git` and `--redact` are current v8 options.
   - `deploy-modal.yml`: a `test` job (`contents: read`, no secrets) runs `uv run pytest -q tests/test_modal_backend_script.py tests/test_graph_parity.py`. A `deploy` job (`needs: test`, `environment: production`, `if: github.ref == 'refs/heads/main'`) runs `uv sync --frozen --no-dev`, then a single step with `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` and `MODAL_ENVIRONMENT=main` in its own `env` runs `uv run --no-sync modal deploy modal/qwen21_uc_app.py`.
5. **Local container check.** On Docker Desktop, build and run the image locally with the production confinement flags, as in Verification. Confirm:
   - it runs as uid 10001;
   - `/healthz` answers with `"loops":"ok"`;
   - `python -m modal --help` works;
   - `/app` holds only `.venv` and `atelier`;
   - `docker image inspect` shows the revision label and `Config.Volumes` null.
6. **Deployment guide.** Write `docs/deployment-guide.md` covering:
   - owner actions and server layout;
   - the key audit record (fingerprint → holder → options, never key material) and the LearnFlow restriction;
   - the volume setup, the `/opt/atelier` layout, and a `.env` variable table (names and meaning only, never values);
   - deploy-key setup and storage;
   - the Access app, the SameSite setting, and the service token with its expiry date and rotation reminder;
   - Modal tokens, the spend limit and the proxy-token inventory;
   - the egress unit;
   - the tunnel runbook with its rollback;
   - `deploy.sh rollback | stop | start | status` as the only manual path, and routine operations.
7. **Lint, test and commit.** Run `uv run pytest -q` and `shellcheck deploy/deploy.sh`, then commit as `feat: container image, compose service, forced-command deploy and CI workflows`.

**B. Owner-gated rollout, in this order. Ask before each step, and stop on any unexpected output.**

8. **[OWNER-GATED] Read-only root-key audit (D1).** This runs before any Atelier data lands on the host.

   ```bash
   ssh folio-prod "sed -E 's/((ssh|ecdsa|sk)-[A-Za-z0-9@.-]+) [A-Za-z0-9+\/=]+/\1 <key>/' /root/.ssh/authorized_keys"  # options, type, comment
   ssh folio-prod 'ssh-keygen -lf /root/.ssh/authorized_keys'                                   # fingerprints, in file order
   ssh folio-prod 'ls -la /home/*/.ssh/authorized_keys 2>/dev/null; sshd -T | grep -Ei "^(permitrootlogin|authorizedkeysfile|passwordauthentication) "'
   ssh folio-prod 'tailscale status --self; tailscale debug prefs | grep -i runssh'            # Tailscale SSH on? [UNVERIFIED flags]
   ```

   - The owner maps every key to its holder (for example: owner laptop, LearnFlow CI), and the result goes into the guide without key material.
   - If the audit finds any unrestricted CI key other than LearnFlow's (for example Folio's), stop and present it to the owner as a decision before continuing.
9. **[OWNER-GATED] Restrict LearnFlow's deploy key (D1).** This amends the contract non-goal "don't change LearnFlow".
   1. On the server, confirm `rrsync` is present: `command -v rrsync` [UNVERIFIED path on Ubuntu 26.04]. Back up `authorized_keys` to a timestamped copy.
   2. Edit **only** LearnFlow's line, identified by its fingerprint from step 8, so it starts with `restrict,command="rrsync -wo /var/www/learnflow"`.
   3. In LearnFlow's `deploy.yml`:
      - pin `burnett01/rsync-deployments` by full commit SHA;
      - set `remote_path` to the rrsync-relative path. Confirm the exact form with `rrsync`'s help; it is [UNVERIFIED].
      - add a guard step after the upload that writes the key to a file and fails the job if `ssh -i <key> -o BatchMode=yes <user>@<host> id` succeeds.
   4. Push, or dispatch LearnFlow's deploy. **Pass:** the run is green, including the guard; `ssh folio-prod 'stat -c %y /var/www/learnflow/index.html'` is newer than the run; and `curl -sI https://learn.flowitup.com` still returns the Access 302.
   5. **Rollback:** put back LearnFlow's original line by editing that single line from the backup, and revert the LearnFlow commit.
10. **[OWNER-GATED] Cloudflare Access application.**
    - Create the service token `atelier-plugin` first, in Access → Service credentials → Service Tokens, with a duration of **1 year**.
      - The owner stores the Client Secret in the password manager only; it is shown once.
      - Record the expiry date in `docs/deployment-guide.md`, and add a calendar reminder two weeks before it to rotate the token.
      - On a suspected leak, revoke it immediately, with no grace period.
    - In Zero Trust → Access → Applications, add a self-hosted application for `atelier.flowitup.com` with:
      - an Allow policy "Owner": Include → Emails → the owner's address;
      - a Service Auth policy "Claude plugin": Include → Service Token → `atelier-plugin`.
    - Set the application cookie's SameSite attribute to **Lax**; Cloudflare's default is None.
    - Copy the Application Audience (AUD) tag and the service token's Client ID into the owner's notes for `.env`.
11. **[OWNER-GATED] Modal (D2).**
    - Create two dedicated tokens: `atelier-runtime` (for `.env` only) and `atelier-ci` (for the `production` environment only). Both are workspace-wide, because the workspace has no Service Users.
    - Set a workspace spend limit on Modal's "Usage & Billing" settings page. Modal's docs confirm that only workspace Owners and Managers can set it, and that reaching it stops workloads that would incur further charges. Enable usage alerts too if Modal offers them; the budget docs don't mention alerts, so they are [UNVERIFIED].
    - Phase 1's redeploy removed the legacy `api` endpoint, so revoke the workspace's proxy-auth tokens, which are now unused.
12. **[OWNER-GATED] Hetzner Volume.** In Folio's Hetzner project, the owner creates a 50 GB volume, the validated size (about €2.85 a month at €0.057/GB), in the server's location, attaches it to `folio-prod-1` **without** automount, and optionally has it formatted as ext4.
13. **[OWNER-GATED] Mount the volume** on `folio-prod-1` as root:

    ```bash
    lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT; ls -l /dev/disk/by-id/ | grep HC_Volume
    DEV=/dev/disk/by-id/scsi-0HC_Volume_<id>          # owner confirms: 50G, no mountpoint
    blkid "$DEV" || mkfs.ext4 -L atelier-data "$DEV"   # format only if it has no filesystem
    UUID=$(blkid -s UUID -o value "$DEV"); mkdir -p /mnt/atelier-data
    cp -a /etc/fstab "/etc/fstab.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    echo "UUID=$UUID /mnt/atelier-data ext4 defaults,nofail,noatime 0 2" >> /etc/fstab
    findmnt --verify && mount /mnt/atelier-data && findmnt /mnt/atelier-data
    touch /mnt/atelier-data/.atelier-volume
    mkdir -p /mnt/atelier-data/images /mnt/atelier-data/backup   # uutils `install -o` rejects an unknown numeric owner
    chown 10001:10001 /mnt/atelier-data /mnt/atelier-data/.atelier-volume /mnt/atelier-data/images /mnt/atelier-data/backup
    chmod 0750 /mnt/atelier-data /mnt/atelier-data/images /mnt/atelier-data/backup
    ```

14. **[OWNER-GATED] `/opt/atelier` and the files on the server.**
    - Check that the port is free: `ss -ltnp | grep -q ':8090 ' && echo BUSY || echo free`.
    - Check that the subnet doesn't collide: `docker network inspect $(docker network ls -q) --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}'` and `ip -4 route` must show nothing in `172.30.90.0/24`. If something does, pick a free /24 and change it in `compose.yaml` and the egress unit.
    - Run `install -d -m 0750 /opt/atelier`. From the laptop, `scp compose.yaml deploy/deploy.sh folio-prod:/opt/atelier/`. Set ownership to `root:root` with `chmod 0644 compose.yaml` and `chmod 0755 deploy.sh`.
    - Create `.env` with `install -m 0600 /dev/null /opt/atelier/.env` and have the owner fill it in with an editor, never with `echo`. It holds the variables from the guide:
      - `ATELIER_PUBLIC_ORIGIN`, `ATELIER_CF_TEAM_DOMAIN`, `ATELIER_CF_AUD`, `ATELIER_OWNER_EMAIL` and `ATELIER_PLUGIN_CLIENT_ID`;
      - `ATELIER_DATA_CAP_GB=40` and `ATELIER_MIN_FREE_GB=5` (the validated defaults), and `ATELIER_TIMEZONE`;
      - `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` (the `atelier-runtime` token) and `MODAL_ENVIRONMENT=main`.
    - Check interpolation with `cd /opt/atelier && ATELIER_TAG=check docker compose config --quiet`.
15. **[OWNER-GATED] Egress block.**
    - Check the chain exists: `iptables -L DOCKER-USER -n`.
    - Install `atelier-egress.service` to `/etc/systemd/system/`, then run `systemctl daemon-reload && systemctl enable --now atelier-egress.service` and `iptables -L DOCKER-USER -n` to show the two DROP rules.
    - The container-side check runs after the first deploy (step 18).
16. **[OWNER-GATED] Deploy key.**
    - On the laptop: `umask 077; d=$(mktemp -d); ssh-keygen -t ed25519 -N '' -C atelier-deploy -f "$d/atelier_deploy"`.
    - The owner stores the private key **deliberately** in the password manager (its recovery copy) and in the `production` environment secret (step 17). Then run plain `rm "$d/atelier_deploy"`; this is not secure deletion, and none is claimed.
    - Back up root's `authorized_keys` on the server, then append `restrict,command="/opt/atelier/deploy.sh" <pubkey>`.
    - Record the host key with `ssh-keyscan -t ed25519 <host>`, and check its fingerprint against `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` run on the server.
    - Prove the boundary: `ssh -i <key> -o IdentitiesOnly=yes root@<host> id` must print `rejected: unexpected command` and exit 2.
17. **[OWNER-GATED] GitHub.**
    - Run `gh repo create flowitup/atelier --private --source /Users/sweet-home/Works/qwen21-uc-modal --remote origin`.
    - In the repo's Settings → Environments, create `production` with deployment branches limited to `main`.
    - Set the environment secrets `HETZNER_HOST`, `HETZNER_KNOWN_HOSTS`, `ATELIER_DEPLOY_SSH_KEY` (read from the file), `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` (the `atelier-ci` token). Use `gh secret set <NAME> --env production --repo flowitup/atelier` with the value typed or piped from a file, never passed as a command-line argument. Create no repository-level secrets.
18. **[OWNER-GATED] First deploy.**
    - Run `git push -u origin main`, then `gh run watch` on the `deploy` run.
    - If the deploy job's pull is denied: GitHub's docs say `GITHUB_TOKEN` can pull a private package only when the repository has read access to it. In the package's settings (Manage Actions access), give `flowitup/atelier` read access, then re-run the failed job.
    - On the server, run `/opt/atelier/deploy.sh status`, the `docker inspect` posture check, and the egress checks from Verification: metadata blocked, Modal and the JWKS reachable.
19. **[OWNER-GATED] Tunnel change by replica cutover.** This is a deliberate, research-backed deviation from brief §6's plain restart. Each sub-step is approved separately and must pass before the next starts.
    1. **(a) Prepare and validate.**

       ```bash
       TS=$(date -u +%Y%m%dT%H%M%SZ); CFG=/etc/cloudflared/config.yml
       cp -a "$CFG" "$CFG.bak-$TS"                                              # back up the live file
       awk '/^[[:space:]]*- service: http_status:404[[:space:]]*$/ && !done {
              match($0, /^[[:space:]]*/); i = substr($0, 1, RLENGTH)
              print i "- hostname: atelier.flowitup.com"; print i "  service: http://localhost:8090"; done = 1 }
            { print } END { if (!done) exit 3 }' "$CFG" > "$CFG.new"
       diff -u "$CFG" "$CFG.new"                                                # PASS: exactly 2 added lines
       grep -E '^(tunnel|credentials-file|origincert|metrics):' "$CFG"          # note these settings
       cloudflared tunnel ingress --help; cloudflared tunnel ingress validate --help; cloudflared tunnel ingress rule --help
       cloudflared tunnel --config "$CFG.new" ingress validate                  # PASS: exit 0 [UNVERIFIED syntax]
       for u in https://folio.flowitup.com/health https://folio.flowitup.com/api/x https://folio.flowitup.com/ \
                https://cdn.flowitup.com/ https://learn.flowitup.com/ https://atelier.flowitup.com/; do
         cloudflared tunnel --config "$CFG.new" ingress rule "$u"; done         # PASS: each URL hits its intended rule
       ```

       Then, only after step 10's Access application exists, run `cloudflared tunnel route dns <tunnel from the config> atelier.flowitup.com`. **Pass:** cloudflared reports the CNAME, and `dig +short atelier.flowitup.com` resolves to Cloudflare.
    2. **(b) Baselines,** from the laptop. **Pass:** folio `/health` is 200, cdn's current status is recorded, and learn returns a 302 to `flowitupteam.cloudflareaccess.com`.
    3. **(c) Start the replica.**
       - Read the live command with `systemctl show -p ExecStart --value cloudflared`.
       - Build the replica command from its `argv[]`, with the same binary and flags plus `--config "$CFG.new"` and its own `--metrics 127.0.0.1:<free port>`. Place `--metrics` between `tunnel` and `run`, as Cloudflare's metrics docs show (`cloudflared tunnel --metrics 127.0.0.1:60123 run my-tunnel`). Check the port is free with `ss -ltnp` first.
       - Run it with `systemd-run --unit=cloudflared-atelier-cutover --collect <that command>`.
       - **Pass:** `journalctl -u cloudflared-atelier-cutover --no-pager | grep -c "Registered tunnel connection"` is at least 1. As a second check, `curl -fsS http://127.0.0.1:<port>/ready` returns 200; only third-party sources document `/ready`, so it stays [UNVERIFIED]. If the replica doesn't register within 60 s, stop it and change nothing else.
    4. **(d) Restart the service onto the new config.**

       ```bash
       install -m "$(stat -c %a "$CFG")" -o "$(stat -c %u "$CFG")" -g "$(stat -c %g "$CFG")" "$CFG.new" "$CFG"
       T_RESTART=$(date '+%Y-%m-%d %H:%M:%S'); systemctl restart cloudflared; sleep 10
       systemctl is-active cloudflared                                                        # PASS: active
       journalctl -u cloudflared --since "$T_RESTART" --no-pager | grep -c "Registered tunnel connection"   # PASS: ≥ 1
       curl -fsS http://127.0.0.1:20241/ready                                                 # second check: 200 (endpoint not in the official docs)
       ```

       **On any failure, STOP.**
       1. Keep the replica running; it serves folio, cdn and learn.
       2. `cp -a "$CFG.bak-$TS" "$CFG"`, then `systemctl restart cloudflared`.
       3. Re-run the three checks against the restored config.
       4. Only once they pass, stop the replica and report to the owner.
    5. **(e) External checks.** **Pass:** folio, cdn and learn match their baselines, and `atelier.flowitup.com` returns a 302 to the Access login.
    6. **(f) Stop the replica:** `systemctl stop cloudflared-atelier-cutover`.
    7. **(g) External checks again,** now with only the service running. **Pass:** the same results as (e).
20. **[OWNER-GATED] Live acceptance for this phase,** spending about $0.10 of GPU. Log in through Access and confirm the gallery loads.
    - With `modal container list --json` showing `[]` (a cold backend), generate one image and watch it go queued → running → done with no 524.
    - Submit two jobs, run `/opt/atelier/deploy.sh stop` and then `/opt/atelier/deploy.sh start` on the server while they run, and confirm both finish and appear after the restart.
21. **[OWNER-GATED] Modal redeploy from CI.** Run `gh workflow run deploy-modal.yml --ref main` once. It redeploys identical code and spends no GPU time. Confirm the run succeeds and `modal app list --json` still shows `deployed`.

## Todo List

- [x] Dockerfile with digest-pinned bases, allowlist `.dockerignore`, and `compose.yaml` with the fixed subnet
- [x] `deploy/deploy.sh` with the digest grammar, provenance checks, detached apply, and `rollback | stop | start | status`; hermetic tests; `shellcheck` clean
- [x] `ci.yml`, `deploy.yml` and the split `deploy-modal.yml`: actions pinned by SHA, `permissions: {}`, `persist-credentials: false`, environment `production`, no build skip
- [x] Local confined container run passes (uid 10001, read-only, `/healthz` with live loops, `python -m modal` present, revision label)
- [x] `docs/deployment-guide.md`: setup, key audit, `.env` table, the `deploy.sh` subcommands, egress, tunnel runbook
- [x] [OWNER-GATED] Root-key and Tailscale audit recorded; any other unrestricted CI key surfaced to the owner
- [x] [OWNER-GATED] LearnFlow key restricted to rrsync; LearnFlow deploy green; shell refused
- [ ] [OWNER-GATED] Access app, service token (1 year, expiry recorded, rotation reminder set), AUD, SameSite=Lax
- [x] [OWNER-GATED] Modal runtime and CI tokens, spend limit (plus alerts if offered), now-unused proxy-auth tokens revoked
- [x] [OWNER-GATED] Hetzner Volume created, attached, mounted by UUID with `nofail`, sentinel present
- [x] [OWNER-GATED] `/opt/atelier` files, `.env`, subnet check, egress unit, restricted key, host key pinned
- [x] [OWNER-GATED] GitHub repo, `production` environment and its secrets; first deploy green
- [x] [OWNER-GATED] Tunnel cutover sub-steps (a)–(g), each passed; DNS route
- [x] [OWNER-GATED] Cold-start job, restart-mid-job check, `deploy-modal` dispatch

## Success Criteria

<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Validation Session 1 - proxy-auth tokens revoked after endpoint removal -->

- **Criterion 1:** `curl -sI https://atelier.flowitup.com/` returns 302 with `Location` on `flowitupteam.cloudflareaccess.com`. On the server, requests to `127.0.0.1:8090/` with no token and with a garbage `Cf-Access-Jwt-Assertion` both return 403.
- **Criterion 2** (live part): a cold-start job submitted through Access finishes with no 524. Two jobs survive a stop and start of the container in the middle of rendering.
- **Criterion 12** (placement): the header shows usage against the volume. `docker inspect atelier` shows the only mount is `/mnt/atelier-data → /data`, and the app refuses to start without the sentinel.
- **Criterion 13:**
  - A push to `main` builds, deploys the recorded digest, and passes the SSH step, which is the health check (version plus live loops).
  - The folio `/health` status, the cdn status and the learn 302 match their pre-change baselines after each tunnel sub-step.
  - A `deploy-modal.yml` run succeeds.
  - The hermetic deploy-script tests pass: malformed commands are rejected, a failed health check rolls back and leaves both tags as they were, `rollback` swaps the tags, and closed stdout and stderr are tolerated.
- **Criterion 14:**
  - `docker image inspect` shows no credential in `Config.Env` or the labels, the revision label equals the SHA, and `/app` holds only `.venv` and `atelier`.
  - gitleaks in CI is clean. Every `uses:` line in `.github/workflows/` is pinned to a 40-hex SHA.
  - The key-boundary check shows the Atelier deploy key cannot run `id`.
- **Modal (D2):** the Usage & Billing page shows the workspace spend limit, and no proxy-auth token remains.
- **Host hardening (D1, F9):**
  - The key audit is recorded in the guide.
  - LearnFlow's key runs only rrsync, and its deploy and shell guard pass.
  - From the container, the metadata address times out while Modal and the Access JWKS answer.

## Verification

```bash
# Local image checks (Docker Desktop)
cd /Users/sweet-home/Works/qwen21-uc-modal
docker build -t atelier:local --build-arg ATELIER_VERSION=local --label org.opencontainers.image.revision=local .
D=$(mktemp -d); docker run -d --name atelier-local --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges:true -p 127.0.0.1:8091:8000 -v "$D:/data" \
  -e ATELIER_ENV=development -e ATELIER_DEV_IDENTITY=owner -e ATELIER_PUBLIC_ORIGIN=http://127.0.0.1:8091 atelier:local
sleep 5; curl -s http://127.0.0.1:8091/healthz; docker exec atelier-local id -u     # "loops":"ok" and 10001
docker exec atelier-local python -m modal --help >/dev/null && echo "modal CLI ok"
docker image inspect atelier:local --format '{{json .Config.Volumes}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
docker exec atelier-local ls -a /app; docker rm -f atelier-local
uv run pytest -q tests/test_deploy_script.py && shellcheck deploy/deploy.sh
grep -hE '^\s*-?\s*uses:' .github/workflows/*.yml | grep -vE '@[0-9a-f]{40}( |$)' ; test $? -eq 1 && echo "all actions pinned by SHA"

# [OWNER-GATED] tunnel baselines and after-checks (run from the laptop)
curl -s -o /dev/null -w 'folio %{http_code}\n' https://folio.flowitup.com/health
curl -s -o /dev/null -w 'cdn %{http_code}\n'   https://cdn.flowitup.com/
curl -s -o /dev/null -w 'learn %{http_code} %{redirect_url}\n' https://learn.flowitup.com/
curl -s -o /dev/null -w 'atelier %{http_code} %{redirect_url}\n' https://atelier.flowitup.com/

# [OWNER-GATED] server-side checks
ssh folio-prod '/opt/atelier/deploy.sh status'
ssh folio-prod "curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/; curl -s -o /dev/null -w '%{http_code}\n' -H 'Cf-Access-Jwt-Assertion: x' http://127.0.0.1:8090/gallery"
ssh folio-prod "docker inspect atelier --format '{{.Config.User}} ro={{.HostConfig.ReadonlyRootfs}} caps={{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{json .HostConfig.PortBindings}} {{range .Mounts}}{{.Source}}->{{.Destination}} {{end}}'"
ssh folio-prod "docker exec atelier python -c \"import urllib.request as u; u.urlopen('http://169.254.169.254/hetzner/v1/metadata', timeout=3)\"" ; test $? -ne 0 && echo "metadata blocked"
ssh folio-prod "docker exec atelier python -m modal app list --json >/dev/null && echo 'Modal reachable'"
ssh folio-prod "docker exec atelier python -c \"import urllib.request as u; print(u.urlopen('https://flowitupteam.cloudflareaccess.com/cdn-cgi/access/certs', timeout=5).status)\""
ssh folio-prod "docker ps --filter label=com.docker.compose.project=folio --format '{{.Names}} {{.Status}}'"   # unchanged vs before
gh run list --repo flowitup/atelier --workflow deploy.yml --limit 1
```

### Owner setup progress (2026-09-26, done in the owner's Chrome at their request)
All secrets were copied by the owner straight into their password manager. None were read, screenshotted or recorded here.
- **Repo:** GitHub `flowitup/atelier` was created (private) and pushed at the owner's request, ahead of step 17. Its `production` environment and secrets are still to do.
- **Modal (step 11):**
  - Tokens `atelier-runtime` and `atelier-ci` were created.
  - The workspace spend limit is set to **$20**, the maximum Modal allows on the Starter plan. The owner chose $50, which Modal refused; the Starter plan includes $30 of monthly credits.
  - The workspace had **no proxy-auth tokens**, so there was nothing to revoke. Service users would need the Team plan.
- **Cloudflare Access (step 10):**
  - Service token `atelier-plugin` created with a 1-year duration (expires around 2027-09-26).
  - Self-hosted application `atelier` for `atelier.flowitup.com`, with the policies "Atelier owner" (Allow, one email: the owner's chosen login address, which goes into `.env` and is not recorded in the repo) and "Atelier Claude plugin" (Service Auth, `atelier-plugin`).
  - Cookie SameSite is **Lax**, identity providers are all available ones, and the session lasts 24 h.
  - The AUD tag was read from the app's settings and goes into `.env`.
  - LearnFlow's separate "Allowed emails" policy was left untouched.
- **Hetzner (step 12):**
  - Volume `atelier-data` (ID 106963035) was created: 50 GB in Falkenstein, attached to `folio-prod-1` with manual mounting, so it is neither formatted nor mounted yet.
  - It costs €3.43/month including VAT (€2.86 before VAT).
  - `folio-prod-1` is a CX33 in `eu-central`.
- **Step 8, root-key audit (read-only, owner-approved):** root's `authorized_keys` held two unrestricted ed25519 keys and no `authorized_keys2`.

  | Fingerprint | Holder | Options |
  |---|---|---|
  | `SHA256:JZx+jCJH…` | the owner's Mac (`~/.ssh/hetzner-deploy`) **and** LearnFlow's CI secret `SSH_PRIVATE_KEY` | none |
  | `SHA256:Qik/LV97…` | Folio's CI (`folio-ci-deploy`) | none |

  - The holders were mapped from sshd's accepted-publickey journal: LearnFlow's 2026-09-13 16:27 deploy came from a GitHub runner using `JZx+`, and Folio's deploys come from Azure runner IPs using `Qik/`.
  - sshd settings: `PermitRootLogin prohibit-password`, password and keyboard-interactive login off, `AcceptEnv` limited to `LANG`, `LC_*`, `COLORTERM` and `NO_COLOR`, and `PermitUserEnvironment no`.
  - Tailscale SSH is off (`RunSSH false`), and `rrsync` is at `/usr/bin/rrsync` (rsync 3.4.1).
- **Owner decisions after the audit:**
  - LearnFlow gets its own restricted key, and the owner's laptop key is rotated, because its private half had sat in LearnFlow's CI secrets since July.
  - Folio's unrestricted CI key is **accepted for now and tracked** as a residual risk, pending a separate Folio hardening task.
- **Step 9 progress:**
  - Key `learnflow-ci` (`SHA256:sRwFrc79…`) was added with `restrict,command="/usr/bin/rrsync -wo /var/www/learnflow"`. A shell request is refused with "rrsync error".
  - `rrsync` (lines 303–307 and 333–334 of `/usr/bin/rrsync`) glues absolute client paths onto the restricted directory, so LearnFlow's `remote_path` becomes `./`.
  - LearnFlow's workflow gets a **pre-upload guard**: the upload runs only if a shell request returns rrsync's refusal. With an unrestricted key the relative path would point into `/root`. The guard was tested locally in both directions.
  - Waiting on the owner to replace LearnFlow's `SSH_PRIVATE_KEY` secret. The workflow change is pushed afterwards.
- **Laptop key rotation:**
  - New admin key `mac-admin-folio-prod-2026-09` (`SHA256:DJ+/jxTo…`) was added and tested, and the `folio-prod` SSH alias now uses `~/.ssh/folio-prod-admin` (`~/.ssh/config` backed up).
  - The old `JZx+` line is removed only after LearnFlow deploys with its own key.
  - Suggested to the owner: add a passphrase with `ssh-keygen -p -f ~/.ssh/folio-prod-admin` and use the macOS Keychain.
- **Server preparation (2026-09-26, the owner said "do it for me"):**
  - **Step 5:** Docker Desktop was restarted. The local confined run passed:
    - `/healthz` reported `"loops":"ok"`, and the app ran as uid 10001;
    - `python -m modal` worked;
    - `Config.Volumes` was null, and the revision label was present;
    - `/app` held only `.venv` and `atelier`, and the root filesystem was read-only;
    - the image was 375 MB.
  - **Git history:** the owner's login email was purged. Five commits were rewritten from a backup branch, with identical final files, and only the three already-published ones were force-pushed with a lease. The Part A commits stay local until the first deploy.
  - **Step 13:**
    - `/dev/sdb` (volume 106963035, 50G, no filesystem) was formatted ext4 and added to `fstab` by UUID with `nofail,noatime` (backup `/etc/fstab.bak-20260926T215130Z`), then mounted. It has 47 GB free.
    - Ownership, the sentinel and the directories were set with `mkdir` and `chown`, because the host's uutils coreutils `install` rejects owner 10001.
  - **Step 14:**
    - Port 8090 was free, and there was no subnet or name collision (`folio` is the only compose project).
    - `/opt/atelier` was created (0750), and `compose.yaml` and `deploy.sh` were copied (checksums match).
    - `.env` was written (0600) with every value that isn't a credential. The owner still fills in `ATELIER_PLUGIN_CLIENT_ID`, `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`.
    - Compose interpolation passes with stand-ins for those three.
  - **Step 15:** the iptables state was saved first, then the unit was installed, enabled and made active. `iptables -S` shows the three DOCKER-USER rules and the INPUT rule. Folio, cdn and LearnFlow answered normally afterwards.
  - **Step 16:**
    - `atelier-deploy` (`SHA256:7+LZJSAo…`) was added with its forced command (backup `authorized_keys.bak-20260926T215349Z`).
    - The host key `SHA256:6dN+8Mw+…` matched between `ssh-keyscan` and the server's own copy.
    - `id` and a shell request were refused with exit 2. A well-formed deploy without a token stopped at "no registry token on stdin". All three refusals were logged in the journal.
  - **Step 17 (in part):** the `production` environment was created with a custom branch policy (`main` only), and `HETZNER_HOST` and `HETZNER_KNOWN_HOSTS` were set. There are no repository-level secrets.
- **Credentials and LearnFlow (2026-09-27 00:08 Paris time):**
  - The owner set LearnFlow's `SSH_PRIVATE_KEY`, plus Atelier's `ATELIER_DEPLOY_SSH_KEY`, `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`.
  - LearnFlow's workflow commit `0b9554e` was pushed. Run 36275371916 was green, the guard passed, and the files were updated at 22:10 UTC.
    - There was no nested or stray upload, and sshd logged only `learnflow-ci` from the runner. `learn.flowitup.com` still returns a 302 to Access.
  - The old `JZx+` line was then removed (backup `authorized_keys.bak-20260926T221116Z`). A login with it is refused, and the `folio-prod` alias still works.
  - The LearnFlow private-key file was deleted from the laptop.
  - Editing `.env` over SSH failed: the server has no terminfo for Ghostty (`xterm-ghostty`). The fix is `TERM=xterm-256color ssh -t folio-prod nano /opt/atelier/.env`.
- **`.env` completed (2026-09-27, about 00:30 Paris time):**
  - The owner couldn't find the plugin Client ID or the `atelier-runtime` secret. The Client ID isn't secret: Claude read it from the Cloudflare dashboard and wrote it into `.env`.
  - In the owner's Chrome, at their request, Claude deleted the unused `atelier-runtime` Modal token and created `atelier-runtime-2`. Claude stopped looking at the tab before the secret appeared.
  - The owner saved the new token through a helper script that reads it at hidden prompts and rewrites only its two `.env` lines over SSH.
  - Check: all three lines have the right shape, `.env` is still 0600 root, and `docker compose config` resolves.
  - The service token's exact expiry, read from the dashboard, is 2027-09-26 17:47 Paris time.
- **Step 18, first deploy (2026-09-27 00:30 Paris time):**
  - Run 36276417182 was green: test, secret scan, build, then deploy. The server logged `deployed 458f387…` about 17 s after the job started.
  - The pull with the job's own `GITHUB_TOKEN` needed no package setting, and the size check before the pull ran through `docker manifest inspect`.
  - `deploy.sh status`: the container was healthy, and `/healthz` reported version `458f387…` with loops `ok`.
  - Posture: uid 10001, read-only, `CapDrop=[ALL]`, `no-new-privileges`, `127.0.0.1:8090` only, `/mnt/atelier-data`→`/data`, 768 MiB.
  - Without a valid JWT the origin returned 403, both with no header and with a forged one.
  - From the container:
    - metadata and the host's tailnet address were blocked (control checks from the host reach both);
    - the Modal API worked with `atelier-runtime-2`;
    - the Access JWKS returned 200.
  - The Folio containers were untouched, and folio returned 200, learn 302 and cdn 403 as its baseline.
  - The laptop's copies of the deploy key and the pinned host key were deleted.
- **Second deploy:** run 36276612738 was green; `current=a119426…` and `previous=458f387…`, so the rollback target is kept on the real host.
- **Step 19, tunnel cutover (2026-09-27 00:35–00:38 Paris time), every sub-step passed:**
  - **(a)** Backup `config.yml.bak-20260926T223546Z`, a diff of exactly two added lines, and `ingress validate` OK; each hostname hit its intended rule. `route dns` added the CNAME using the server's `origincert`.
  - **(b)** Baselines on three samples: folio 200, cdn 403 (its storage backend's `AccessDenied`), learn 302 to Access.
  - **(c)** The replica (`--metrics 127.0.0.1:20242`) registered 4 connections within about 4 s, and `/ready` returned 200.
  - **(d)** The live config was replaced in place (0644 root kept), and the restart re-registered within about 2 s; `/ready` returned 200.
  - **(e)** The neighbours matched their baselines, and `atelier.flowitup.com` returned a 302 to Access.
  - **(f)** The replica was stopped: one cloudflared process left, and its metrics port closed.
  - **(g)** The same results as (e).
  - The `.new` file and the timestamp marker were removed.
- **Step 20, live acceptance (about $0.15 of GPU, covered by Modal's free credits):**
  - The owner logged in through Access.
  - Cold backend (`modal container list` returned `[]`): one 9:16 image went queued → running → done in 62 s, with no 524.
  - Two more images: `deploy.sh stop` (1 s, maintenance on) then `deploy.sh start` (6 s, healthy, maintenance cleared) ran while both rendered. Both finished (21 s and 33 s), and the gallery shows all three.
  - The files are on the volume and owned by 10001.
  - Known issue for phase 7: the gallery shows "Next" whenever a page has images, even when no next page exists.
- **Step 21, Modal redeploy from CI:**
  - The first dispatch failed with "Token ID is malformed": the `atelier-ci` values the owner had pasted into GitHub weren't a real token, and its secret was lost.
  - Claude deleted `atelier-ci` (the owner said yes) and created `atelier-ci-2`. The owner saved it through a hidden-prompt `gh secret set` helper.
  - The re-run (36278211775) was green, and `modal app list` shows `qwen21-uc` deployed.
- **Owner decisions:** auto-renew for `flowitup.com` stays **off**; it expires 2027-04-07 and is renewed by hand. The credential inventory is in `docs/deployment-guide.md` ("Where each credential lives").
- **Still to do:**
  - Remaining owner item: the calendar reminder to rotate the Access service token two weeks before 2027-09-26.
  - The owner's calendar reminder to rotate the service token two weeks before 2027-09-26.
  - Follow-ups outside this plan: rotate `hetzner-deploy` on `dev-deploy`, and harden Folio's CI key.

## Risk Assessment

<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F7 tunnel cutover -->
<!-- Updated: Red Team 2026-09-25 - F8 deploy and rollback -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Validation Session 1 - registry user on stdin -->
<!-- Updated: Validation Session 1 - verified gitleaks, cloudflared metrics and Modal spend limit -->

| Risk | Likelihood × impact | Observable signal | Pre-decided response |
|---|---|---|---|
| The `cloudflared tunnel ingress validate/rule` syntax differs [UNVERIFIED] | Medium × Medium | `--help` shows other subcommands or flags | Adapt the command to what `--help` documents. If no validation command exists, stop and ask the owner, because validation before a restart is a contract constraint. |
| The replica fails to become ready | Low × Medium | No "Registered tunnel connection" line within 60 s in sub-step (c) | Stop the replica; the live service was never touched. Ask the owner how to proceed. |
| The restarted service fails on the new config | Low × High | Sub-step (d) checks fail on the new process only | STOP: keep the replica, restore the backup, restart, re-verify, then stop the replica and report. Folio stays up on the replica throughout. |
| The audit finds another unrestricted CI key | Medium × High | A root key held by a CI system with no `command=` option | Stop and put it to the owner as a decision; don't continue the rollout silently. |
| rrsync's relative path breaks LearnFlow's deploy | Medium × Medium | LearnFlow's run fails, or files land in a nested directory | Fix `remote_path` from rrsync's help and re-run. If still broken, roll back the key line and the workflow change and ask the owner. |
| GHCR denies the pull with `github.token` | Medium × Medium | `docker login` or `pull` "denied" in the deploy logs; nothing changes on the server | GitHub's docs require the repository to have read access to a private package. **[OWNER-GATED]** Grant `flowitup/atelier` read access in the package's Manage Actions access, then re-run the job (step 18). |
| `systemd-run --wait --collect --pipe` behaves differently [UNVERIFIED] | Low × Medium | The deploy step's exit code doesn't match the journal's result | Confirm with `systemd-run --help` on the server, and adjust the flags so the unit's exit status reaches CI. |
| Docker on this host uses the nftables firewall backend, with no `DOCKER-USER` chain | Low × Medium | `iptables -L DOCKER-USER` fails in step 15 | Stop and ask the owner. Don't add rules to Docker's own chains. |
| The chosen subnet collides with an existing network | Low × Medium | Step 14's collision check shows an overlap | Pick a free /24 and change it in both `compose.yaml` and the egress unit. |
| `docker compose up --wait-timeout` is unsupported on Compose v2.40.3 | Low × Medium | `unknown flag` in the journal of the deploy unit | Replace it with the `wait-healthy.sh`-style `docker inspect` loop (Folio `wait-healthy.sh:17-33`), 12 tries of 5 s. |
| The Modal CLI fails in the read-only container | Low × Medium | `python -m modal …` errors about a read-only filesystem | Point `XDG_CACHE_HOME` and `TMPDIR` at `/tmp` in the Dockerfile and rebuild. Phase 6 depends on this. |
| The volume is detached or not mounted at boot | Low × High | `findmnt /mnt/atelier-data` is empty; `/healthz` returns 503 because the sentinel is missing | The app refuses to run, so Folio's disk is safe. The owner reattaches and runs `mount /mnt/atelier-data`. |

**Rollback:**
- **App:** `deploy.sh` rolls back automatically. Manually, run `/opt/atelier/deploy.sh rollback`, which also swaps the tag files.
- **Tunnel:** restore the backup with the replica method (step 19, sub-step (d)'s failure path).
- **LearnFlow key:** restore its single original line and revert the LearnFlow workflow commit.
- **Egress unit:** `systemctl disable --now atelier-egress.service`, which removes its rules.
- **Full removal, Atelier only:**
  - `/opt/atelier/deploy.sh stop`;
  - delete **only** the Atelier line from root's `authorized_keys` (match its `atelier-deploy` comment and review before saving), and never restore an older copy of the file, which would drop keys added since;
  - remove the ingress rule by the same replica method;
  - delete the DNS record and the Access app;
  - disable the egress unit;
  - unmount and detach the volume (the data stays on the volume).

## Security Considerations

<!-- Updated: Red Team 2026-09-25 - F3 co-tenant root keys -->
<!-- Updated: Red Team 2026-09-25 - F9 supply chain -->
<!-- Updated: Red Team 2026-09-25 - F12 Modal tokens -->
<!-- Updated: Validation Session 1 - registry user on stdin -->
<!-- Updated: Validation Session 1 - proxy-auth tokens revoked after endpoint removal -->

- **The CI deploy key can only deploy.** It is `restrict`ed to `deploy.sh`, whose forced-command grammar accepts exactly `deploy <sha> <digest>`; every other command exits 2 before touching Docker. The owner's subcommands need a root shell.
- **Image provenance is checked before anything runs.**
  - Only the digest the `main` build produced is pulled.
  - Its revision label must equal the SHA, it may declare no volumes, and it must fit under the size cap.
  - Base images are pinned by digest and actions by commit SHA. Deploy secrets live in a `production` environment limited to `main`, and top-level `permissions: {}` keeps every token minimal.
- **The registry token is short-lived.** It arrives on stdin with the actor's name, is used with a throwaway Docker config, and is logged out and deleted before the service changes. It never appears in `argv`, in root's Docker config, or on disk after the run.
- **Host key pinning.** `known_hosts` is pinned from a fingerprint the owner checked, so CI never uses `StrictHostKeyChecking=no`.
- **The container is confined, but it holds Atelier's secrets.**
  - It runs as uid 10001 with a read-only root filesystem, all capabilities dropped and `no-new-privileges`. It binds to 127.0.0.1 only, on its own network, with egress to cloud metadata and the tailnet blocked, and it has no Docker socket, no Folio volume or env, and memory and CPU limits.
  - It still reads Atelier's data and holds the runtime Modal token, which is why provenance matters.
- **Residual risk: host root.**
  - Anyone with root on `folio-prod-1` can read `/mnt/atelier-data` (the plaintext images and prompts), `/opt/atelier/.env` (the Modal runtime token) and `/opt/atelier/backup.env` (the restic password and R2 keys).
  - Root-capable parties include the operator, any root key that stays unrestricted, and Tailscale SSH if it is enabled.
  - The D1 audit and the LearnFlow restriction shrink that set; they do not remove it. This residual risk comes with the contract's choice of a shared host.
- **Modal tokens are workspace-wide (D2).** The runtime token lives only in `/opt/atelier/.env` (0600); the CI token only in the `production` environment, exposed to the single `modal deploy` step. A workspace spend limit bounds the cost of a leak. The legacy web endpoint is gone and its proxy-auth tokens are revoked.
- **Scope on shared systems.** Folio is untouched apart from the one ingress rule and its backup. LearnFlow changes only as decided in D1: one restricted key line and its deploy workflow.

## Next Steps

- Phase 5 adds nightly encrypted backups of `/mnt/atelier-data` and the backup badge; its restore runbook uses `deploy.sh stop` and `start`.
- Phase 6 adds GPU status, warm-up and stop, verified live on this deployment.
