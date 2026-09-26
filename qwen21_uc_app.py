"""
Qwen-Image 2.1 Uncensored (UC) on Modal — ComfyUI backend + API.

  modal run    qwen21_uc_app.py::download_models   # one-time: models -> Volume
  modal deploy qwen21_uc_app.py                    # deploy (scales to zero)
  modal run    qwen21_uc_app.py --prompt "..."     # quick test, writes out/*.png

Call from Python (e.g. the Hetzner worker):
  gen = modal.Cls.from_name("qwen21-uc", "Qwen21UC")()
  png = gen.generate.remote(prompt="...", width=1088, height=1920)
"""
import json
import subprocess
import time
import uuid
from pathlib import Path

import modal

APP_NAME = "qwen21-uc"
GPU = "L40S"                   # 48 GB; "A10" (24 GB) is cheaper but slower/tighter
COMFY_VERSION = "v0.37.2"      # same as Comfy Desktop on the Mac
MODELS_DIR = "/models"

REPO = "abenzerps/Qwen-Image-2.1-Uncensored-GGUF"
FILES = {
    # repo path                                               -> (subfolder, sha256)
    "qwen-image-2.1-UC-int8_convrot.safetensors": ("diffusion_models", "5bc5a6c007eff1e0d4004344a24c4af966b9d2c83d142e613b0d456ccceb19ae"),
    "text_encoders/qwen3vl_8b_int8_convrot.safetensors": ("text_encoders", "8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f"),
    "vae/qwen_image_2.1_vae_bf16.safetensors": ("vae", "bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9"),
}
UNET = "qwen-image-2.1-UC-int8_convrot.safetensors"
CLIP = "qwen3vl_8b_int8_convrot.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"

app = modal.App(APP_NAME)
vol = modal.Volume.from_name("qwen21-uc-models", create_if_missing=True)

EXTRA_PATHS = f"""modal:
  base_path: {MODELS_DIR}
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  vae: vae
"""

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git", "libgl1", "libglib2.0-0")
    .run_commands(
        f"git clone --depth 1 --branch {COMFY_VERSION} https://github.com/comfyanonymous/ComfyUI /root/ComfyUI",
        "pip install -r /root/ComfyUI/requirements.txt",
    )
    .pip_install("requests", "fastapi[standard]")
)

dl_image = modal.Image.debian_slim(python_version="3.12").pip_install("huggingface_hub")


@app.function(image=dl_image, volumes={MODELS_DIR: vol}, timeout=3600, cpu=4)
def download_models():
    import hashlib
    import shutil
    from huggingface_hub import hf_hub_download

    for repo_path, (sub, sha) in FILES.items():
        dst = Path(MODELS_DIR) / sub / Path(repo_path).name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            t = time.time()
            src = hf_hub_download(REPO, repo_path, local_dir="/tmp/hf")
            shutil.move(src, dst)
            print(f"downloaded {dst.name} in {time.time()-t:.0f}s")
        h = hashlib.sha256()
        with open(dst, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 24), b""):
                h.update(chunk)
        ok = h.hexdigest() == sha
        print(f"{'OK ' if ok else 'BAD'} {dst} ({dst.stat().st_size/1e9:.2f} GB)")
        if not ok:
            dst.unlink()
            raise RuntimeError(f"checksum mismatch: {dst.name}")
    vol.commit()


def build_workflow(prompt, width, height, steps, seed, cfg=1.0, negative=""):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "prompt": prompt, "negative_prompt": negative, "resolution": 1024}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1], "latent_image": ["5", 0],
                                                    "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "qwen21uc"}},
    }


@app.cls(gpu=GPU, image=image, volumes={MODELS_DIR: vol}, scaledown_window=60, timeout=1800, max_containers=1)
@modal.concurrent(max_inputs=4)
class Qwen21UC:
    @modal.enter()
    def start(self):
        import requests
        self.base = "http://127.0.0.1:8188"
        Path("/root/ComfyUI/extra_model_paths.yaml").write_text(EXTRA_PATHS)
        self.proc = subprocess.Popen(
            ["python", "main.py", "--listen", "127.0.0.1", "--port", "8188", "--disable-auto-launch"],
            cwd="/root/ComfyUI",
        )
        for _ in range(300):
            try:
                if requests.get(f"{self.base}/system_stats", timeout=2).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError("ComfyUI did not start")

    def _run(self, workflow: dict) -> bytes:
        import requests
        pid = requests.post(f"{self.base}/prompt", json={"prompt": workflow, "client_id": str(uuid.uuid4())}).json()
        if "prompt_id" not in pid:
            raise RuntimeError(f"ComfyUI rejected workflow: {json.dumps(pid)[:1500]}")
        pid = pid["prompt_id"]
        while True:
            hist = requests.get(f"{self.base}/history/{pid}").json()
            if pid in hist:
                st = hist[pid].get("status", {})
                if st.get("status_str") == "error":
                    raise RuntimeError(json.dumps(st.get("messages", []))[-1500:])
                if st.get("completed"):
                    break
            time.sleep(1)
        for out in hist[pid]["outputs"].values():
            for img in out.get("images", []):
                return requests.get(f"{self.base}/view", params=img).content
        raise RuntimeError("no image in outputs")

    @modal.method()
    def generate(self, prompt: str, width: int = 1088, height: int = 1920, steps: int = 25,
                 seed: int = -1, cfg: float = 1.0, negative: str = "") -> bytes:
        import random
        seed = random.randint(1, 2**31 - 1) if seed < 0 else seed
        return self._run(build_workflow(prompt, width, height, steps, seed, cfg, negative))

    @modal.method()
    def run_workflow(self, workflow: dict) -> bytes:
        """Run any API-format ComfyUI workflow (e.g. exported from Comfy Desktop)."""
        return self._run(workflow)

    @modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
    def api(self, body: dict):
        """POST {prompt, width, height, steps, seed} with Modal-Key/Modal-Secret headers -> image/png."""
        from fastapi import Response
        png = self.generate.local(**{k: v for k, v in body.items() if k in ("prompt", "width", "height", "steps", "seed", "cfg", "negative")})
        return Response(content=png, media_type="image/png")


@app.local_entrypoint()
def main(prompt: str = "Vertical travel photo of a young Vietnamese woman in her mid-20s on My Khe beach, Da Nang, turquoise waves, golden hour",
         width: int = 1088, height: int = 1920, steps: int = 25, seed: int = -1):
    t = time.time()
    png = Qwen21UC().generate.remote(prompt=prompt, width=width, height=height, steps=steps, seed=seed)
    Path("out").mkdir(exist_ok=True)
    p = Path("out") / f"qwen21uc_{int(time.time())}.png"
    p.write_bytes(png)
    print(f"saved {p} ({len(png)/1e6:.1f} MB) in {time.time()-t:.0f}s")
