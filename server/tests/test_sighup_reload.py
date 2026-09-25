"""T-OPS-07 SIGHUP 热更验收（04 §12.4：价格/阈值/变速表段边界；失败保留旧配置；不暂停时钟）。

M6 收口：systemctl reload 等价 = kill -HUP（systemd 模板 ExecReload 已接，T-OPS-04）。
"""

from __future__ import annotations

import datetime as dt

import asyncpg
import pytest
import yaml

pytestmark = pytest.mark.asyncio

_T0 = dt.datetime(2026, 9, 25, 8, 0, tzinfo=dt.timezone.utc)
_DAY = dt.timedelta(days=1)


REAL_MODELS = __import__("pathlib").Path(__file__).resolve().parents[1] / "config" / "models.yaml"


def _write_models(path, *, in_price: float, alarm: float = 1.5, throttle: float = 2.0,
                  recover: float = 1.3) -> None:
    """以真实 models.yaml 为底改派（满足九路由/七行参数冻结校验，01 T-CFG-02）。"""
    cfg = yaml.safe_load(REAL_MODELS.read_text(encoding="utf-8"))
    for m in cfg["providers"]["zhipu"]["chat"]:
        if m["id"] == "glm-4.5-flash":
            m["input_price"] = in_price
            m["output_price"] = in_price
    cfg["thresholds"]["cost_breaker"] = {"alarm_ratio": alarm, "throttle_ratio": throttle,
                                         "recover_ratio": recover}
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


async def test_price_change_applies_to_new_calls(tmp_path) -> None:
    """改价后新调用按新价计费（models.yaml 热更 → price_of 新值 → 04 §8.3 公式）。"""
    from worldsim.llm_gateway.ledger import cost_micro_cny
    from worldsim.llm_gateway.router import ModelRouter

    p = tmp_path / "models.yaml"
    _write_models(p, in_price=2.0)
    router = ModelRouter(yaml.safe_load(p.read_text(encoding="utf-8")), path=str(p))
    in_p, out_p = router.price_of("zhipu", "glm-4.5-flash")
    cost_old = cost_micro_cny(1000, 500, in_p, out_p)
    _write_models(p, in_price=4.0)
    assert router.reload() is True
    in_p, out_p = router.price_of("zhipu", "glm-4.5-flash")
    cost_new = cost_micro_cny(1000, 500, in_p, out_p)
    assert cost_new == 2 * cost_old == 2 * (1000 * 2.0 + 500 * 2.0)


async def test_threshold_change_applies(test_db_dsn: str, tmp_path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """改熔断比率 → 判级用新值；改 health_thresholds.yaml → T-AUD-08 判定用新值（读即生效）。"""
    from worldsim.llm_gateway.breaker import CostBreaker

    p = tmp_path / "models.yaml"
    _write_models(p, in_price=0, throttle=2.0)
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        cb = CostBreaker(pool, yaml.safe_load(p.read_text())["thresholds"], baseline_cny=10.0)
        # 旧阈 2.0：1.8× 持续两日只到 alarm
        await cb.evaluate(18.0, sim_now=_T0)
        assert await cb.evaluate(18.0, sim_now=_T0 + _DAY) == "alarm"
        # 热更 throttle 阈 1.7 → 同序列下一日即升 throttle
        _write_models(p, in_price=0, throttle=1.7)
        assert cb.reload_thresholds(yaml.safe_load(p.read_text())["thresholds"]) is True
        assert await cb.evaluate(18.0, sim_now=_T0 + 2 * _DAY) == "alarm"   # 新 band 刚进，未持续
        assert await cb.evaluate(18.0, sim_now=_T0 + 3 * _DAY) == "throttle"  # 持续满 → 新阈生效
        # health_thresholds.yaml：改阈值后 classify 用新值（load_thresholds 无缓存读即生效）
        from worldsim.audit import metrics
        real_cfg = metrics.load_thresholds()
        patched = [dict(m) for m in real_cfg["metrics"]]
        target = next(m for m in patched if m["metric"] == "appearance_gini")
        target["healthy"] = {"max": 0.0001, "_closed": True}
        tp = tmp_path / "health_thresholds.yaml"
        tp.write_text(yaml.safe_dump({"metrics": patched}, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(metrics, "THRESHOLDS_PATH", tp)
        assert metrics.classify("appearance_gini", 0.5) != "healthy"  # 新阈下 0.5 不再健康
    finally:
        await pool.close()


async def test_bad_yaml_keeps_old_and_warns(tmp_path, caplog) -> None:
    """reload 校验失败 → 保留旧配置 + WARN（04 §12.4）。"""
    from worldsim.llm_gateway.breaker import CostBreaker
    from worldsim.llm_gateway.router import ModelRouter

    p = tmp_path / "models.yaml"
    _write_models(p, in_price=2.0)
    router = ModelRouter(yaml.safe_load(p.read_text()), path=str(p))
    p.write_text("{{{ 非法 yaml", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert router.reload() is False
    assert any("保留旧配置" in r.message for r in caplog.records)
    assert router.price_of("zhipu", "glm-4.5-flash")[0] == 2.0  # 旧配置保留

    cb = CostBreaker(None, {"cost_breaker": {"alarm_ratio": 1.5, "throttle_ratio": 2.0,
                                             "recover_ratio": 1.3}}, baseline_cny=10.0)
    assert cb.reload_thresholds({"cost_breaker": {"alarm_ratio": "abc"}}) is False
    assert cb._throttle_ratio == 2.0


def test_speed_table_defers_to_segment_boundary(tmp_path) -> None:
    """变速表热更在下一个段边界生效，当前段不中断（04 §3.1/§12.4）。"""
    from worldsim.time_engine.speed_table import SpeedTableReloader, load

    src = tmp_path / "speed_table.yaml"
    seg_v1 = {
        "segments": [{"start": "00:00", "end": "24:00", "mode": "continuous", "ratio": 3.0}],
        "constraints": {"min_sim_days_per_real_week": 1, "max_catchup_ratio": 6.0},
    }
    src.write_text(yaml.safe_dump(seg_v1), encoding="utf-8")
    table = load(str(src))
    reloader = SpeedTableReloader(str(src), table)
    seg_v2 = {
        "segments": [{"start": "00:00", "end": "24:00", "mode": "continuous", "ratio": 1.0}],
        "constraints": {"min_sim_days_per_real_week": 1, "max_catchup_ratio": 6.0},
    }
    src.write_text(yaml.safe_dump(seg_v2), encoding="utf-8")
    reloader.request_reload()  # SIGHUP handler 只挂起
    assert reloader.current.segments[0].ratio == 3.0  # 当前段不中断（挂起未生效）
    assert reloader.maybe_reload_at_boundary() is True  # 段边界生效
    assert reloader.current.segments[0].ratio == 1.0


async def test_no_pause_during_reload(test_db_dsn: str, tmp_path) -> None:
    """全程无 time.paused 事件（验收 2：reload 不暂停时钟）。"""
    from worldsim.llm_gateway.router import ModelRouter

    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        before = await pool.fetchval("SELECT count(*) FROM events WHERE type='time.paused'")
        p = tmp_path / "models.yaml"
        _write_models(p, in_price=2.0)
        router = ModelRouter(yaml.safe_load(p.read_text()), path=str(p))
        _write_models(p, in_price=3.0)
        router.reload()
        after = await pool.fetchval("SELECT count(*) FROM events WHERE type='time.paused'")
        assert after == before
    finally:
        await pool.close()
