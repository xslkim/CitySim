"""WorldSim 观察端 API（obs-api，M4，05 T-WEB-02 起）。

FastAPI + uvicorn（00 §3/03 §8.1）；数据源 = 主库 `obs` schema 白名单视图层（00 §1 A5，
M6 切副本 DSN 零代码变更）；协议 = 03 §5.1（REST 响应包 `{ok,data,meta.watermark_tick}`）+
03 §5.2（WS `/ws`）；鉴权 = 03 §8.3 per-user token（10 行容量，obs-api 自管 SQLite）。
"""
