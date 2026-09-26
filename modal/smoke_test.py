import time
from pathlib import Path

import modal

gen = modal.Cls.from_name("qwen21-uc", "Qwen21UC")()
prompt = "Vertical travel photo of a young Vietnamese woman in her mid-20s in a bikini on My Khe beach, Da Nang, turquoise waves, golden hour, natural skin, photorealistic"
out_dir = Path(__file__).resolve().parent.parent / "out"
out_dir.mkdir(exist_ok=True)
for i in range(2):
    t = time.time()
    png = gen.generate.remote(prompt=prompt, width=1088, height=1920, steps=25, seed=42 + i)
    dt = time.time() - t
    p = out_dir / f"modal_test_{i}.png"
    p.write_bytes(png)
    print(f"run {i}: {dt:.1f}s -> {p} ({len(png)/1e6:.2f} MB)", flush=True)
