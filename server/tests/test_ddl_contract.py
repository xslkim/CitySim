"""T-DB-02 DDL 契约测试（append-only / id 形态 / 枚举 / 字段类型），防 DDL 漂移。

全部用例跑 worldsim_test（conftest `test_db_dsn`：superuser 建库即灌 ddl/schema_v1.sql；
表 owner = superuser，GRANT/REVOKE 对 worldsim 真实生效）。
违反型用例断言 asyncpg 抛错：触发器 RAISE EXCEPTION → RaiseError；
权限 → InsufficientPrivilegeError；CHECK → CheckViolationError。
用例间行集隔离：agents 用 id 段 A01/A40（id 形态）、A02/A03（debts）；events 不引用 agents。
"""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

AGENT_ROW = """
    INSERT INTO agents (id, name, gender, age, persona, needs, position)
    VALUES ($1, '测试', 'M', 30, '{}', '{}', 'apt.L2.203')
"""
EVENT_ROW = """
    INSERT INTO events (tick, sim_time, type, source, trigger, payload)
    VALUES ($1, now(), $2, 'system', $3, '{}')
    RETURNING seq
"""


async def _insert_event(conn: asyncpg.Connection, tick: int, trigger: str = "system") -> int:
    return await conn.fetchval(EVENT_ROW, tick, f"time.paused", trigger)


class TestAppendOnly:
    async def test_update_rejected(self, test_db_dsn: str) -> None:
        """以表 owner 执行 UPDATE 亦被触发器 RAISE EXCEPTION 拒绝（04 §5.2 双保险之一）。"""
        conn = await asyncpg.connect(test_db_dsn)
        try:
            seq = await _insert_event(conn, 9001)
            with pytest.raises(asyncpg.RaiseError, match="events is append-only"):
                await conn.execute("UPDATE events SET tick = tick + 1 WHERE seq = $1", seq)
            assert await conn.fetchval("SELECT tick FROM events WHERE seq = $1", seq) == 9001
        finally:
            await conn.close()

    async def test_delete_rejected(self, test_db_dsn: str) -> None:
        """以表 owner 执行 DELETE 亦被触发器拒绝。"""
        conn = await asyncpg.connect(test_db_dsn)
        try:
            seq = await _insert_event(conn, 9002)
            with pytest.raises(asyncpg.RaiseError, match="events is append-only"):
                await conn.execute("DELETE FROM events WHERE seq = $1", seq)
            assert await conn.fetchval("SELECT count(*) FROM events WHERE seq = $1", seq) == 1
        finally:
            await conn.close()

    async def test_revoke_enforced(self, test_db_dsn: str) -> None:
        """SET ROLE worldsim：UPDATE/DELETE 报权限错误（REVOKE），SELECT/INSERT 正常。"""
        conn = await asyncpg.connect(test_db_dsn)
        try:
            seq = await _insert_event(conn, 9003)
            await conn.execute("SET ROLE worldsim")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE events SET tick = tick + 1 WHERE seq = $1", seq)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("DELETE FROM events WHERE seq = $1", seq)
            assert await conn.fetchval("SELECT count(*) FROM events WHERE seq = $1", seq) == 1
            new_seq = await _insert_event(conn, 9004)
            assert new_seq > seq
        finally:
            await conn.close()


class TestAgentIdCheck:
    @pytest.mark.parametrize("bad_id", ["A00", "A41", "a01", "ag07", "7"])
    async def test_agent_id_rejected(self, test_db_dsn: str, bad_id: str) -> None:
        """'A00'/'A41'/'a01'/'ag07'/'7'（TEXT 化）插入 agents 全被拒（00 §4 红线 1）。"""
        conn = await asyncpg.connect(test_db_dsn)
        try:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(AGENT_ROW, bad_id)
        finally:
            await conn.close()

    @pytest.mark.parametrize("ok_id", ["A01", "A40"])
    async def test_agent_id_accepted(self, test_db_dsn: str, ok_id: str) -> None:
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute(AGENT_ROW, ok_id)
            assert await conn.fetchval("SELECT id FROM agents WHERE id = $1", ok_id) == ok_id
        finally:
            await conn.close()


class TestTriggerEnum:
    @pytest.mark.parametrize("trg", ["autonomous", "world", "director", "gift", "vote", "system"])
    async def test_trigger_enum(self, test_db_dsn: str, trg: str) -> None:
        """06 §1.1 六枚举逐个 INSERT 通过（含 system）。"""
        conn = await asyncpg.connect(test_db_dsn)
        try:
            seq = await _insert_event(conn, 9100, trg)
            assert await conn.fetchval("SELECT trigger FROM events WHERE seq = $1", seq) == trg
        finally:
            await conn.close()

    @pytest.mark.parametrize("bad", ["sys", "gm"])
    async def test_trigger_enum_rejected(self, test_db_dsn: str, bad: str) -> None:
        conn = await asyncpg.connect(test_db_dsn)
        try:
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_event(conn, 9101, bad)
        finally:
            await conn.close()


class TestVisibility:
    async def test_visibility_default_internal(self, test_db_dsn: str) -> None:
        """不填 visibility 落库为 'internal'（05 §2 默认剥除口径的落库面）。"""
        conn = await asyncpg.connect(test_db_dsn)
        try:
            seq = await _insert_event(conn, 9200)
            assert await conn.fetchval("SELECT visibility FROM events WHERE seq = $1", seq) == "internal"
        finally:
            await conn.close()

    @pytest.mark.parametrize("vis", ["public", "internal"])
    async def test_visibility_accepted(self, test_db_dsn: str, vis: str) -> None:
        conn = await asyncpg.connect(test_db_dsn)
        try:
            seq = await conn.fetchval(
                """
                INSERT INTO events (tick, sim_time, type, source, trigger, payload, visibility)
                VALUES (9201, now(), 'time.paused', 'system', 'system', '{}', $1)
                RETURNING seq
                """,
                vis,
            )
            assert await conn.fetchval("SELECT visibility FROM events WHERE seq = $1", seq) == vis
        finally:
            await conn.close()

    async def test_visibility_rejected(self, test_db_dsn: str) -> None:
        conn = await asyncpg.connect(test_db_dsn)
        try:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO events (tick, sim_time, type, source, trigger, payload, visibility)
                    VALUES (9202, now(), 'time.paused', 'system', 'system', '{}', 'block')
                    """
                )
        finally:
            await conn.close()


class TestDebtsChecks:
    async def _mk_pair(self, conn: asyncpg.Connection) -> None:
        for aid in ("A02", "A03"):
            await conn.execute(AGENT_ROW, aid)

    async def test_debts_checks(self, test_db_dsn: str) -> None:
        """repaid>amount / a_id=b_id / amount<=0 三例全被拒；合法行（含方向 a 债权→b 债务）通过。"""
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await self._mk_pair(conn)
            base = """
                INSERT INTO debts (a_id, b_id, amount_cents, due_sim, repaid_cents, created_tick)
                VALUES ('A02', 'A03', $1, now() + interval '14 days', $2, 0)
            """
            with pytest.raises(asyncpg.CheckViolationError):  # repaid_cents > amount_cents
                await conn.execute(base, 100, 101)
            with pytest.raises(asyncpg.CheckViolationError):  # a_id = b_id
                await conn.execute(
                    """
                    INSERT INTO debts (a_id, b_id, amount_cents, due_sim, created_tick)
                    VALUES ('A02', 'A02', 100, now(), 0)
                    """
                )
            with pytest.raises(asyncpg.CheckViolationError):  # amount_cents <= 0
                await conn.execute(base, 0, 0)
            await conn.execute(base, 400000, 0)
            row = await conn.fetchrow("SELECT a_id, b_id, amount_cents FROM debts WHERE a_id = 'A02'")
            assert (row["a_id"], row["b_id"], row["amount_cents"]) == ("A02", "A03", 400000)
        finally:
            await conn.close()
