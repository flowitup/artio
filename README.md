# Artio

Artio is the owner's private image studio at `artio.flowitup.com`: one FastAPI + HTMX + SQLite
service, running in a confined container on `folio-prod-1`, that drives Modal GPU backends behind
Cloudflare Access. It has no public sign-up and no anonymous access -- every request needs a verified
Access identity, either the owner's own email or the Claude plugin's service token.

## What it does

- **Generate.** Pick a model, a size preset (or a custom size), a seed strategy and a batch count;
  follow the queue live; browse, remix and delete finished images.
- **GPU control.** See each backend's status (warm, warming, running, scaled to zero, stopped or
  unhealthy), warm one up ahead of a session, or stop it -- computed on read, never polled in the
  background while no page is open.
- **Prompt library.** Save, load and delete named presets; star and tag images; search by prompt
  text or tag, combined with any filter.
- **Custom workflows.** Upload a ComfyUI API-format graph, run it with a seed mode of its own and
  an uploaded picture for each Load Image node, and keep every past run's exact graph even after
  the stored workflow is deleted.
- **Claude plugin.** Drive Artio from Claude -- list models, generate, check status, search and
  fetch images, list and run stored workflows, and see GPU status -- through a versioned JSON API
  (`/api/v1`) the plugin's service token can reach and nothing else.

**There are no backups.** The owner decided on 2026-09-27 that Artio keeps no copy beyond its own
data volume: if it is lost, the images and the database are gone. Download anything worth keeping
from the gallery.

## Local development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                    # installs artio's own dependencies (the dev group too)
uv run uvicorn artio.main:create_app --factory --reload --env-file .env.local
```

A local run needs `ARTIO_ENV=development` (or `test`) plus the Access settings in
`artio/config.py`; `ARTIO_ENV=development` also allows `ARTIO_DEV_IDENTITY=owner` to bypass
JWT verification entirely for local iteration -- never set outside development, and refused by
`load_settings` if it is.

## Commands

```bash
uv run ruff check                                           # lint
uv run pytest -q                                             # the whole suite (one opt-in live test is deselected by default)
uv run pytest -m live                                        # the opt-in test that spends real Modal GPU time
bash plugin/build.sh                                          # builds plugin/artio.plugin (the Claude desktop app's install file)
uv lock --script plugin/mcp_servers/artio_mcp/server.py     # regenerate the plugin's lockfile after changing its dependencies
```

The Modal backend itself lives under `modal/` (`qwen21_uc_app.py`); `uv run modal deploy
modal/qwen21_uc_app.py` redeploys it. See that directory's own comments for the ComfyUI graph and
model files.

## Documentation

- **[`docs/system-architecture.md`](docs/system-architecture.md)** -- components, data flows, auth
  and route authorization, the job lifecycle, GPU control, storage and deploy.
- **[`docs/deployment-guide.md`](docs/deployment-guide.md)** -- the one-time server and cloud setup,
  the credential table, day-to-day deployment, the Claude plugin install (both routes), token
  rotation and the leak response, and the routine-operations runbook.
- **`plugin/README.md`** -- the Claude plugin's own install, configuration and privacy notes.
- **`plans/260925-1331-artio-image-studio/`** -- the phase-by-phase implementation plan and its
  acceptance record.
