from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import uuid
from io import BytesIO
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from PIL import Image, UnidentifiedImageError

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
TILES_DIR = DATA_DIR / "tiles"
MANIFESTS_DIR = DATA_DIR / "manifests"

DEFAULT_TILE_SIZE = 256
DEFAULT_TITLE = "tile image"
DEFAULT_MIN_ZOOM = 0

for directory in (IMAGES_DIR, TILES_DIR, MANIFESTS_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Tile Service", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Models
# =========================

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

    # 扩展字段
    tileFormat: str = "png"
    scheme: str = "xyz"
    projection: str = "pixel"
    bounds: List[float]
    generatedBy: str = "gdal2tiles"


class CreateTileResponse(BaseModel):
    imageId: str
    manifest: TileManifest


# =========================
# API
# =========================

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/tiles", response_model=CreateTileResponse)
async def create_tiles(
    request: Request,
    file: UploadFile = File(...),
    tile_size: int = Form(DEFAULT_TILE_SIZE),
    image_id: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    min_zoom: Optional[int] = Form(None),
    max_zoom: Optional[int] = Form(None),
):
    validate_tile_size(tile_size)
    validate_optional_zoom_inputs(min_zoom, max_zoom)

    image_id = normalize_image_id(image_id)
    title = title or file.filename or DEFAULT_TITLE

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="上传文件为空")

    suffix = guess_image_suffix(file.filename, raw_bytes)
    image_path = IMAGES_DIR / f"{image_id}{suffix}"

    # ✅ 先校验图片，再保存
    width, height = validate_and_save_image(raw_bytes, image_path)

    manifest = build_tiles_for_image(
        request=request,
        image_path=image_path,
        image_id=image_id,
        title=title,
        tile_size=tile_size,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        width=width,
        height=height,
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

    return FileResponse(
        tile_path,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


# =========================
# Core Logic
# =========================

def build_tiles_for_image(
    request: Request,
    image_path: Path,
    image_id: str,
    title: str,
    tile_size: int,
    min_zoom: Optional[int],
    max_zoom: Optional[int],
    width: int,
    height: int,
) -> TileManifest:

    auto_min, auto_max = compute_zoom_levels(width, height, tile_size)

    min_zoom = auto_min if min_zoom is None else min_zoom
    max_zoom = auto_max if max_zoom is None else max_zoom

    validate_zoom_range(min_zoom, max_zoom)

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

    manifest = generate_manifest(
        request=request,
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


def run_gdal2tiles(image_path, output_dir, min_zoom, max_zoom, tile_size):
    cmd = [
        sys.executable,
        "-m",
        "osgeo_utils.gdal2tiles",
        "--profile", "raster",
        "--xyz",
        "--zoom", f"{min_zoom}-{max_zoom}",
        "--tilesize", str(tile_size),
        str(image_path),
        str(output_dir),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=result.stderr
        )


def generate_manifest(request, image_id, title, width, height, min_zoom, max_zoom, tile_size):
    extent = [0.0, float(-height), float(width), 0.0]
    origin = [0.0, 0.0]
    center = [width / 2, -height / 2]
    bounds = [0.0, 0.0, float(width), float(height)]

    resolutions = compute_resolutions(width, height, min_zoom, max_zoom, tile_size)
    initial_resolution = resolutions[0]

    base_url = str(request.base_url).rstrip("/")

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
        urlTemplate=f"{base_url}/api/tiles/{image_id}/{{z}}/{{x}}/{{y}}.png",
        manifestUrl=f"{base_url}/api/tiles/{image_id}/manifest",
        bounds=bounds,
    )


# =========================
# Utils
# =========================

def compute_zoom_levels(width, height, tile_size):
    max_dim = max(width, height)
    min_zoom = DEFAULT_MIN_ZOOM
    max_zoom = int(math.ceil(math.log2(max(max_dim / tile_size, 1))))
    return min_zoom, max_zoom


def compute_resolutions(width, height, min_zoom, max_zoom, tile_size):
    levels = max_zoom - min_zoom + 1
    max_dim = max(width, height)
    base = max(max_dim / tile_size, 1)
    start = float(2 ** math.ceil(math.log2(base)))
    return [start / (2 ** i) for i in range(levels)]


def validate_and_save_image(raw_bytes: bytes, image_path: Path):
    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            img.verify()
        with Image.open(BytesIO(raw_bytes)) as img:
            width, height = img.size
    except UnidentifiedImageError:
        raise HTTPException(status_code=400, detail="不是合法图片文件")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    with image_path.open("wb") as f:
        f.write(raw_bytes)

    return width, height


def guess_image_suffix(filename, raw_bytes):
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix:
            return suffix

    if raw_bytes.startswith(b"\x89PNG"):
        return ".png"
    if raw_bytes.startswith(b"\xff\xd8"):
        return ".jpg"

    return ".png"


def normalize_image_id(image_id):
    raw = image_id or uuid.uuid4().hex
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))
    if not cleaned:
        raise HTTPException(status_code=400, detail="非法 image_id")
    return cleaned


def validate_tile_size(tile_size):
    if tile_size <= 0:
        raise HTTPException(status_code=400, detail="tile_size 必须大于 0")


def validate_zoom_range(min_zoom, max_zoom):
    if min_zoom < 0 or max_zoom < min_zoom:
        raise HTTPException(status_code=400, detail="zoom 参数不合法")


def validate_optional_zoom_inputs(min_zoom, max_zoom):
    if min_zoom is None and max_zoom is None:
        return
    if min_zoom is None or max_zoom is None:
        raise HTTPException(status_code=400, detail="zoom 要么都传，要么不传")
    validate_zoom_range(min_zoom, max_zoom)