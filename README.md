# qwen21-uc-modal

Qwen-Image 2.1 Uncensored (int8_convrot) on Modal — ComfyUI v0.37.2 on an L40S, scales to zero.
Workspace: `yaiba2307` · env: `main` · app: `qwen21-uc` · volume: `qwen21-uc-models` (~17 GB)

## Commands
```bash
uv run modal deploy modal/qwen21_uc_app.py                  # redeploy after edits
uv run modal run modal/qwen21_uc_app.py::download_models    # only if the Volume is lost
uv run python modal/smoke_test.py                           # 2 test renders -> out/ (spends GPU time)
modal app logs qwen21-uc                          # live logs
modal app stop qwen21-uc                          # take it offline
```

## Call it
```python
import modal
gen = modal.Cls.from_name("qwen21-uc", "Qwen21UC")()
png = gen.generate.remote(prompt="...", width=1088, height=1920, steps=25, seed=-1)
png = gen.run_workflow.remote(workflow_api_dict)   # any ComfyUI API-format graph
gen.ping.remote()                                   # "ok" while ComfyUI answers (cold call boots a container)
```

## Benchmarks (2026-09-25, 1088×1920, 25 steps, cfg 1, euler/simple)
- Cold start + 1st image: 68 s · warm image: 15.6 s (~2 it/s) · ≈ $0.009 / warm image (L40S $1.95/h)

License: Qwen Research License — non-commercial.
