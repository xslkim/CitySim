"""T-ART-03 静态资源三挂载测试：头像/stream 页/map_layout.json + text_raw 零出站 + 目录穿越。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from worldsim.observe.app import create_app
from worldsim.observe.static import ASSETS_DIR, MAP_LAYOUT, STREAM_DIR


def _client() -> TestClient:
    return TestClient(create_app(pool=None, token_db_path=":memory:"))


def test_portrait_png_served() -> None:
    resp = _client().get("/assets/portraits/linwan.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


def test_stream_index_served_no_cache() -> None:
    resp = _client().get("/stream/")
    assert resp.status_code == 200
    assert "AI 生成内容" in resp.text
    assert resp.headers.get("cache-control") == "no-cache"


def test_manifest_served() -> None:
    resp = _client().get("/assets/portraits/manifest.json")
    assert resp.status_code == 200
    assert json.loads(resp.text)["A01"]["file"] == "linwan.png"


def test_map_layout_served_no_cache() -> None:
    resp = _client().get("/map_layout.json")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    assert resp.headers.get("cache-control") == "no-cache"
    site_ids = {s["id"] for s in resp.json()["sites"]}
    assert "offsite" in site_ids  # 03 §3.1


def test_traversal_blocked() -> None:
    client = _client()
    assert client.get("/assets/../server/pyproject.toml").status_code == 404
    assert client.get("/assets/%2e%2e/server/pyproject.toml").status_code == 404
    assert client.get("/stream/../server/pyproject.toml").status_code == 404


def test_no_text_raw_served() -> None:
    """遍历两挂载根下 .json/.html/.js 文件与 /map_layout.json 响应，断言不含 text_raw（红线 7）。"""
    for root in (ASSETS_DIR, STREAM_DIR):
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in (".json", ".html", ".js"):
                assert "text_raw" not in p.read_text(encoding="utf-8"), p
    resp = _client().get("/map_layout.json")
    assert "text_raw" not in resp.text
    assert "text_raw" not in MAP_LAYOUT.read_text(encoding="utf-8")
