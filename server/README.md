# WorldSim server

Python 3.12（uv 管理）内核项目：模拟内核 + LLM 网关 + 摄入 API（M6）+ 观察端 API（M4）。
包结构按 `docs/design/04-服务器详细设计.md` §2.1，M0 仅骨架（各包仅 `__init__.py`），实现随里程碑填入。

## 约定（docs/tasks/00-总览与里程碑.md §1 A14）

- 一切 Python 执行走 `cd server && uv run ...`，禁止裸 `python`/`python3`。
- 环境变量见仓库根 `.env`（唯一清单 = 00 §2 附表）；配置文件见 `config/`。
- 数据库：`server/scripts/db_init.sh {init|start|stop|status|reset}`（PG 16.15 源码装于 `~/pgsql`，socket `/tmp` + `127.0.0.1:5432`）。

## 常用命令

```bash
cd server
uv sync                              # 安装依赖（含 dev 组）
uv run pytest                        # 全量测试（importmode=importlib）
uv run python scripts/gen_event_types.py        # 派生注册表下游镜像（T-CFG-05）
uv run python scripts/gen_event_types.py --check # 漂移检查（纯消费方 04 T-WA-10）
```
