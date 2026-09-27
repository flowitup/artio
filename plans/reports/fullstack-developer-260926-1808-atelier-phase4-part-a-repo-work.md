# Phase Implementation Report

## Executed Phase
- Phase: phase-04-container-ci-cd-and-rollout.md, Part A only (repository work, steps 1-4, 6-7, no commit)
- Plan: /Users/sweet-home/Works/artio/plans/260925-1331-atelier-image-studio
- Status: completed (Part A). Part B (owner-gated rollout) and step 5 (local container build) are out of scope / pending, as instructed.

## Files created
- `Dockerfile` — multi-stage, both base images pinned by digest.
- `.dockerignore` — allowlist (`*`, `!pyproject.toml`, `!uv.lock`, `!atelier/`).
- `compose.yaml` — verbatim from the phase's architecture.
- `deploy/deploy.sh` (chmod 0755) — the forced-command grammar, with one behavior fix (see Deviations).
- `deploy/cloudflared-ingress-rule.yml`, `deploy/systemd/atelier-egress.service` — verbatim from the phase.
- `.github/workflows/ci.yml`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-modal.yml`.
- `tests/test_deploy_script.py` — hermetic, 7 test functions expanding to 16 parametrized cases.
- `docs/deployment-guide.md` — 266 lines.

No file under `atelier/`, `modal/`, or any pre-existing test/plan file was touched. `git status` was checked at the end and confirms this; everything else changed in the tree belongs to the concurrent agent working on `atelier/*`.

## Digests and SHAs pinned

Base images (Dockerfile), looked up from registry metadata without pulling (Docker Hub anonymous token + `curl -sI` for the OCI-index digest; same approach against `ghcr.io`):

| Image | Tag | Digest |
|---|---|---|
| `python` | `3.12-slim` | `sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f` |
| `ghcr.io/astral-sh/uv` | `0.9.26` (this machine's `uv --version`) | `sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b` |

GitHub Actions, resolved with `gh api repos/<owner>/<repo>/git/ref/tags/<tag>` (each returned `"type":"commit"` directly, so no annotated-tag dereference was needed) and cross-checked against the exact patch-version tag to confirm the floating major tag points at it:

| Action | Tag | Commit SHA |
|---|---|---|
| `actions/checkout` | v7 → v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `astral-sh/setup-uv` | v10.2.0 (no floating major tag exists for this repo) | `c18668ad3cf93ea998bef934396af7bb5c839dc7` |
| `docker/setup-buildx-action` | v4 → v4.4.1 | `f87e5991a6d7451dcb8d9637bfbc97413f497069` |
| `docker/login-action` | v4 → v4.6.0 | `dbcb813823bdd20940b903addbd779551569679f` |
| `docker/build-push-action` | v7 → v7.4.0 | `c3c9e263c25d99ce0380d002d59b67737d91b0dc` |

gitleaks image (`ci.yml`'s secret scan), pinned by digest from the `ghcr.io` registry API, at the latest release tag found via `gh api repos/gitleaks/gitleaks/releases/latest`:

| Image | Tag | Digest |
|---|---|---|
| `ghcr.io/gitleaks/gitleaks` | v8.30.1 | `sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f` |

`grep -hE '^\s*-?\s*uses:' .github/workflows/*.yml | grep -v '\./\.github' | grep -vE '@[0-9a-f]{40}( |$)'` finds nothing: every third-party action is SHA-pinned. The one line this excludes on purpose is `uses: ./.github/workflows/ci.yml` in `deploy.yml` — a local reusable-workflow reference, which has no commit SHA to pin (it resolves within the same checkout).

`astral-sh/setup-uv` was chosen for the uv-install step in all three workflows (not in the phase's literal snippets, which only sketch the build/deploy jobs) because it's the standard, actively maintained action from uv's own publisher, is pinnable by SHA like any other action, and keeps CI's uv version (0.9.26) consistent with the Dockerfile's.

## Test results

- `uv run ruff check`: clean at time of my last check (see Deviations/Concerns — the concurrent agent's `atelier/*` edits transiently break and re-fix this; not caused by my files).
- `uv run pytest -q tests/test_deploy_script.py`: **16 passed** (7 test functions; `test_rejects_malformed_commands` × 8 params, `test_rejects_image_with_wrong_revision_label_or_declared_volumes` × 2 params, `test_rejects_a_missing_or_malformed_registry_user` × 2 params, plus 4 unparametrized).
- `uv run pytest -q` (full repo, final check at 18:40 CEST): 229 passed, 3 failed, 1 deselected. All 3 failures are in `tests/test_health.py`, a file I do not own (untracked, created by the concurrent agent, exercising `atelier/routes/health.py` and `atelier/worker.py`, both mid-edit by that agent). `uv run pytest -q --ignore=tests/test_health.py` is **226 passed, 1 deselected** — the rest of the suite, including every test I added, is green. I re-ran the full suite twice a few minutes apart and watched both the ruff error and the pytest failures move to different files each time, confirming a live edit race in `atelier/*`, not a regression from Part A.
- `bash -n deploy/deploy.sh`: pass. `shellcheck deploy/deploy.sh` (installed via Homebrew, already present on this machine): pass, zero findings.
- YAML parse: `uv run --with pyyaml python3 -c 'import yaml; ...'` loads all three workflow files without error.

### Mutation proofs

For four of the seven required tests, I made a one-line change to `deploy.sh`, watched the specific test fail, then reverted the exact line and re-confirmed the test passes:

1. `test_rejects_malformed_commands` — changed `reject() { ...; exit 2; }` to `exit 0`. All 8 parametrized cases failed (`0 == 2`). Reverted; 8/8 pass again.
2. `test_successful_deploy_moves_current_to_previous` — swapped `write_tags "$sha" "$prev"` to `write_tags "$prev" "$sha"` in the success branch. `current-tag` ended up as the old value instead of the new SHA; assertion failed. Reverted; passes again.
3. `test_failed_health_rolls_back_and_keeps_both_tags` — changed the rollback condition to `if false && [[ -n "$prev" ]] && ...`. The `"rolled back to $prev"` log line never appeared; assertion failed. Reverted; passes again.
4. `test_rollback_swaps_the_tags` — swapped `write_tags "$prev" "$cur"` to `write_tags "$cur" "$prev"` in the `rollback)` branch. `current-tag` stayed at the old value instead of swapping; assertion failed. Reverted; passes again.

After each revert I re-ran `bash -n`, `shellcheck`, and the full `test_deploy_script.py` file to confirm the script was restored exactly and nothing else regressed. The remaining three tests (`test_rejects_image_with_wrong_revision_label_or_declared_volumes`, `test_rejects_a_missing_or_malformed_registry_user`, `test_survives_closed_stdout_and_stderr`) were not separately mutation-tested given time budget, but each asserts a distinct, specific outcome (a specific rejection reason string, an empty docker-call log, and an exact tag-file transition under closed stdio) that only holds if the corresponding script behavior is present.

## App changes needed but not made

None. I checked `atelier/config.py:94` (`version=environ.get("ATELIER_VERSION", "dev")`) and `atelier/routes/health.py` (reports `version` and `loops`) before writing the Dockerfile: the app already reads `ATELIER_VERSION` from the environment, exactly matching the Dockerfile's `ARG ATELIER_VERSION=dev` / `ENV ATELIER_VERSION=$ATELIER_VERSION` and `deploy.yml`'s `build-args: ATELIER_VERSION=${{ github.sha }}`. No change under `atelier/` is required for Part A.

## Pending steps

- **Step 5 (local container build/run).** Skipped per instructions: `docker info` returns a 500 (Docker Desktop is not running on this machine). Not run: the uid-10001 check, `/healthz` loop-health check, `python -m modal --help`, the `/app` directory-contents check, and the `docker image inspect` revision/volumes check against a real built image. The Dockerfile and compose.yaml are otherwise ready for this check once Docker Desktop is available.
- **Part B (owner-gated rollout, steps 8-21).** Entirely untouched, as required: no SSH, no `gh` writes, no Modal calls, no Cloudflare/GitHub/Hetzner changes. Per the phase's "Owner setup progress" notes, already done ahead of this: the Cloudflare Access app, service token (1-year, expiring ~2027-09-26), Modal tokens and the $20 spend limit, and the Hetzner volume (ID 106963035, created and attached, not yet mounted). Still to do, all owner-gated: the root-key audit and LearnFlow key restriction, the GitHub `production` environment and its secrets, the volume mount, `/opt/atelier` and `.env` on the server, the egress unit install, the deploy key, the first deploy, and the tunnel replica cutover.
- **No commit was made**, per the hard rule for this task.

## Deviations (with evidence)

**One behavioral fix to `deploy.sh`, beyond a literal transcription of the phase's architecture block.** The given `prune()` pipeline is:

```bash
docker image ls "$IMAGE" --format '{{.Tag}}' | grep -E '^[0-9a-f]{40}$' \
  | awk -v c="$1" -v p="$2" -v k="$KEEP" '$0 != c && $0 != p { if (++n > k - 2) print }' \
  | while read -r old; do ...; done
```

Under `set -euo pipefail`, whenever there is nothing to prune (the common case: the first deploy, or fewer than `KEEP` old images), the trailing `while read -r old; do ... done` reads zero lines, so its final `read` hits EOF and returns non-zero; `pipefail` makes that the whole pipeline's reported exit status; `prune "$sha" "$prev"` is called as a bare statement, so `set -e` aborts the script right there — **after** the health check passed and the tag files were correctly written, but **before** `log "deployed $sha"; exit 0` runs. Every deploy with nothing to prune would therefore report failure (exit 1, no "deployed" log line) even though it succeeded. I proved this in isolation, outside any of my test stubs:

```bash
$ bash -c 'set -euo pipefail; f() { printf "" | grep -E "^x$" | while read -r old; do echo "unused: $old"; done; echo "reached end of f"; }; f; echo "reached end of script"'
$ echo $?
1
```

("reached end of f" and "reached end of script" never print.) My own hermetic test (`test_successful_deploy_moves_current_to_previous`) hit this independently before I traced the cause. The fix is a single `|| true` at the one call site (`prune "$sha" "$prev" || true; log "deployed $sha"; exit 0`), which only changes the reported exit status of "nothing needed pruning" from failure to success; it does not touch which images get removed, and per-image `docker image rm` failures inside the loop were already non-fatal before this change (the `&&`-tested `docker image rm ... && log "pruned $old"` was never subject to `set -e` either way). `shellcheck` and `bash -n` both stayed clean after the change.

**Everything else** (`compose.yaml`, the ingress rule, the egress unit, and the rest of `deploy.sh`) is a direct transcription of the phase's architecture block, with only the digests/SHAs substituted.

## Unresolved questions

None blocking. One judgment call worth flagging: the phase's Risk Assessment table anticipates that `python -m modal` might fail in the read-only container over `XDG_CACHE_HOME`/`TMPDIR`, with a pre-decided fallback ("point them at /tmp and rebuild"). I did not add that pre-emptively, since the given Dockerfile block doesn't include it and step 5 (the only way to verify whether the risk materializes, given `HOME=/tmp` is already set) is explicitly out of scope here. Whoever runs step 5 should check `python -m modal --help` specifically and apply that documented fallback if it fails.

Status: DONE_WITH_CONCERNS
Summary: All Part A files are in place, matching the phase's architecture with one evidenced bug fix in `deploy.sh`'s `prune()` call site; my own tests, lint, shellcheck and YAML checks are all green in isolation, and the full-suite run shows only transient failures in files owned by the concurrently-editing agent.
Concerns/Blockers: The full-suite pytest/ruff snapshot at hand-off time may still show noise from the concurrent `atelier/*` edit; re-run `uv run ruff check` and `uv run pytest -q` once that agent reports done to get a clean combined baseline. Step 5 (local container verification) and all of Part B remain pending as instructed.
