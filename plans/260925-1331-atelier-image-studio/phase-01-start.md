---
phase: 1
title: "Repo bootstrap & Modal backend"
status: completed
priority: P1
effort: "3h"
dependencies: []
---

# Phase 1: Repo bootstrap & Modal backend

## Context Links

<!-- Updated: Validation Session 1 - Modal script extras allowed -->

- Contract, as amended after validation. Amendment 8 allows exactly three changes to the Modal script: a `ping()` that probes ComfyUI, removal of the legacy `api` web endpoint, and nothing that alters the model, workflow or sampler. See the [brainstorm report](../reports/brainstorm-260925-1507-atelier-image-studio-hetzner.md).
- Brief §1–2 (repo layout), §9 item 1: [architecture brief](./reports/architecture-brief.md)
- Warm-up and `ping()` semantics: [research-02 §3](./research/researcher-02-modal-sdk-app-plugin.md)
- Red-team findings applied here: [failure modes](./reports/red-team-failure-mode-analyst.md) (Finding 9), [security](./reports/red-team-security-adversary.md) (Findings 5 and 6), [scope](./reports/red-team-scope-complexity-critic.md) (README line 9)
- Existing code:
  - `qwen21_uc_app.py`:
    - image definition at :46-54, whose `.pip_install("requests", "fastapi[standard]")` is at :53;
    - `build_workflow` at :85-96;
    - class config at :99-100 (`timeout=1800`, `max_containers=1`, `max_inputs=4`, `scaledown_window=60`);
    - `start()` polls ComfyUI's `/system_stats` at :111-117;
    - `run_workflow` at :147-150;
    - the legacy `api` endpoint at :152-157.
  - `test_deployed.py` at :1-11 (it writes to `Path(__file__).parent / "out"` at :9).
  - `README.md`: command block at :7-13, with the moved paths on lines 8, 9 and 10; the HTTP endpoint section at :22-23.
- Verified in Validation Session 1:
  - `fastapi` is used only by the `api` endpoint (`:53` and `:152-157`), so it can leave the image.
  - `requests` is still used by `start()` and `_run` (`:104-137`).
  - `/system_stats` is the endpoint `start()` already relies on (`:113`).

## Overview

<!-- Updated: Validation Session 1 - Modal script extras allowed -->

When this phase is done, the project folder is a git repository on `main` that ignores `logs/`, `out/` and image files under `plans/`. It is a uv project where `ruff` and `pytest` pass offline.

The Modal backend lives at `modal/qwen21_uc_app.py` with the three validated changes:
- a `ping()` that answers `"ok"` only while ComfyUI responds;
- the legacy `api` web endpoint removed;
- `fastapi[standard]` dropped from the image.

The model, workflow and sampler are unchanged. The backend is redeployed on Modal, answers `ok`, and no longer serves the old web URL. Priority P1, because every later phase builds on the repo, the toolchain and `ping()`.

## Key Insights

<!-- Updated: Red Team 2026-09-25 - F15 GPU status model -->
<!-- Updated: Red Team 2026-09-25 - F12 Modal tokens -->
<!-- Updated: Validation Session 1 - Modal script extras allowed -->

- **`test_deployed.py` spends money if pytest collects it.** It matches pytest's default `test_*.py` pattern, and every call renders on the GPU. Moving it to `modal/smoke_test.py` and setting `testpaths = ["tests"]` removes the trap.
- **`modal/` must never get an `__init__.py`.** Without one, Python treats `modal/` only as a namespace-package candidate. The installed `modal` package is a regular package and always wins the import, so `import modal` still works from the repo root.
- **The move breaks the smoke test's output path.** After the move, `Path(__file__).parent / "out"` (`test_deployed.py:9`) would point at a missing `modal/out/`, so the move must also point it at the repo-root `out/`.
- **`ping()` is a ComfyUI health probe.**
  - It calls `GET http://127.0.0.1:8188/system_stats`, returns `"ok"` only on HTTP 200, and raises otherwise, so the ping call fails.
  - `@modal.enter()` already waits for that same endpoint before the container accepts inputs (`:111-117`). A failing ping therefore means ComfyUI died after boot, which is exactly the case phase 6 must detect.
  - Phase 6 shows "warm" only after a successful ping, and treats a failed ping as "unhealthy".
- **A cold `ping()` costs a full boot.** `@modal.enter()` runs once per new container (research-02 §3), so a ping on a cold backend pays the ~68 s ComfyUI boot plus the 60 s idle tail, about $0.07 at $0.000542/s. On a warm container it checks ComfyUI and resets the idle timer.
- **The legacy web endpoint goes, with its dependency.**
  - The `api` method (`:152-157`) is the only user of `fastapi`, so `.pip_install("requests", "fastapi[standard]")` becomes `.pip_install("requests")`.
  - Redeploying removes the endpoint's public URL. Phase 4 then revokes the proxy-auth tokens that guarded it.
  - ComfyUI's own requirements are installed by the earlier `pip install -r requirements.txt` layer and are unaffected.
- **Only the image's last layer changes.** The redeploy rebuilds the final `pip_install` layer; the apt, ComfyUI clone and requirements layers stay cached. [UNVERIFIED]: a longer rebuild would only cost time.

## Requirements

<!-- Updated: Red Team 2026-09-25 - F11 image data control -->
<!-- Updated: Red Team 2026-09-25 - minor fixes -->
<!-- Updated: Validation Session 1 - Modal script extras allowed -->

Functional:
- **Git:** the repo is initialized on branch `main`. `.gitignore` excludes:
  - `out/`, `logs/`, `data/`, `.env` and `.env.*`;
  - `__pycache__/`, `.venv/`, `.pytest_cache/` and `.ruff_cache/`;
  - `*.plugin` and `.DS_Store`;
  - `plans/**/*.png`, `*.jpg`, `*.jpeg` and `*.webp`, so acceptance evidence and generated images can never be committed.
- **uv project:**
  - It is named `atelier`, with `package = false` because it is run from source and never built as a wheel.
  - It targets Python 3.12.
  - Runtime deps are fastapi, uvicorn[standard], jinja2, python-multipart, `modal>=1.5.5,<1.6`, pyjwt[crypto], pillow and tzdata. These are the Atelier app's dependencies, not the Modal image's. Dev deps are pytest, ruff and httpx.
- **Modal script:** `modal/qwen21_uc_app.py` equals today's script with exactly three changes:
  - a new `@modal.method() def ping(self) -> str` that probes ComfyUI as in Architecture;
  - the `api` method and its `@modal.fastapi_endpoint` decorator deleted;
  - `fastapi[standard]` removed from the image's `pip_install`.

  `build_workflow`, the model files, the sampler settings, `generate`, `run_workflow`, `download_models`, the local entrypoint and the docstring stay as they are.
- **Smoke test:** `modal/smoke_test.py` equals today's `test_deployed.py`, with the output directory pointed at the repo-root `out/` (created if missing) and its one-line `import time, modal` split so ruff passes.
- **README:** every moved command points at the new paths (lines 8, 9 and 10), and the "HTTP: POST …modal.run" section (lines 22–23) is deleted. The full rewrite happens in phase 8.

Non-functional:
- `uv run ruff check` and `uv run pytest` pass with no network and no Modal credentials.
- `logs/token_new.log` is never opened, printed, staged or committed.

## Architecture

<!-- Updated: Validation Session 1 - Modal script extras allowed -->

```
qwen21-uc-modal/                  (git repo, branch main)
├── .gitignore  .python-version  pyproject.toml  uv.lock  README.md
├── modal/                        (NO __init__.py, never copied into the app image)
│   ├── qwen21_uc_app.py          (deployed with `modal deploy`; ping() probe, no web endpoint)
│   └── smoke_test.py             (manual, spends GPU time)
├── atelier/__init__.py           (app package, filled by the engine work)
└── tests/                        (conftest.py loads the backend script by path)
```

The warm-up call path used later is `Cls.from_name("qwen21-uc", "Qwen21UC")().ping`. On a warm container it checks ComfyUI and resets the 60 s scaledown timer. On a cold backend it triggers the normal `@modal.enter()` boot first.

```python
    @modal.method()
    def ping(self) -> str:
        """Warm-up probe: succeeds only while ComfyUI answers, so "warm" means ComfyUI is ready."""
        import requests
        response = requests.get(f"{self.base}/system_stats", timeout=5)
        if response.status_code != 200:
            raise RuntimeError(f"ComfyUI is not answering: /system_stats returned HTTP {response.status_code}")
        return "ok"
```

- The method goes directly after `run_workflow` (today's line 150), where the deleted `api` method was.
- A connection error from `requests` also propagates, so any ComfyUI failure fails the call.
- The image line becomes `.pip_install("requests")`.

## Related Code Files

<!-- Updated: Validation Session 1 - Modal script extras allowed -->

Create:
- `/Users/sweet-home/Works/atelier/.gitignore`
- `/Users/sweet-home/Works/atelier/.python-version` (contains `3.12`)
- `/Users/sweet-home/Works/atelier/pyproject.toml`
- `/Users/sweet-home/Works/atelier/uv.lock` (generated by `uv lock`)
- `/Users/sweet-home/Works/atelier/modal/qwen21_uc_app.py` (moved from the root, with the three validated changes)
- `/Users/sweet-home/Works/atelier/modal/smoke_test.py` (moved from `test_deployed.py`)
- `/Users/sweet-home/Works/atelier/atelier/__init__.py` (docstring only)
- `/Users/sweet-home/Works/atelier/tests/conftest.py` (`backend_script` fixture)
- `/Users/sweet-home/Works/atelier/tests/test_modal_backend_script.py`

Modify:
- `/Users/sweet-home/Works/atelier/README.md`: the command block (lines 8–10), and the HTTP section deleted (lines 22–23).

Delete (moved with `git mv`):
- `/Users/sweet-home/Works/atelier/qwen21_uc_app.py`
- `/Users/sweet-home/Works/atelier/test_deployed.py`

## Implementation Steps

<!-- Updated: Red Team 2026-09-25 - minor fixes -->
<!-- Updated: Validation Session 1 - Modal script extras allowed -->

1. **Pre-flight.** Confirm the folder is not yet a repo: `git -C /Users/sweet-home/Works/atelier rev-parse` must fail. Do not list, open or cat anything under `logs/`.
2. **Initialize.** Run `git init -b main` and write `.gitignore` with the entries from Requirements. Confirm the ignores with two separate checks, `git check-ignore -q logs/` and `git check-ignore -q out/`. With several paths, `git check-ignore` succeeds if any one is ignored, so the checks must be separate. They check paths without reading any file.
3. **Record the original state.** Stage `.gitignore`, `qwen21_uc_app.py`, `test_deployed.py`, `README.md`, `plans/` and `.claude/`, then run the tracked-content secret check from Verification.
   - If the check is clean, commit as `chore: import existing Modal backend and planning docs`.
   - This keeps the original script in history, so the next diff shows only the validated changes.
4. **Create the uv project.** Write `.python-version` and `pyproject.toml`:

   ```toml
   [project]
   name = "atelier"
   version = "0.1.0"
   requires-python = ">=3.12,<3.13"
   dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "jinja2>=3.1", "python-multipart>=0.0.9",
                   "modal>=1.5.5,<1.6", "pyjwt[crypto]>=2.9", "pillow>=10.4", "tzdata>=2024.1"]

   [dependency-groups]
   dev = ["pytest>=8.3", "ruff>=0.6", "httpx>=0.27"]

   [tool.uv]
   package = false

   [tool.ruff]
   target-version = "py312"
   line-length = 110

   [tool.ruff.lint]
   extend-select = ["I"]

   [tool.pytest.ini_options]
   testpaths = ["tests"]
   pythonpath = ["."]
   addopts = "-m 'not live'"
   markers = ["live: calls the real Modal backend and spends GPU money; run only with the owner's go-ahead"]
   ```

   Then run `uv lock && uv sync`.
5. **Move the backend.** Run `mkdir modal`, then `git mv qwen21_uc_app.py modal/qwen21_uc_app.py` and `git mv test_deployed.py modal/smoke_test.py`.
6. **Apply the three script changes.**
   - Delete the `api` method with its `@modal.fastapi_endpoint` decorator.
   - Insert `ping()` from Architecture in its place.
   - Change the image's `.pip_install("requests", "fastapi[standard]")` to `.pip_install("requests")`.
   - Make no other edit.
7. **Fix the smoke test.**
   - Split `import time, modal` into two imports.
   - Replace the output path with `out_dir = Path(__file__).resolve().parent.parent / "out"`, followed by `out_dir.mkdir(exist_ok=True)`.
   - Keep the renders and prompt as they are.
8. **Add the package and fixture.**
   - Create `atelier/__init__.py` containing only a module docstring.
   - Create `tests/conftest.py` with a session-scoped `backend_script` fixture. It loads `modal/qwen21_uc_app.py` through `importlib.util.spec_from_file_location("qwen21_uc_app", path)`. The engine's parity test reuses it.
9. **Write `tests/test_modal_backend_script.py`:**
   - `test_backend_class_exposes_ping_generate_and_run_workflow` parses the script with `ast` and asserts that class `Qwen21UC` defines the methods `ping`, `generate` and `run_workflow`, each decorated with `modal.method`.
   - `test_backend_serves_no_web_endpoint` asserts that no method carries a `fastapi_endpoint` decorator and that the script source no longer mentions `fastapi`.
   - `test_ping_probes_comfyui_system_stats` asserts from the `ast` that `ping` calls `requests.get` on a URL ending in `/system_stats` and contains a `raise`.
   - `test_build_workflow_still_builds_the_eight_node_graph` asserts that the loaded module's `build_workflow("p", 1088, 1920, 25, 7)` has node keys `"1"` to `"8"` and a KSampler seed of 7.
10. **Update README.**
    - `README.md:8` becomes `modal deploy modal/qwen21_uc_app.py`.
    - `README.md:9` becomes `modal run modal/qwen21_uc_app.py::download_models`.
    - `README.md:10` becomes `python modal/smoke_test.py`.
    - Delete the "HTTP: POST …modal.run" lines (22–23), because the endpoint no longer exists.
11. **Lint and test.** Run `uv run ruff check` and `uv run pytest -q`.
    - If ruff flags `modal/qwen21_uc_app.py`, add a `[tool.ruff.lint.per-file-ignores]` entry for exactly those rule codes. Never edit the script to satisfy lint.
12. **Commit** as `refactor: move Modal backend under modal/, probe ComfyUI in ping, drop the legacy web endpoint`.
13. **[OWNER-GATED] Deploy.**
    - Ask the owner before running `uv run modal deploy modal/qwen21_uc_app.py` from the laptop; it uses the owner's `~/.modal.toml`. The deploy spends no GPU time.
    - Confirm the output no longer lists a web endpoint URL.
14. **[OWNER-GATED] Ping smoke call.** Ask the owner before running it; it spends about $0.07:
    `uv run python -c "import modal; print(modal.Cls.from_name('qwen21-uc','Qwen21UC')().ping.remote())"`.
    - Expect `ok` after about 70 s.
    - Then run `uv run modal container list --json` once right away and again after 90 s. The second run must return `[]`.

## Todo List

- [x] Initialize the git repo on `main`, write `.gitignore` (including image files under `plans/`), and confirm `logs/` and `out/` are ignored with two separate checks
- [x] Commit the original files after a clean tracked-content secret check
- [x] Create `pyproject.toml`, `.python-version` and `uv.lock`, and run `uv sync`
- [x] Move the Modal script and smoke test under `modal/` with `git mv`
- [x] Apply the three script changes: ComfyUI-probing `ping()`, `api` endpoint deleted, `fastapi[standard]` dropped from the image
- [x] Fix the smoke test's output path and imports
- [x] Add `atelier/__init__.py`, `tests/conftest.py` and `tests/test_modal_backend_script.py`
- [x] Update the README: three moved commands (lines 8–10), HTTP section deleted
- [x] Get `uv run ruff check` and `uv run pytest` green, then commit
- [x] [OWNER-GATED] `modal deploy` from `modal/`; no web endpoint listed
- [x] [OWNER-GATED] Ping smoke call returns `ok`, and containers return to 0

## Success Criteria

<!-- Updated: Validation Session 1 - Modal script extras allowed -->

- `git ls-files | grep -E '^(logs|out)/'` prints nothing, and `git check-ignore -q plans/x.png` succeeds. Supports criterion 14.
- `git diff HEAD~1 -M -- modal/qwen21_uc_app.py` shows exactly the three validated changes: `ping()` added, `api` deleted, and `fastapi[standard]` removed from `pip_install`. `build_workflow`, the model files and the sampler settings are untouched (contract amendment 8).
- `uv run ruff check` and `uv run pytest -q` exit 0 offline.
- The deployed backend answers `ok`, and `modal app list --json` shows `qwen21-uc` with state `deployed`. `modal container list --json` returns `[]` within about 90 s of the ping, which is the fail-safe basis for criterion 6.
- The deploy output lists no web endpoint.

## Verification

```bash
cd /Users/sweet-home/Works/atelier
git check-ignore -q logs/ && echo "logs/ ignored"
git check-ignore -q out/ && echo "out/ ignored"
git check-ignore -q plans/example.png && echo "plan images ignored"
git ls-files | grep -E '^(logs|out)/' ; test $? -eq 1 && echo "nothing tracked under logs/ or out/"
# Tracked-content secret check: reads only files git tracks, never logs/.
git grep -nE 'ak-[A-Za-z0-9]{16,}|as-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_|BEGIN [A-Z ]*PRIVATE KEY' ; test $? -eq 1 && echo "no token patterns"
uv run ruff check
uv run pytest -q
git diff HEAD~1 -M -- modal/qwen21_uc_app.py        # ping() added, api deleted, fastapi dropped; nothing else
grep -n "fastapi" modal/qwen21_uc_app.py ; test $? -eq 1 && echo "no fastapi left in the Modal script"
# [OWNER-GATED] each of the next three commands
uv run modal deploy modal/qwen21_uc_app.py                                          # lists no web endpoint
uv run python -c "import modal; print(modal.Cls.from_name('qwen21-uc','Qwen21UC')().ping.remote())"   # ok
uv run modal app list --json && sleep 90 && uv run modal container list --json      # expect state deployed, then []
```

The red team verified the `ak-`/`as-` token prefixes (`modal/config.py:25-26`). The grep is still a guard, not proof; CI adds a full scanner in phase 4.

### Verification notes (2026-09-26, owner-approved live steps)
- `git grep` across every commit found no token patterns, and nothing under `logs/` or `out/` was ever tracked. The repo was pushed as the private `flowitup/atelier` at the owner's request.
- `uv run ruff check` passed (per-file ignores `I001`, `S110`, `BLE001` for the untouched backend script), and `uv run pytest -q` passed 6 tests.
- The rename diff shows exactly the three validated changes: 9 lines added, 7 removed.
- `modal deploy` finished in 4.7 s. Only the final `pip_install` layer rebuilt, and the output lists no web endpoint URL.
- The cold `ping.remote()` returned `'ok'` in 27.8 s, a ComfyUI boot without model load. Stats right after showed 1 runner.
- **The container scaled to zero about 118 s after the ping,** measured by polling `modal container list --json` every 10 s. That is the 60 s `scaledown_window` plus Modal's shutdown latency. Phase 6 should expect "about 2 minutes", not "about 60 s", for the fail-safe.

## Risk Assessment

<!-- Updated: Validation Session 1 - Modal script extras allowed -->

| Risk | Likelihood × impact | Observable signal | Pre-decided response |
|---|---|---|---|
| Loading the script by path does network I/O or needs credentials | Low × Medium | `tests/test_modal_backend_script.py` errors with an auth or connection error while offline | Replace the module load in `conftest.py` with a checked-in JSON fixture captured once from `build_workflow`. Keep the `ast` tests. Adjust within the plan. |
| `modal/` shadows the `modal` package | Low × High | `ImportError: cannot import name 'App' from 'modal' (unknown location)` | Make sure `modal/__init__.py` does not exist. If the error persists, stop and ask the owner before renaming the directory, because the layout is a brief decision. |
| Ruff flags the moved script | Medium × Low | `ruff check` findings in `modal/qwen21_uc_app.py` | Add a per-file ignore. Never edit the script for lint. |
| `ping()` fails on a healthy but busy backend | Low × Medium | Pings fail with a timeout while jobs on the same container succeed | Raise the probe's timeout from 5 s to 15 s, and tell the owner. The probe stays; it is a validated decision. |
| Something still calls the removed web endpoint | Low × Low | A caller reports failures after the redeploy | The owner confirmed it was unused. If a caller appears, tell the owner; restoring it means redeploying the previous commit, which is an owner decision. |
| The redeploy rebuilds more than the last image layer | Low × Low | `modal deploy` output shows the ComfyUI install steps | Wait for it to finish. No action needed. |
| The deploy disturbs a render in flight | Low × Low | Nothing is running yet: `modal container list` shows `[]` before the deploy | Deploy only while the container list is empty. |

**Rollback:** `git revert` the move commit, then **[OWNER-GATED]** redeploy the previous script with `uv run modal deploy qwen21_uc_app.py`. That also restores the web endpoint, so tell the owner first, because its proxy-auth tokens are revoked in phase 4.

## Security Considerations

<!-- Updated: Red Team 2026-09-25 - F12 Modal tokens -->
<!-- Updated: Validation Session 1 - Modal script extras allowed -->

- Never open `logs/`. It may hold token output. The secret check reads only git-tracked content.
- This phase deploys with the owner's personal token from `~/.modal.toml`, on the laptop only. The dedicated runtime and CI tokens are created in phase 4; both are workspace-wide, because the workspace has no Service Users.
- Removing the legacy `api` endpoint closes the only way to generate images outside Access. Its proxy-auth tokens become unused and are revoked in phase 4.
- `.gitignore` keeps image files under `plans/` out of git, so acceptance evidence cannot leak generated images into the repository.
- Commit messages use conventional commits, with no plan or phase references and no AI attribution.

## Next Steps

Phase 2 builds the engine on this toolchain. Its graph-parity test reuses the `backend_script` fixture from `tests/conftest.py`. Phase 6 builds its warm and unhealthy states on this `ping()` probe.
