---
name: artio
description: >
  Drive Artio, a private image studio, to generate and browse images or run
  stored ComfyUI workflows. Trigger when the user asks to generate/create an
  image with Artio, check a generation's status, search or fetch a saved
  image, list or run a stored workflow, or check the GPU/backend status. Never
  suggests warming or stopping a GPU backend -- Artio's plugin API has no
  such tool, by design.
---

# Artio

Artio turns a prompt into a saved PNG on a Modal GPU backend. Every tool here
goes through the plugin's own service token; it can read models, images,
workflows and GPU status, and it can start a generation or a workflow run, but
it can never warm up or stop a backend, upload or delete anything, or reach any
page outside `/api/v1` -- those stay owner-only in the web app itself.

## Typical flow

1. **Check `gpu_status` first** if it's been a while since the last generation,
   or the user asks how long a request will take. A backend scaled to zero
   takes about 70 seconds to boot its first image (a cold start); a warm one
   answers in a few seconds. Mention this once, plainly -- never suggest
   warming the backend yourself; that control is deliberately owner-only.
2. **Call `list_models`** if you don't already know the model id, its size
   presets, or its steps/cfg bounds. Use a preset name (`size`) when the user
   wants a standard aspect ratio; use `width`/`height` only for a custom size
   (must be a multiple of the model's `size.multiple`).
3. **Call `generate`** with the model, prompt and any parameters the user gave.
   Leave `seed` unset for a random seed; set it for a reproducible one. Leave
   `include_thumbnail` false unless the user wants to see the result inline --
   it costs image tokens and leaves a copy of the picture in this transcript.
   The call waits up to `wait_seconds` (default 90, hard-capped at 120) and
   saves each finished PNG under the configured save folder, reporting
   progress while it waits.
   - If the result includes a `"next"` field, at least one job is still
     running: tell the user, and offer to check back with `job_status` using
     the returned job ids.
   - Tell the user the saved file path(s) from `saved_to`; don't just say
     "done" without it.
4. **Use `job_status`** to follow up on ids `generate` or `run_workflow` left
   running, or that the user asks about later in the conversation.
5. **Use `list_images`** to search by prompt/tag text, an exact tag, starred
   only, or a specific model -- newest first. Use `get_image` to fetch one
   image's full metadata and save its file locally (optionally at a specific
   `save_to` path inside the save folder); ask for a thumbnail only if the
   user wants to see it.
6. **Use `list_workflows`** to see what's stored, and `run_workflow` (by name
   or id) to run one, with the same wait/thumbnail behavior as `generate`.

## Error cases

Every tool reports a plain-language hint on failure, inside its own JSON
result -- it does not silently fail or crash the conversation. Common ones:

- **"service token rejected"**: the plugin's Cloudflare Access credentials are
  missing, wrong, or the token was revoked. Tell the user to check the
  plugin's configuration (`/plugin configure artio` in Claude Code, or the
  desktop app's plugin settings) -- this is not something you can fix from
  inside the conversation.
- **"disk cap reached" (`disk_guard`)**: Artio's image storage is full or the
  volume is low on free space. No new generation or workflow run can start
  until the owner clears space; don't retry automatically.
- **"not found"**: an image or workflow id doesn't exist (it may have been
  deleted). Double-check the id, or call `list_images`/`list_workflows` again.
- **"the request was invalid" (`validation_error`)**: a parameter is out of
  the model's bounds, or an unknown model/workflow was named. Call
  `list_models` or `list_workflows` to see the valid values, then retry.
- **A tool call itself fails outright** (rather than returning a JSON error):
  this means the arguments were malformed at the protocol level (for example a
  `save_to` that resolves outside the save folder). Fix the argument and retry
  once; don't loop on it.
