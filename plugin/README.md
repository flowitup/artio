# Atelier plugin for Claude

Connector for [Atelier](https://atelier.flowitup.com), the owner's private
image studio. It lets Claude list models, generate images, check job and GPU
status, and search, fetch and run stored ComfyUI workflows -- all through the
Cloudflare Access service token `atelier-plugin`. It has no tool to warm or
stop a GPU backend, upload or delete anything, or reach any page outside
`/api/v1`: those stay owner-only in the web app.

## Install

**Primary route (Claude desktop app):** open `atelier.plugin` (built by
`build.sh`, below) with the app, or install it from Settings -> Capabilities.
The configuration dialog then asks for `cf_client_id` and `cf_client_secret`.

**Secondary route (Claude Code), from a checkout of this repository:**

```bash
claude plugin marketplace add /path/to/this/repo   # registers the one-entry local marketplace
claude plugin install atelier@atelier-local        # or /plugin install atelier@atelier-local in a session
/plugin configure atelier                           # enter cf_client_id and cf_client_secret
```

A shell `claude plugin install` shows no configuration dialog; always follow
it with `/plugin configure atelier` in a session.

**If the desktop app's install route shows no `userConfig` prompt:** fall back
to environment passthrough, the same pattern Folio's plugin uses. Edit
`.mcp.json` so each `env` value reads `"${ATELIER_CF_CLIENT_ID}"` etc. instead
of `"${user_config.*}"`, and export those variables in the shell that starts
Claude. Never put a literal secret value in any file.

## Configure

| Setting | Meaning |
|---|---|
| `base_url` | Atelier's public origin. Leave at the default unless told otherwise. |
| `cf_client_id` | The `atelier-plugin` service token's Client ID (not secret). |
| `cf_client_secret` | The service token's Client Secret, shown only once when created. Marked sensitive: Claude stores it in the OS credential store, never in a file. |
| `save_dir` | Where full-resolution PNGs are saved. Defaults to `~/Atelier`, created with restrictive (0700) permissions. |

The Client Secret comes from the owner's password manager. It is never logged,
never written to disk by the plugin itself, and never included in the built
`.plugin` zip.

## Tools

`list_models`, `generate`, `job_status`, `list_images`, `get_image`,
`list_workflows`, `run_workflow`, `gpu_status`. See `skills/atelier/SKILL.md`
for the intended flow and error handling; every tool's own docstring (visible
to Claude) explains its exact parameters.

## Privacy

- **Thumbnails are opt-in.** `generate`, `run_workflow` and `get_image` all
  take `include_thumbnail` (default `false`). A thumbnail is at most one WebP
  image, at most 256 px on its long side and about 20 KB, sent to Anthropic
  and kept in the local Claude transcript only when a call explicitly asks
  for one -- image content otherwise never leaves Atelier's own store.
- **Transcript retention** follows Claude Code's own `cleanupPeriodDays`
  setting in `~/.claude/settings.json` (30 days by default). A thumbnail that
  reached a transcript is retained for as long as that transcript is.
- **Saves are confined to `save_dir`.** Every full PNG this plugin writes
  lands inside the configured save folder; a `save_to` argument that would
  resolve outside it is refused, not silently redirected.
- **The client secret** lives only in the OS credential store (desktop route)
  or the shell environment (Code route, environment-passthrough fallback) and
  the owner's password manager -- never in this repository, the built zip, a
  log line, or a tool's own result. A pasted-in trailing newline, space or CR
  is stripped automatically at startup; a value that still contains a control
  character or a non-ASCII byte is refused at startup (with a message that
  only names the setting, never its value), instead of ever being echoed back
  from a failed request.

## Layout

```
.claude-plugin/plugin.json
.mcp.json
mcp_servers/atelier_mcp/server.py
mcp_servers/atelier_mcp/server.py.lock
skills/atelier/SKILL.md
README.md
```

Package for installation: `bash build.sh`, run from this directory, zips these
files (including the lock) into `atelier.plugin` for the Claude desktop app.
