-- Two reference-to-new-image workflows for Qwen-Image 2.1, stored once like the 0004 starters: one
-- takes a single reference image, the other two (for example a person and an outfit), and the prompt
-- names them <image1> and <image2>. Same flattened official "Image Edit (Qwen Image 2.1)" template as
-- 0004, but with the template's custom-size switch on: the sampler starts from a blank 1088x1920 (9:16)
-- canvas instead of the encoder's latent of image_1, so the result is a new picture, not an edit of
-- the first reference. OR IGNORE keeps a workflow the owner already stored under the same name.
INSERT OR IGNORE INTO workflows (name, backend_id, graph_json, image_inputs_json, created_at) VALUES
  ('Qwen 2.1 Reference to Image', 'qwen21-uc',
   json('{
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen-image-2.1-UC-int8_convrot.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "4": {"class_type": "LoadImage", "inputs": {"image": "example.png"}, "_meta": {"title": "Reference <image1>"}},
    "10": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "images.image_1": ["4", 0], "prompt": "The same person from <image1>, standing on a rooftop in Hanoi at sunset, full body, natural light, photorealistic.", "negative_prompt": "", "resolution": 1024}, "_meta": {"title": "Prompt"}},
    "11": {"class_type": "EmptyLatentImage", "inputs": {"width": 1088, "height": 1920, "batch_size": 1}},
    "12": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["10", 0], "negative": ["10", 1], "latent_image": ["11", 0], "seed": 0, "steps": 25, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["3", 0]}},
    "14": {"class_type": "SaveImage", "inputs": {"images": ["13", 0], "filename_prefix": "artio_ref"}}
  }'),
   '[{"node": "4", "title": "Reference <image1>"}]', (julianday('now') - 2440587.5) * 86400.0),
  ('Qwen 2.1 Two References to Image', 'qwen21-uc',
   json('{
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen-image-2.1-UC-int8_convrot.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "4": {"class_type": "LoadImage", "inputs": {"image": "example.png"}, "_meta": {"title": "Reference <image1>"}},
    "5": {"class_type": "LoadImage", "inputs": {"image": "example.png"}, "_meta": {"title": "Reference <image2>"}},
    "10": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "images.image_1": ["4", 0], "images.image_2": ["5", 0], "prompt": "The person from <image1> wearing the outfit from <image2>, walking down a lantern-lit street in Hoi An at night, full body, photorealistic.", "negative_prompt": "", "resolution": 1024}, "_meta": {"title": "Prompt"}},
    "11": {"class_type": "EmptyLatentImage", "inputs": {"width": 1088, "height": 1920, "batch_size": 1}},
    "12": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["10", 0], "negative": ["10", 1], "latent_image": ["11", 0], "seed": 0, "steps": 25, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["3", 0]}},
    "14": {"class_type": "SaveImage", "inputs": {"images": ["13", 0], "filename_prefix": "artio_ref2"}}
  }'),
   '[{"node": "4", "title": "Reference <image1>"}, {"node": "5", "title": "Reference <image2>"}]', (julianday('now') - 2440587.5) * 86400.0);
