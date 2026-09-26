# Code review: container, deploy and CI files (uncommitted, 2026-09-26)

**Reviewer:** an independent code-reviewer subagent. It read the source, ran behavioral probes (real `flock`, broken pipe, realistic `image ls`, stale loops) and 22 mutations of `deploy.sh`, and verified the pins with read-only GitHub and registry GETs. No SSH, Docker or Modal calls.

**Baseline:** 16 deploy-script tests passed, and `bash -n` plus shellcheck 0.11.0 were clean. No Critical or High findings.

**What held:**
- the forced-command parsing (no eval or glob);
- the token passed on stdin to a 0600 throwaway config that is logged out and removed;
- the pull by digest, followed by the revision-label, volumes and size checks;
- health requiring both the version and `"loops":"ok"`;
- rollback that doesn't touch the tag files;
- prune limited to Atelier refs, keeping 3;
- detachment through `systemd-run --wait --pipe` (PID 1 parent, `IgnoreSIGPIPE`);
- `permissions: {}` with minimal per-job grants, `persist-credentials: false`, `production` only on the deploy jobs, and no build-skip input;
- all 5 action SHAs and 3 image digests match their tags;
- `compose.yaml` and the egress unit are byte-identical to the contract.

**Server facts gathered afterwards** (read-only, 2026-09-26):
- sshd `AcceptEnv` is limited to `LANG`, `LC_*`, `COLORTERM` and `NO_COLOR`, with `PermitUserEnvironment no`.
- No Docker credential helpers are installed.
- Docker 29 uses the **containerd image store** (`io.containerd.snapshotter.v1`), and `live-restore=false`.
- Tailscale has `RouteAll=false` and no advertised routes.
- No Docker network uses 172.30.0.0/16.

| ID | Sev | Finding | Disposition |
|---|---|---|---|
| M1 | Medium | `stop` releases the lock, so a CI deploy can bring Atelier up in the middle of a restore. | **Accept.** `stop` writes a maintenance marker, `apply` refuses (exit 75) while it exists, and `start` clears it after a healthy start. The restore runbook also disables `deploy.yml`. |
| M2 | Medium | The size cap is checked only after the full pull, and rejected or failed images and orphaned digests are never removed from Folio's shared disk. | **Accept.** Check the size from the registry manifest before pulling (independent of the containerd store). Remove `$IMAGE@digest` on any reject. Prune on the failure path too, including untagged Atelier images. |
| M3 | Medium | The malformed-command tests pass because stdin is empty, not because of the regexes. | **Accept.** Send a valid token and user in every case, assert the exact rejection message, and add a 41-hex SHA, `sha256:<64>x` and a newline-suffixed command. |
| M4 | Medium | The `loops` check, the lock, prune's selection, keeping the token out of argv, the size cap and the manual-rollback restore are untested (19 of 22 mutations survive). | **Accept.** Add tests; the mutation harness should then catch all 22. |
| L1 | Low | `read` accepts extra lines, and `ATELIER_DEPLOY_DIR` is trusted on the forced-command path. | **Accept.** Match the whole string with `BASH_REMATCH`, and ignore the override when `SSH_ORIGINAL_COMMAND` is set. The audit's sshd grep also checks `acceptenv` and `permituserenvironment`. |
| L2 | Low | Compose and prune errors never reach the journal, and the `\|\| true` rationale was wrong. | **Accept.** Pipe compose output to `logger -t atelier-deploy` and use `prune \|\| log …`. |
| L3 | Low | Compose runs without `-f`/`-p`. | **Accept.** Add a `dc()` helper. |
| L4 | Low | An apply killed by SIGTERM reports success, and the manual verbs aren't detached. | **Accept.** Use `-p Type=oneshot` plus a trap, and wrap the manual verbs in `systemd-run`. |
| L5 | Low | Egress unit: a failed first rule is masked, only FORWARD is covered, and there's a short window at boot. | **Accept in part.** Use one `ExecStart` per rule with `iptables -w`, add `-o tailscale0` and an INPUT rule for 100.64.0.0/10. Docker's startup ordering on Folio's host stays unchanged, and the brief window at boot before the rules exist is documented as a residual risk. |
| L6 | Low | `.dockerignore` has no effect on the Git-context build, and BuildKit runs from a mutable tag. | **Accept.** Use `context: .`, and pin the BuildKit image by digest (or drop setup-buildx). |
| L7 | Low | `modal deploy` has no concurrency group, and there are no job timeouts or SSH keepalives. | **Accept.** |
| L8 | Low | `/app` also ships `pyproject.toml` and `uv.lock`, and pinned digests never get updates. | **Accept.** Copy only `.venv` and `atelier`, and add `.github/dependabot.yml` for the docker and github-actions ecosystems. |
| L9 | Low | The broken-pipe test uses EBADF, the `logger` stub is unused (tests write to the real syslog), and `stop`, `start` and `status` are untested. | **Accept.** |
| L10 | Low | Guide errors: the cap "warns" (it refuses), the token rotation steps are wrong, the egress install path is wrong, and some runbooks are missing. | **Accept.** |

## Fix verification (2026-09-26)

A fullstack-developer agent applied every accepted row above. Its report is `plans/reports/fullstack-developer-260926-1808-atelier-deploy-ci-review-fixes.md`. The controller then re-checked the work and found three more problems, all now fixed:

| Problem | Evidence | Fix |
|---|---|---|
| `deploy.yml`'s `test` job set `timeout-minutes` on a reusable-workflow call. GitHub rejects that key there, so the whole workflow file would have been invalid and no deploy would ever start. | `actionlint` syntax-check error | Removed. The called `ci.yml` job keeps its own 15-minute timeout. |
| `detach()` copied `ATELIER_DEPLOY_DIR` from the caller's environment into the transient unit, and the unit trusts that variable. The forced-command path therefore did not fully ignore the override, despite what its comment claimed. It isn't exploitable today, because sshd forwards no client environment. | A mutant with the old behavior survived every existing test | The unit now always receives the directory the script already resolved. A new test proves an environment value reaches neither phase, and the matching mutant is caught. |
| The size check before the pull used `docker buildx imagetools`, and folio-prod-1 has no buildx: it runs Ubuntu's own `docker.io` 29.1.3 package. On this host the check would always have been skipped. | Read-only server check: `docker: unknown command: docker buildx`; `jq` 1.8.1 is present | It now uses the CLI's built-in `docker manifest inspect`, which the host has. A new test follows an image index (the linux/amd64 image plus a provenance attestation) to the right manifest. |

Checks after the fixes:
- `ruff check` is clean, and 309 tests pass (1 live test deselected), including 40 deploy-script tests.
- `shellcheck`, `bash -n` and `actionlint` are clean.
- The mutation harness catches 21 of 21 mutants. Three are new: the environment-trust detach, a removed registry size check, and an index lookup that takes the first manifest. Four of the original 22 were retired because the code they mutated was deleted once the whole-string forced-command regex made it unreachable.

Other host facts confirmed read-only: `logger`, `flock`, `systemd-run` (systemd 259) and `curl` are at `/usr/bin`, and Compose is `docker-compose-v2` 2.40.3.

Still open: the local container check (plan step 5) waits for Docker Desktop, whose VM stopped responding on 2026-09-25.
