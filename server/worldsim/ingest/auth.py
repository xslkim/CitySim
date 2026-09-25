"""摄入 API 鉴权（07 T-SYN-05；04 §9.1：Bearer token，服务侧只存/只比 SHA-256 哈希，零硬编码）。

哈希来源：`WSIM_INGEST_TOKEN_HASH`（上线形态，04 §1.5 cloud.env）；
开发期未配置时由 `WSIM_INGEST_TOKEN` 进程启动时派生哈希（仅内存，不落盘不打印——工程默认，
登记 07 偏差表 D2 同组）。
"""

from __future__ import annotations

import hashlib
import hmac
import os


def expected_token_hash() -> str:
    h = os.environ.get("WSIM_INGEST_TOKEN_HASH")
    if h:
        return h.strip().lower()
    token = os.environ.get("WSIM_INGEST_TOKEN")
    if token:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
    raise RuntimeError("摄入 API 缺 WSIM_INGEST_TOKEN_HASH（或开发期 WSIM_INGEST_TOKEN，04 §9.1）")


def token_ok(token: str, *, expected_hash: str) -> bool:
    """恒定时间比较 token 的 SHA-256（服务侧不存原文，04 §9.1）。"""
    if not token:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, expected_hash)


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""
