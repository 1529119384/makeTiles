from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from PIL import Image

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

app = FastAPI(title="Tile Service", version="2.0.0")

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
    tileFormat: str = "png"
    scheme: str = "xyz"
    projection: str = "pixel"
    bounds: List[float]
    generatedBy: str = "osgeo_utils.gdal2tiles"


class CreateTileResponse(BaseModel):
    imageId: str
    manifest: TileManifest


class ExistingImageRequest(BaseModel):
    image_path: str
    image_id: Optional[str] = None
    title: Optional[str] = None
    tile_size: int = DEFAULT_TILE_SIZE
    min_zoom: Optional[int] = None
    max_zoom: Optional[int] = None


class TileSummary(BaseModel):
    imageId: str
    title: str
    manifestUrl: str
    urlTemplate: str


@app.get("/health")
def health() -> dict:
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

    suffix = Path(file.filename or "").suffix or ".png"
    image_id = normalize_image_id(image_id)
    title = title or file.filename or DEFAULT_TITLE

    image_path = IMAGES_DIR / f"{image_id}{suffix}"
    with image_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    manifest = build_tiles_for_image(
        request=request,
        image_path=image_path,
        image_id=image_id,
        title=title,
        tile_size=tile_size,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
    )
    return CreateTileResponse(imageId=image_id, manifest=manifest)


@app.post("/api/tiles/from-local", response_model=CreateTileResponse)
def create_tiles_from_local(payload: ExistingImageRequest, request: Request):
    validate_tile_size(payload.tile_size)
    validate_optional_zoom_inputs(payload.min_zoom, payload.max_zoom)

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
        request=request,
        image_path=stored_image_path,
        image_id=image_id,
        title=title,
        tile_size=payload.tile_size,
        min_zoom=payload.min_zoom,
        max_zoom=payload.max_zoom,
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


@app.get("/api/tiles/{image_id}", response_model=TileSummary)
def get_tile_summary(image_id: str):
    manifest_path = MANIFESTS_DIR / f"{image_id}.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="image_id 不存在")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return TileSummary(
        imageId=image_id,
        manifestUrl=manifest["manifestUrl"],
        urlTemplate=manifest["urlTemplate"],
        title=manifest["title"],
    )


@app.get("/api/tiles", response_model=list[TileSummary])
def list_tiles():
    items: list[TileSummary] = []
    for manifest_path in sorted(MANIFESTS_DIR.glob("*.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        items.append(
            TileSummary(
                imageId=manifest["id"],
                title=manifest["title"],
                manifestUrl=manifest["manifestUrl"],
                urlTemplate=manifest["urlTemplate"],
            )
        )
    return items


def build_tiles_for_image(
    request: Request,
    image_path: Path,
    image_id: str,
    title: str,
    tile_size: int,
    min_zoom: Optional[int],
    max_zoom: Optional[int],
) -> TileManifest:
    width, height = read_image_size(image_path)

    auto_min_zoom, auto_max_zoom = compute_zoom_levels(width, height, tile_size)
    final_min_zoom = auto_min_zoom if min_zoom is None else min_zoom
    final_max_zoom = auto_max_zoom if max_zoom is None else max_zoom
    validate_zoom_range(final_min_zoom, final_max_zoom)

    tile_output_dir = TILES_DIR / image_id
    if tile_output_dir.exists():
        shutil.rmtree(tile_output_dir)
    tile_output_dir.mkdir(parents=True, exist_ok=True)

    run_gdal2tiles(
        image_path=image_path,
        output_dir=tile_output_dir,
        min_zoom=final_min_zoom,
        max_zoom=final_max_zoom,
        tile_size=tile_size,
    )

    manifest = generate_manifest(
        request=request,
        image_id=image_id,
        title=title,
        width=width,
        height=height,
        min_zoom=final_min_zoom,
        max_zoom=final_max_zoom,
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
                "cmd": cmd,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )



def generate_manifest(
    request: Request,
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
    bounds = [0.0, 0.0, float(width), float(height)]
    resolutions = compute_resolutions(width, height, min_zoom, max_zoom, tile_size)
    initial_resolution = resolutions[0]

    base_url = str(request.base_url).rstrip("/")
    manifest_url = f"{base_url}/api/tiles/{image_id}/manifest"
    url_template = f"{base_url}/api/tiles/{image_id}/{{z}}/{{x}}/{{y}}.png"

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
        bounds=bounds,
    )



def compute_zoom_levels(width: int, height: int, tile_size: int) -> tuple[int, int]:
    max_dim = max(width, height)
    min_zoom = DEFAULT_MIN_ZOOM
    max_zoom = max(int(math.ceil(math.log2(max(max_dim / tile_size, 1)))), min_zoom)
    return min_zoom, max_zoom



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
    return [start_resolution / (2 ** i) for i in range(levels)]



def read_image_size(image_path: Path) -> tuple[int, int]:
    with Image.open(image_path) as img:
        return img.width, img.height



def normalize_image_id(image_id: Optional[str]) -> str:
    raw = image_id or uuid.uuid4().hex
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))
    if not cleaned:
        raise HTTPException(status_code=400, detail="image_id 非法")
    return cleaned



def validate_tile_size(tile_size: int) -> None:
    if tile_size <= 0:
        raise HTTPException(status_code=400, detail="tile_size 必须大于 0")



def validate_zoom_range(min_zoom: int, max_zoom: int) -> None:
    if min_zoom < 0 or max_zoom < 0 or max_zoom < min_zoom:
        raise HTTPException(status_code=400, detail="zoom 参数不合法")



def validate_optional_zoom_inputs(min_zoom: Optional[int], max_zoom: Optional[int]) -> None:
    if min_zoom is None and max_zoom is None:
        return
    if min_zoom is None or max_zoom is None:
        raise HTTPException(
            status_code=400,
            detail="min_zoom 和 max_zoom 要么都不传，让系统自动计算；要么都传。",
        )
    validate_zoom_range(min_zoom, max_zoom)
