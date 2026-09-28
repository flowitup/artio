-- Three starter workflows for Qwen-Image 2.1, stored once so they appear on the Workflows page
-- without an upload: image edit, background removal and a 2K upscale. Each is the official ComfyUI
-- "Image Edit (Qwen Image 2.1)" template flattened to API format with its prompt enhancer and cache
-- node left out and Artio's own model files swapped in; they differ only in prompt and resolution
-- (the encoder's total pixel budget: 1024 is the official default, 2048 is native 2K). A migration
-- runs once per database, so deleting one later keeps it deleted, and a workflow the owner already
-- stored under the same name is left as it is (OR IGNORE on the UNIQUE name).
INSERT OR IGNORE INTO workflows (name, backend_id, graph_json, image_inputs_json, created_at) VALUES
  ('Qwen 2.1 Image Edit', 'qwen21-uc',
   json('{
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen-image-2.1-UC-int8_convrot.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "4": {"class_type": "LoadImage", "inputs": {"image": "example.png"}, "_meta": {"title": "Image to edit"}},
    "5": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "images.image_1": ["4", 0], "prompt": "Replace the background with a sunny beach at golden hour. Keep the person, pose, face and clothing exactly the same.", "negative_prompt": "", "resolution": 1024}, "_meta": {"title": "Prompt"}},
    "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["5", 0], "negative": ["5", 1], "latent_image": ["5", 2], "seed": 0, "steps": 25, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
    "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "artio_edit"}}
  }'),
   '[{"node": "4", "title": "Image to edit"}]', (julianday('now') - 2440587.5) * 86400.0),
  ('Qwen 2.1 Remove Background', 'qwen21-uc',
   json('{
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen-image-2.1-UC-int8_convrot.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "4": {"class_type": "LoadImage", "inputs": {"image": "example.png"}, "_meta": {"title": "Image"}},
    "5": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "images.image_1": ["4", 0], "prompt": "Remove the background, and output a PNG image", "negative_prompt": "", "resolution": 1024}, "_meta": {"title": "Prompt"}},
    "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["5", 0], "negative": ["5", 1], "latent_image": ["5", 2], "seed": 0, "steps": 25, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
    "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "artio_nobg"}}
  }'),
   '[{"node": "4", "title": "Image"}]', (julianday('now') - 2440587.5) * 86400.0),
  ('Qwen 2.1 2K Upscale', 'qwen21-uc',
   json('{
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen-image-2.1-UC-int8_convrot.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "4": {"class_type": "LoadImage", "inputs": {"image": "example.png"}, "_meta": {"title": "Image to upscale"}},
    "5": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "images.image_1": ["4", 0], "prompt": "Upscale this image to 2K. Sharpen fine details and textures while keeping the content, composition, colors, faces and any text exactly the same.", "negative_prompt": "", "resolution": 2048}, "_meta": {"title": "Prompt"}},
    "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["5", 0], "negative": ["5", 1], "latent_image": ["5", 2], "seed": 0, "steps": 25, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
    "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "artio_2k"}}
  }'),
   '[{"node": "4", "title": "Image to upscale"}]', (julianday('now') - 2440587.5) * 86400.0);
