from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
TILES_DIR = DATA_DIR / "tiles"
MANIFESTS_DIR = DATA_DIR / "manifests"

DEFAULT_MIN_ZOOM = 0
DEFAULT_MAX_ZOOM = 5
DEFAULT_TILE_SIZE = 256
DEFAULT_TITLE = "tile image"

for directory in (IMAGES_DIR, TILES_DIR, MANIFESTS_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Tile Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TileManifest(BaseModel):
    id: str
    title: str
    width: int
    height: int
    minZoom: int
    maxZoom: int
    tileSize: int
    extent: List[float]
    origin: List[float]
    resolutions: List[float]
    center: List[float]
    initialResolution: float
    urlTemplate: str
    manifestUrl: str


class CreateTileResponse(BaseModel):
    imageId: str
    manifest: TileManifest


class ExistingImageRequest(BaseModel):
    image_path: str
    image_id: Optional[str] = None
    title: Optional[str] = None
    min_zoom: int = DEFAULT_MIN_ZOOM
    max_zoom: int = DEFAULT_MAX_ZOOM
    tile_size: int = DEFAULT_TILE_SIZE


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/tiles", response_model=CreateTileResponse)
async def create_tiles(
    file: UploadFile = File(...),
    min_zoom: int = Form(DEFAULT_MIN_ZOOM),
    max_zoom: int = Form(DEFAULT_MAX_ZOOM),
    tile_size: int = Form(DEFAULT_TILE_SIZE),
    image_id: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
):
    validate_zoom_and_tile_size(min_zoom, max_zoom, tile_size)

    suffix = Path(file.filename or "").suffix or ".png"
    image_id = normalize_image_id(image_id)
    title = title or file.filename or DEFAULT_TITLE

    image_path = IMAGES_DIR / f"{image_id}{suffix}"
    with image_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    manifest = build_tiles_for_image(
        image_path=image_path,
        image_id=image_id,
        title=title,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        tile_size=tile_size,
    )
    return CreateTileResponse(imageId=image_id, manifest=manifest)


@app.post("/api/tiles/from-local", response_model=CreateTileResponse)
def create_tiles_from_local(payload: ExistingImageRequest):
    validate_zoom_and_tile_size(payload.min_zoom, payload.max_zoom, payload.tile_size)

    image_path = Path(payload.image_path).expanduser().resolve()
    if not image_path.exists() or not image_path.is_file():
        raise HTTPException(status_code=404, detail=f"图片不存在: {image_path}")

    image_id = normalize_image_id(payload.image_id)
    title = payload.title or image_path.name

    suffix = image_path.suffix or ".png"
    stored_image_path = IMAGES_DIR / f"{image_id}{suffix}"
    if image_path != stored_image_path:
        shutil.copy2(image_path, stored_image_path)
    else:
        stored_image_path = image_path

    manifest = build_tiles_for_image(
        image_path=stored_image_path,
        image_id=image_id,
        title=title,
        min_zoom=payload.min_zoom,
        max_zoom=payload.max_zoom,
        tile_size=payload.tile_size,
    )
    return CreateTileResponse(imageId=image_id, manifest=manifest)


@app.get("/api/tiles/{image_id}/manifest", response_model=TileManifest)
def get_manifest(image_id: str):
    manifest_path = MANIFESTS_DIR / f"{image_id}.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="manifest 不存在")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


@app.get("/api/tiles/{image_id}/{z}/{x}/{y}.png")
def get_tile(image_id: str, z: int, x: int, y: int):
    tile_path = TILES_DIR / image_id / str(z) / str(x) / f"{y}.png"
    if not tile_path.exists():
        raise HTTPException(status_code=404, detail="tile 不存在")
    return FileResponse(tile_path, media_type="image/png")


@app.get("/api/tiles/{image_id}")
def get_tile_summary(image_id: str):
    manifest_path = MANIFESTS_DIR / f"{image_id}.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="image_id 不存在")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "imageId": image_id,
        "manifestUrl": manifest["manifestUrl"],
        "urlTemplate": manifest["urlTemplate"],
        "title": manifest["title"],
    }


def build_tiles_for_image(
    image_path: Path,
    image_id: str,
    title: str,
    min_zoom: int,
    max_zoom: int,
    tile_size: int,
) -> TileManifest:
    tile_output_dir = TILES_DIR / image_id
    if tile_output_dir.exists():
        shutil.rmtree(tile_output_dir)
    tile_output_dir.mkdir(parents=True, exist_ok=True)

    run_gdal2tiles(
        image_path=image_path,
        output_dir=tile_output_dir,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        tile_size=tile_size,
    )

    width, height = read_image_size(image_path)
    manifest = generate_manifest(
        image_id=image_id,
        title=title,
        width=width,
        height=height,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        tile_size=tile_size,
    )

    manifest_path = MANIFESTS_DIR / f"{image_id}.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest



def run_gdal2tiles(
    image_path: Path,
    output_dir: Path,
    min_zoom: int,
    max_zoom: int,
    tile_size: int,
) -> None:
    cmd = [
        sys.executable,
        "-m",
        "osgeo_utils.gdal2tiles",
        "--profile",
        "raster",
        "--xyz",
        "--zoom",
        f"{min_zoom}-{max_zoom}",
        "--tilesize",
        str(tile_size),
        str(image_path),
        str(output_dir),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "gdal2tiles 执行失败",
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )



def generate_manifest(
    image_id: str,
    title: str,
    width: int,
    height: int,
    min_zoom: int,
    max_zoom: int,
    tile_size: int,
) -> TileManifest:
    extent = [0.0, float(-height), float(width), 0.0]
    origin = [0.0, 0.0]
    center = [width / 2, -height / 2]
    resolutions = compute_resolutions(width, height, min_zoom, max_zoom, tile_size)
    initial_resolution = resolutions[0]
    manifest_url = f"/api/tiles/{image_id}/manifest"
    url_template = f"/api/tiles/{image_id}/{{z}}/{{x}}/{{y}}.png"

    return TileManifest(
        id=image_id,
        title=title,
        width=width,
        height=height,
        minZoom=min_zoom,
        maxZoom=max_zoom,
        tileSize=tile_size,
        extent=extent,
        origin=origin,
        resolutions=resolutions,
        center=center,
        initialResolution=initial_resolution,
        urlTemplate=url_template,
        manifestUrl=manifest_url,
    )



def compute_resolutions(
    width: int,
    height: int,
    min_zoom: int,
    max_zoom: int,
    tile_size: int,
) -> List[float]:
    levels = max_zoom - min_zoom + 1
    max_dim = max(width, height)
    base = max(max_dim / tile_size, 1)
    start_resolution = float(2 ** math.ceil(math.log2(base)))
    resolutions = [start_resolution / (2 ** i) for i in range(levels)]
    return resolutions



def read_image_size(image_path: Path) -> tuple[int, int]:
    with Image.open(image_path) as img:
        return img.width, img.height



def normalize_image_id(image_id: Optional[str]) -> str:
    raw = image_id or uuid.uuid4().hex
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))
    if not cleaned:
        raise HTTPException(status_code=400, detail="image_id 非法")
    return cleaned



def validate_zoom_and_tile_size(min_zoom: int, max_zoom: int, tile_size: int) -> None:
    if min_zoom < 0 or max_zoom < 0 or max_zoom < min_zoom:
        raise HTTPException(status_code=400, detail="zoom 参数不合法")
    if tile_size <= 0:
        raise HTTPException(status_code=400, detail="tile_size 必须大于 0")
