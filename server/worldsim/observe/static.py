"""T-ART-03 头像与静态资源路由（06 §7 B8：03 §8.1 拓扑无此三路由，开发期 obs-api 挂载，生产 nginx 片段见 deploy/nginx-stream.conf）。

三挂载（路径白名单，仅此三根 + 单文件，StaticFiles 自身防目录穿越）：
- `/assets/` → `ui/assets/`（头像/特写/manifest/palette，web 端与直播页共用同一资源目录，00 §2）
- `/stream/` → `stream/`（直播画面页，html=True；`.html` 响应 `Cache-Control: no-cache`，03 §8.1 口径）
- `/map_layout.json` → `web/public/map_layout.json` 单文件（round1-fixes §C 增项；`no-cache`）

资产 URL 形态 `/assets/portraits/<file>`；页面内引用一律走该绝对路径（与 web 端同构）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parents[3]
ASSETS_DIR = REPO_ROOT / "ui" / "assets"
STREAM_DIR = REPO_ROOT / "stream"
MAP_LAYOUT = REPO_ROOT / "web" / "public" / "map_layout.json"

router = APIRouter()


class NoCacheHtmlStaticFiles(StaticFiles):
    """StaticFiles + 缓存头（03 §8.1）：`.html` 恒 `no-cache`；`no_cache_all=True`（stream/ 无构建
    hash 资源，开发期全量 no-cache）时所有响应 `no-cache`。带 hash 资源 immutable 一档本仓不适用。"""

    def __init__(self, *args, no_cache_all: bool = False, **kwargs):  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self._no_cache_all = no_cache_all

    async def get_response(self, path: str, scope):  # noqa: ANN001, ANN202 - starlette 签名
        resp = await super().get_response(path, scope)
        if resp.status_code == 200 and (
            self._no_cache_all or resp.headers.get("content-type", "").startswith("text/html")
        ):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


@router.get("/map_layout.json", include_in_schema=False)
async def map_layout() -> FileResponse:
    """单文件挂载（03 §3.1 结构；源 = 05 T-WEB-13 web/public/map_layout.json，与 web 端同源同文件）。"""
    return FileResponse(MAP_LAYOUT, media_type="application/json",
                        headers={"Cache-Control": "no-cache"})


def mount(app) -> None:  # noqa: ANN001, ANN202 - FastAPI
    """静态目录挂载（app.py 装配循环调用；挂载顺序在 API/WS 路由之后）。"""
    app.mount("/assets", NoCacheHtmlStaticFiles(directory=str(ASSETS_DIR)), name="assets")
    app.mount("/stream", NoCacheHtmlStaticFiles(directory=str(STREAM_DIR), html=True,
                                                no_cache_all=True), name="stream")
