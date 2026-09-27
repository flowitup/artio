"""Image detail, file/thumbnail serving and delete."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse, Response

from artio import db, library, storage

router = APIRouter()

_MAX_ID = 2**63 - 1


def _resolve_or_404(data_dir: Path, relative: str) -> Path:
    resolved = storage.resolve_under(data_dir, relative)
    if resolved is None:
        raise HTTPException(status_code=404, detail="not found")
    return resolved


def _load_image_or_404(conn, image_id: int) -> library.ImageDetail:
    image = library.image_detail(conn, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="image not found")
    return image


@router.get("/images/{image_id}")
async def image_detail(request: Request, image_id: int = PathParam(ge=1, le=_MAX_ID)) -> HTMLResponse:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        image = _load_image_or_404(conn, image_id)
    return request.app.state.templates.TemplateResponse(request, "image.html", {"image": image})


@router.get("/images/{image_id}/file")
async def image_file(request: Request, image_id: int = PathParam(ge=1, le=_MAX_ID)) -> FileResponse:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        row = conn.execute("SELECT file_png, seed FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="image not found")
    path = _resolve_or_404(settings.data_dir, row["file_png"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image file missing")
    filename = f"artio-{image_id}-{row['seed'] if row['seed'] is not None else 'na'}.png"
    return FileResponse(path, media_type="image/png", filename=filename)


@router.get("/images/{image_id}/thumb")
async def image_thumb(request: Request, image_id: int = PathParam(ge=1, le=_MAX_ID)) -> FileResponse:
    settings = request.app.state.settings
    with db.session(settings) as conn:
        row = conn.execute("SELECT file_thumb FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="image not found")
    path = _resolve_or_404(settings.data_dir, row["file_thumb"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="thumbnail missing")
    return FileResponse(path, media_type="image/webp")


@router.post("/images/{image_id}/delete")
async def delete_image(request: Request, image_id: int) -> Response:
    # image_id has no upper Path bound here: this is an htmx target, so an id past SQLite's 64-bit
    # range must still answer 200 with a message (below), never FastAPI's automatic 422.
    settings = request.app.state.settings
    with db.session(settings) as conn:
        try:
            library.delete_image(conn, settings.data_dir, image_id)
        except (library.UnknownImage, OverflowError):
            flash_html = request.app.state.templates.get_template("partials/flash.html").render(
                {"request": request, "message": f"Image {image_id} no longer exists."}
            )
            response = HTMLResponse(flash_html, status_code=200)
            response.headers["HX-Retarget"] = "#flash"
            response.headers["HX-Reswap"] = "innerHTML"
            return response
    return Response(status_code=200, headers={"HX-Redirect": "/gallery"})
