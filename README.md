# FastAPI Tile Service

这个项目把你原来的 `make_tiles.py` 改造成了一个可运行的 FastAPI 服务，支持：

- 上传图片后在线切片
- 从本地已有图片生成切片
- 自动生成 `manifest`
- 通过 HTTP/HTTPS 路由返回瓦片
- 给前端提供统一的 `manifestUrl` 和 `urlTemplate`

## 目录结构

```text
fastapi_tile_service/
  app/
    main.py
  data/
    images/
    manifests/
    tiles/
  requirements.txt
  README.md
```

## 安装

建议使用 Python 3.11+。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如果你的环境里安装 GDAL 比较麻烦，也可以先单独处理：

```bash
pip install GDAL
```

要求 `python -m osgeo_utils.gdal2tiles` 能正常执行。

## 启动 HTTP

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## 启动 HTTPS

你可以直接给 uvicorn 传证书：

```bash
uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8443 \
  --ssl-certfile ./certs/fullchain.pem \
  --ssl-keyfile ./certs/privkey.pem
```

生产环境更推荐放在 Nginx / Caddy / Traefik 后面，由反向代理处理 HTTPS。

## 接口

### 1. 健康检查

```http
GET /health
```

### 2. 上传图片并切片

```http
POST /api/tiles
Content-Type: multipart/form-data
```

表单参数：

- `file`: 图片文件，必填
- `min_zoom`: 默认 0
- `max_zoom`: 默认 5
- `tile_size`: 默认 256
- `image_id`: 可选，不传就自动生成
- `title`: 可选

curl 示例：

```bash
curl -X POST "http://127.0.0.1:8000/api/tiles" \
  -F "file=@./images/page_1.png" \
  -F "min_zoom=0" \
  -F "max_zoom=5" \
  -F "tile_size=256" \
  -F "image_id=page_1"
```

### 3. 从本地图片生成切片

```http
POST /api/tiles/from-local
Content-Type: application/json
```

请求体：

```json
{
  "image_path": "D:/code/utils/make_tiles/images/page_1.png",
  "image_id": "page_1",
  "title": "page_1.png",
  "min_zoom": 0,
  "max_zoom": 5,
  "tile_size": 256
}
```

### 4. 获取 manifest

```http
GET /api/tiles/{image_id}/manifest
```

返回示例：

```json
{
  "id": "page_1",
  "title": "page_1.png",
  "width": 3572,
  "height": 5052,
  "minZoom": 0,
  "maxZoom": 5,
  "tileSize": 256,
  "extent": [0, -5052, 3572, 0],
  "origin": [0, 0],
  "resolutions": [32, 16, 8, 4, 2, 1],
  "center": [1786, -2526],
  "initialResolution": 32,
  "urlTemplate": "/api/tiles/page_1/{z}/{x}/{y}.png",
  "manifestUrl": "/api/tiles/page_1/manifest"
}
```

### 5. 获取瓦片

```http
GET /api/tiles/{image_id}/{z}/{x}/{y}.png
```

例如：

```http
GET /api/tiles/page_1/0/0/0.png
```

## 前端接入方式

前端不要再手写 `extent`、`center`、`resolutions`。应该先请求：

```http
GET /api/tiles/page_1/manifest
```

再用返回值初始化 OpenLayers：

- `extent`
- `origin`
- `resolutions`
- `center`
- `initialResolution`
- `urlTemplate`

## manifest 生成规则

服务端会根据原图尺寸自动生成：

- `extent = [0, -height, width, 0]`
- `origin = [0, 0]`
- `center = [width / 2, -height / 2]`
- `resolutions = 2 倍递减序列`
- `initialResolution = resolutions[0]`

## 你原脚本对应的改造点

你原来的脚本只有一个离线入口：

- 固定输入图片
- 固定输出目录
- 调用 `gdal2tiles`

现在改造后：

- `run_gdal2tiles()` 还在
- 只是被封装进 API 服务
- 切片后自动读取图片宽高
- 自动生成 `manifest`
- 自动提供瓦片路由

## 生产建议

- 瓦片目录可以挂载到持久化存储
- 大图片建议异步切片，不要阻塞 HTTP 请求
- 私有资源建议在接口层加鉴权
- 高并发场景建议把瓦片放到 OSS/S3/CDN，再把 `urlTemplate` 指到 CDN 域名
