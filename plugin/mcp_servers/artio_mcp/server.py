#!/usr/bin/env -S uv run --locked --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp[cli]>=1.2,<2",
#     "httpx>=0.27",
#     "pydantic>=2.6",
#     "pillow>=10.4",
# ]
# ///
"""The Artio Claude plugin's MCP server: eight tools over Artio's `/api/v1`, authenticated with
the Cloudflare Access service token (`CF-Access-Client-Id`/`CF-Access-Client-Secret`).

Runs from a locked, pinned dependency set (`uv run --locked --script`, this file's committed
`.lock`), so a transitive dependency can never silently change under a process that holds the
service-token secret. The client never follows a redirect: a redirect means Cloudflare Access
rejected the token and is sending the login page instead, which must never be parsed as a real
response -- it is reported as "service token rejected", with a hint, instead. Full images are
saved only inside the configured save_dir; a thumbnail is returned only when a call asks for one
(`include_thumbnail`, off by default), and it is at most one WebP within THUMB_BUDGET. Nothing
here ever calls a warm or stop endpoint: Artio's `/api/v1` doesn't expose one.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Literal

import httpx
from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from PIL import Image as PILImage
from pydantic import BaseModel, ConfigDict

MAX_WAIT_S = 120
DEFAULT_WAIT_S = 90  # a wait past ~120s risks Claude Code backgrounding the call; see the plan's risk table
POLL_INTERVAL_S = 3.0
THUMB_SIDE = 256
THUMB_BUDGET = 20_000
_ERROR_TEXT_LIMIT = 500
_TEXT_FIELD_LIMIT = 200  # prompt/error text in trimmed list payloads; full detail stays in get_image
_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_WORKFLOW_ID_RE = re.compile(r"[0-9]+")  # ASCII digits only: isdigit() also accepts non-ASCII digits


class ConfigurationError(RuntimeError):
    """Raised at import time for a credential that can never be sent as an HTTP header value. The
    message never includes the value itself -- only which variable was rejected and why."""


def _clean_credential(raw: str, name: str) -> str:
    """Strips incidental leading/trailing whitespace (a trailing newline, CR or space is a common
    copy-paste artifact) and refuses anything else outside visible ASCII -- httpx would otherwise
    raise `LocalProtocolError("Illegal header value b'<secret>'")` on the first request, and that
    exception's own text is the raw header value, which must never reach a tool result (see
    _Client.request below, which also never interpolates a raw exception for this same reason)."""
    cleaned = raw.strip()
    if not cleaned:
        return cleaned
    if not cleaned.isascii():
        raise ConfigurationError(f"{name} contains non-ASCII characters; check for a copy-paste artifact.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise ConfigurationError(f"{name} contains a control character; check for a copy-paste artifact.")
    return cleaned


BASE_URL = os.environ.get("ARTIO_BASE_URL") or "https://artio.flowitup.com"
CLIENT_ID = _clean_credential(os.environ.get("ARTIO_CF_CLIENT_ID") or "", "ARTIO_CF_CLIENT_ID")
CLIENT_SECRET = _clean_credential(os.environ.get("ARTIO_CF_CLIENT_SECRET") or "", "ARTIO_CF_CLIENT_SECRET")

mcp = FastMCP("artio")


# -- save confinement and thumbnails --------------------------------------------------------------


def save_dir() -> Path:
    root = Path(os.environ.get("ARTIO_SAVE_DIR") or "~/Artio").expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def target_path(save_to: str | None, default_name: str) -> Path:
    """Resolves a save target under save_dir(). `save_to` is model-controlled input, so an absolute
    path (which replaces `root` entirely under the `/` operator) or a `..` escape is refused, never
    silently redirected -- ToolError surfaces the refusal as this call's own result, not a crash."""
    root = save_dir()
    path = (root / (save_to or default_name)).expanduser().resolve()
    if not path.is_relative_to(root):
        raise ToolError(f"save_to must be inside the configured image folder ({root}).")
    return path


def thumbnail_webp(png: bytes) -> bytes:
    """At most one WebP, long side at most THUMB_SIDE, at most about THUMB_BUDGET bytes -- comfortably
    under the MCP output token cap even counting its base64 encoding."""
    img = PILImage.open(io.BytesIO(png))
    img.thumbnail((THUMB_SIDE, THUMB_SIDE))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    for quality in (80, 70, 60, 50, 40, 30):
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=quality)
        if buf.tell() <= THUMB_BUDGET:
            return buf.getvalue()
    img.thumbnail((THUMB_SIDE // 2, THUMB_SIDE // 2))
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=40)
    return buf.getvalue()


# -- the Artio HTTP client -----------------------------------------------------------------------


def _server_message(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    message = body.get("error", {}).get("message") if isinstance(body, dict) else None
    return message[:_ERROR_TEXT_LIMIT] if isinstance(message, str) else None


def _error_hint(response: httpx.Response) -> str:
    status = response.status_code
    if status in (401, 403):
        return "service token rejected: check the plugin's client ID/secret and the Service Auth policy."
    if status == 404:
        return _server_message(response) or "not found."
    if status == 422:
        return _server_message(response) or "the request was invalid."
    if status == 507:
        return _server_message(response) or "Artio's disk cap was reached."
    return (_server_message(response) or f"Artio answered with HTTP {status}.")[:_ERROR_TEXT_LIMIT]


class _Client:
    """Wraps every Artio API call. Never follows a redirect (a redirect is Access's login page, not
    a real answer) and never raises for an HTTP-level problem: `request()` returns an `{"error": ...}`
    dict instead, so a tool can report it inline as part of its own result rather than aborting the
    whole call. Headers and bodies are never echoed back into a hint beyond the server's own short
    error message."""

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout: float = 30.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret},
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
        )

    async def request(self, method: str, path: str, **kwargs: object) -> dict | list | bytes:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # Never interpolate str(exc): for a header-construction or transport-level failure, its
            # own text can itself be (or contain) the raw header value that caused it -- exactly the
            # secret this client sends on every request. The exception's type name plus the
            # configured base URL is enough to act on, and neither can ever hold header content.
            hint = f"could not reach Artio ({self._base_url}): {type(exc).__name__}"
            return {"error": {"status": None, "hint": hint[:_ERROR_TEXT_LIMIT]}}

        if response.is_redirect:
            return {
                "error": {
                    "status": response.status_code,
                    "hint": "service token rejected: Cloudflare Access redirected to its login page "
                    "instead of forwarding the request -- check the client ID/secret and the "
                    "Service Auth policy.",
                }
            }
        if response.status_code >= 400:
            return {"error": {"status": response.status_code, "hint": _error_hint(response)}}

        content_type = response.headers.get("content-type", "")
        if content_type.startswith("image/"):
            return response.content
        if not response.content:
            return {}
        return response.json()


api = _Client(BASE_URL, CLIENT_ID, CLIENT_SECRET)


def _is_error(result: object) -> bool:
    return isinstance(result, dict) and "error" in result


def _truncate(text: object, limit: int = _TEXT_FIELD_LIMIT) -> object:
    """Shortens a long text field (a prompt, a ComfyUI error) to about `limit` characters with an
    ellipsis, for the multi-row list tools only -- get_image and job detail still return the API's
    own full text. 50 rows of untruncated prompt or error text can run well past the MCP output
    token cap; this keeps a list call compact regardless of how long any one field is."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def save_png(image_id: int, seed: int | None, save_to: str | None = None) -> Path:
    result = await api.request("GET", f"/api/v1/images/{image_id}/file")
    if _is_error(result):
        raise ToolError(result["error"]["hint"])  # type: ignore[index]
    default_name = f"artio-{image_id}-{seed if seed is not None else 'na'}.png"
    path = target_path(save_to, default_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result)  # type: ignore[arg-type]
    return path


async def wait_for_jobs(job_ids: list[int], wait_seconds: float, ctx: Context) -> list[dict]:
    """Polls /api/v1/jobs every POLL_INTERVAL_S, reporting progress, until every job has left
    queued/submitted or wait_seconds has passed (capped by the caller at MAX_WAIT_S) -- always
    checking status at least once, even when wait_seconds is 0. Jobs still running past the wait are
    returned as-is, so the caller can point Claude at job_status instead of blocking indefinitely."""
    deadline = time.monotonic() + wait_seconds
    by_id: dict[int, dict] = {}
    ids_param = ",".join(str(j) for j in job_ids)
    while True:
        result = await api.request("GET", "/api/v1/jobs", params={"ids": ids_param})
        if _is_error(result):
            return [{"id": j, "status": "unknown", "error": result["error"]["hint"]} for j in job_ids]  # type: ignore[index]
        for job in result:  # type: ignore[union-attr]
            job["error"] = _truncate(job.get("error"))
            by_id[job["id"]] = job
        pending = [j for j in job_ids if by_id.get(j, {}).get("status") in (None, "queued", "submitted")]
        if not pending or time.monotonic() >= deadline:
            break
        await _report_progress(
            ctx, progress=len(job_ids) - len(pending), total=len(job_ids), message=f"{len(pending)} job(s) still running"
        )
        await asyncio.sleep(POLL_INTERVAL_S)
    return [by_id.get(j, {"id": j, "status": "unknown"}) for j in job_ids]


async def _report_progress(ctx: Context, *, progress: float, total: float, message: str) -> None:
    """Best-effort: a client with no progress token (or, as in the integration test, no live request
    context at all) must never turn a plain status update into a failed tool call."""
    try:
        await ctx.report_progress(progress=progress, total=total, message=message)
    except Exception:  # noqa: BLE001, S110 -- progress reporting is a courtesy, never load-bearing
        pass


def _batch_result(created: dict, job_results: list[dict], saved: list[Path]) -> dict:
    result = {"batch_id": created["batch_id"], "jobs": job_results, "saved_to": [str(p) for p in saved]}
    if any(j.get("status") in ("queued", "submitted", "unknown") for j in job_results):
        result["next"] = "Still running. Call job_status with these job ids."
    return result


async def _generate_or_run(created: dict, wait_seconds: int, include_thumbnail: bool, ctx: Context) -> list:
    wait_s = min(wait_seconds, MAX_WAIT_S)
    job_results = await wait_for_jobs(created["job_ids"], wait_s, ctx)
    saved: list[Path] = []
    for job in job_results:
        if job.get("status") == "done" and job.get("image_id") is not None:
            try:
                saved.append(await save_png(job["image_id"], job.get("seed")))
            except Exception as exc:  # noqa: BLE001 -- a save failure must never lose an already-paid-for batch
                # save_png only ever raises ToolError with a hint _error_hint() or target_path()
                # already built to be safe (no header content, no secret): recording str(exc) here
                # is exactly as safe as that hint was.
                job["save_error"] = f"could not save this image: {exc}"[:_ERROR_TEXT_LIMIT]
    blocks: list = [json.dumps(_batch_result(created, job_results, saved))]
    if include_thumbnail and saved:
        blocks.append(Image(data=thumbnail_webp(saved[0].read_bytes()), format="webp"))
    return blocks


# -- tools ------------------------------------------------------------------------------------------


class ListModelsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def list_models(args: ListModelsArgs) -> list:
    """Lists every model the registry offers, with its presets, defaults and parameter bounds."""
    return [json.dumps(await api.request("GET", "/api/v1/models"))]


class GenerateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    prompt: str
    negative: str = ""
    size: str | None = None
    width: int | None = None
    height: int | None = None
    steps: int | None = None
    cfg: float | None = None
    seed: int | None = None
    count: int = 1
    wait_seconds: int = DEFAULT_WAIT_S
    include_thumbnail: bool = False

    def request_body(self) -> dict:
        body: dict = {"model": self.model, "prompt": self.prompt, "negative": self.negative, "count": self.count}
        for field in ("size", "width", "height", "steps", "cfg", "seed"):
            value = getattr(self, field)
            if value is not None:
                body[field] = value
        return body


@mcp.tool(structured_output=False)
async def generate(args: GenerateArgs, ctx: Context) -> list:
    """Starts a batch of generations and waits (up to wait_seconds, capped at 120s) for them to
    finish, saving each finished PNG under the configured folder. Returns a thumbnail only when
    include_thumbnail is true. Still-running jobs are reported by id for job_status to follow up."""
    created = await api.request("POST", "/api/v1/generate", json=args.request_body())
    if _is_error(created):
        return [json.dumps(created)]
    return await _generate_or_run(created, args.wait_seconds, args.include_thumbnail, ctx)  # type: ignore[arg-type]


class JobStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_ids: list[int]


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def job_status(args: JobStatusArgs) -> list:
    """Reports the current status of one or more job ids, from a previous generate or run_workflow.
    A long ComfyUI error is shortened to keep a many-id call compact; job_status on that one id
    again after a targeted search still returns the same shortened text -- Artio's own web UI and
    `/api/v1/jobs` keep the full text, only this multi-row tool trims it."""
    ids_param = ",".join(str(j) for j in args.job_ids)
    result = await api.request("GET", "/api/v1/jobs", params={"ids": ids_param})
    if not _is_error(result):
        for job in result:  # type: ignore[union-attr]
            job["error"] = _truncate(job.get("error"))
    return [json.dumps(result)]


class ListImagesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    tag: str | None = None
    starred: bool = False
    model: str | None = None
    limit: int = 20
    offset: int = 0


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def list_images(args: ListImagesArgs) -> list:
    """Searches and lists saved images, newest first, by prompt/tag text, an exact tag, starred-only
    or model. `limit` is capped at 50 by the API itself. Each entry's prompt is shortened to keep a
    full page of results compact -- call get_image for one image's untruncated prompt and metadata."""
    params: dict[str, str | int] = {
        "starred": "true" if args.starred else "false",
        "limit": args.limit,
        "offset": args.offset,
    }
    if args.query:
        params["q"] = args.query
    if args.tag:
        params["tag"] = args.tag
    if args.model:
        params["model"] = args.model
    result = await api.request("GET", "/api/v1/images", params=params)
    if not _is_error(result):
        for row in result:  # type: ignore[union-attr]
            row["prompt"] = _truncate(row.get("prompt"))
    return [json.dumps(result)]


class GetImageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_id: int
    save_to: str | None = None
    include_thumbnail: bool = False


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def get_image(args: GetImageArgs) -> list:
    """Fetches one image's full metadata and saves its PNG under the configured folder (optionally at
    a save_to path, which must resolve inside that folder). Returns a thumbnail only when asked."""
    detail = await api.request("GET", f"/api/v1/images/{args.image_id}")
    if _is_error(detail):
        return [json.dumps(detail)]
    path = await save_png(args.image_id, detail.get("seed"), args.save_to)  # type: ignore[union-attr]
    blocks: list = [json.dumps({"image_id": args.image_id, "saved_to": str(path), "metadata": detail})]
    if args.include_thumbnail:
        blocks.append(Image(data=thumbnail_webp(path.read_bytes()), format="webp"))
    return blocks


class ListWorkflowsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def list_workflows(args: ListWorkflowsArgs) -> list:
    """Lists stored ComfyUI workflows, each with its id, name, backend and whether it accepts a seed."""
    return [json.dumps(await api.request("GET", "/api/v1/workflows"))]


async def _resolve_workflow_id(workflow: str) -> int | dict:
    """A name match always wins over a numeric-id guess: a workflow literally named "2024" (or any
    other all-digit name) must resolve to itself, not to id 2024. Only once no stored workflow's
    name equals `workflow` exactly does a pure-ASCII-digit string (`str.isdigit()` alone also
    accepts non-ASCII digits, e.g. "١٢") fall back to being read as an id."""
    listing = await api.request("GET", "/api/v1/workflows")
    if _is_error(listing):
        return listing  # type: ignore[return-value]
    for entry in listing:  # type: ignore[union-attr]
        if entry["name"] == workflow:
            return entry["id"]
    if _WORKFLOW_ID_RE.fullmatch(workflow):
        return int(workflow)
    return {"error": {"hint": f"No stored workflow named or with id {workflow!r}. Call list_workflows first."}}


class RunWorkflowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: str
    seed_mode: Literal["random", "fixed", "keep"] = "random"
    seed: int | None = None
    count: int = 1
    wait_seconds: int = DEFAULT_WAIT_S
    include_thumbnail: bool = False


@mcp.tool(structured_output=False)
async def run_workflow(args: RunWorkflowArgs, ctx: Context) -> list:
    """Runs a stored ComfyUI workflow by name or id (resolved through list_workflows), and waits for
    it the same way generate does."""
    workflow_id = await _resolve_workflow_id(args.workflow)
    if isinstance(workflow_id, dict):
        return [json.dumps(workflow_id)]
    body: dict = {"seed_mode": args.seed_mode, "count": args.count}
    if args.seed is not None:
        body["seed"] = args.seed
    created = await api.request("POST", f"/api/v1/workflows/{workflow_id}/run", json=body)
    if _is_error(created):
        return [json.dumps(created)]
    return await _generate_or_run(created, args.wait_seconds, args.include_thumbnail, ctx)  # type: ignore[arg-type]


class GpuStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def gpu_status(args: GpuStatusArgs) -> list:
    """Reports each backend's current state (warm, warming, running, scaled to zero, stopped or
    unhealthy). There is no tool to warm or stop a backend -- Artio's API doesn't expose one."""
    return [json.dumps(await api.request("GET", "/api/v1/gpu"))]


if __name__ == "__main__":
    mcp.run()
