"""双人事件单源写入、目击投影与传播链（02 T-ADJ-05 记忆写入段；04 §6.3/§6.1 step5；06 §2；01 §4.1）。

波次 2a 交付范围（与 T-ADJ-05 波次 2b 主段的挂接面）：事件落库后的**记忆侧写入原语**
（`insert_memory` 唯一写径）+ gossip 失真链纯函数；19 动作事件 → 记忆投影的管道 step5 全量
联动归 T-ADJ-05 波次 2b（本模块函数即其消费面）。

- **单源**：同一场交互只生成一条 events 行（`actors=[A,B]`，管道侧），双方各写 `kind='event'`
  记忆投影——`source_event_seq` 相同、`content` 各自视角一句话（04 §6.3；审计"双人记忆同源"依据）。
- **目击投影**：`position = event.location_id` 且不在 `actors` 内的在场者（LOD 不限）每人一条
  `kind='projection'`（系统模板压缩），重要性 = 原事件 −2（下限 1），`is_witness=true`（06 §2）；
  同节点目击上限 ≤6 人（超出按 rng 确定性抽样，01 §4.1/§7 围观行）；argue 事件 30% 概率产生目击
  投影（01 §4.1，事件级骰子，rng 由 events.rng_seed 派生可回放）。
- **传播链**：凡由事件直接触发的事件 `payload.caused_by='<seq>'`、gossip `payload.cites` 引本人记忆
  id（裸 seq 数字字符串数组，≥1 条，`validate_cites` 强制）——值形态 00 §4 红线 3。
- **失真链**（01 §4.1）：fidelity 初始 1.0、每手 ×0.8、细节丢失阈值 0.7/0.55、极化 30%、关键翻转
  15%（N 高 20%）、张冠李戴 5%、深度上限 4 手；fidelity 为内核传播输入，**不进 payload、不出站**
  （00 §4 红线 8）。
"""

from __future__ import annotations

import datetime as dt
import json
import random
from dataclasses import dataclass
from typing import Any

WITNESS_CAP = 6                    # 同节点目击投影上限 ≤6 人（01 §4.1/§7，评审 P2-10）
ARGUE_WITNESS_PROB = 0.30          # argue 30% 概率产生目击投影（01 §4.1，事件级骰子）
WITNESS_IMPORTANCE_FLOOR = 1       # 目击重要性下限（04 §6.3）
WITNESS_IMPORTANCE_OFFSET = 2      # 目击重要性 = 原事件 −2（04 §6.3）

FIDELITY_INIT = 1.0                # 保真度初始（01 §4.1）
FIDELITY_DECAY = 0.8               # 每过一手 ×0.8（01 §4.1）
FIDELITY_DROP_NUMBERS = 0.7        # <0.7 丢数字/时间字段（01 §4.1）
FIDELITY_SKELETON = 0.55           # <0.55 只保留"谁+干了什么"主干（01 §4.1）
GOSSIP_DEPTH_CAP = 4               # 传播深度上限统一 4 手（01 §4.1 仲裁口径）
POLARIZE_PROB = 0.30               # 极化扭曲概率（01 §4.1）
FLIP_PROB = 0.15                   # 关键翻转概率（01 §4.1）
FLIP_PROB_N_HIGH = 0.20            # N 高（≥70）转述者翻转概率上调（01 §4.1）
MISATTRIBUTE_PROB = 0.05           # 张冠李戴概率（01 §4.1）
N_HIGH_THRESHOLD = 70              # Big Five 高档阈（01 §3.1 档位口径）


# ---- 记忆写入唯一原语（全内核 memories 行一律经此；content_display M1 直写，02 文档 D10） ------------


async def insert_memory(
    pool: Any,
    gateway: Any,
    *,
    agent_id: str,
    sim_time: dt.datetime,
    kind: str,
    content: str,
    importance: int,
    source_event_seq: int | None = None,
    is_witness: bool = False,
    rng_seed: int = 0,
) -> int:
    """写一条记忆（含 embedding；`content_display` M1 直写不过安全管线，M2 起过管线，02 文档 D10）。"""
    vec = await gateway.embed([content], seed=rng_seed, agent_id=agent_id, sim_time=sim_time)
    return await pool.fetchval(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance, embedding, source_event_seq, is_witness)
        VALUES ($1, $2, $3, $4, $4, $5, $6::vector, $7, $8)
        RETURNING id
        """,
        agent_id, sim_time, kind, content, importance,
        "[" + ",".join(repr(v) for v in vec.vectors[0]) + "]", source_event_seq, is_witness,
    )


# ---- 双人事件单源（04 §6.3） --------------------------------------------------


async def write_event_memories(
    pool: Any,
    gateway: Any,
    *,
    event_seq: int,
    sim_time: dt.datetime,
    perspectives: dict[str, str],
    importance: int,
    rng_seed: int,
) -> dict[str, int]:
    """参与者各写 `kind='event'` 记忆投影：同一 `source_event_seq`、各自视角一句话。

    `perspectives` = {agent_id: content}（content 由调用方按各自视角生成，系统模板/LLM 摘要均可）。
    返回 {agent_id: memory_id}。
    """
    if not 1 <= importance <= 10:
        raise ValueError("importance 值域 1~10（04 §5.2 memories CHECK）")
    ids: dict[str, int] = {}
    for agent_id, content in perspectives.items():
        ids[agent_id] = await insert_memory(
            pool, gateway, agent_id=agent_id, sim_time=sim_time, kind="event",
            content=content, importance=importance, source_event_seq=event_seq, rng_seed=rng_seed,
        )
    return ids


# ---- 目击投影（04 §6.3；01 §4.1 argue 目击概率与上限） ----------------------------


async def write_witness_projections(
    pool: Any,
    gateway: Any,
    *,
    event_seq: int,
    event_type: str,
    location_id: str | None,
    actors: list[str],
    sim_time: dt.datetime,
    base_importance: int,
    rng_seed: int,
    summary: str = "",
) -> list[int]:
    """旁观者目击投影。返回写入的记忆 id 列表（无在场者/argue 骰子未中 → 空）。

    - 在场者 = `position = location_id` 且不在 `actors` 内（LOD 不限）；argue 事件先掷事件级
      30% 骰（未中全体无投影）；同节点上限 ≤6 人，超出按 rng 确定性抽样。
    - 重要性 = max(1, 原事件 −2)；`is_witness=true`；内容 = 系统模板压缩（"你看到 A 和 B 在 X 激烈争论"）。
    """
    if location_id is None:
        return []
    rng = random.Random(f"witness:{rng_seed}:{event_seq}")
    if event_type == "dialogue.argue" and rng.random() >= ARGUE_WITNESS_PROB:
        return []  # argue 目击 30% 事件级骰子（01 §4.1）
    witnesses = [
        r["id"]
        for r in await pool.fetch(
            "SELECT id FROM agents WHERE position=$1 AND NOT (id = ANY($2::text[])) ORDER BY id",
            location_id, actors,
        )
    ]
    if len(witnesses) > WITNESS_CAP:
        witnesses = sorted(rng.sample(witnesses, WITNESS_CAP))  # 超出随机抽样取 6（评审 P2-10）
    if not witnesses:
        return []
    names = {
        r["id"]: r["name"]
        for r in await pool.fetch("SELECT id, name FROM agents WHERE id = ANY($1::text[])", actors)
    }
    who = "、".join(names.get(a, a) for a in actors)
    content = f"你看到{who}在{location_id}{summary or '互动'}"
    importance = max(WITNESS_IMPORTANCE_FLOOR, base_importance - WITNESS_IMPORTANCE_OFFSET)
    ids: list[int] = []
    for wid in witnesses:
        ids.append(
            await insert_memory(
                pool, gateway, agent_id=wid, sim_time=sim_time, kind="projection",
                content=content, importance=importance, source_event_seq=event_seq,
                is_witness=True, rng_seed=rng_seed,
            )
        )
    return ids


# ---- gossip 传播链（06 §2；01 §4.1） ----------------------------------------------


async def validate_cites(pool: Any, *, agent_id: str, cites: list[str]) -> list[int]:
    """gossip `payload.cites` 强校验：≥1 条、裸数字字符串数组、必须指向**本人**记忆条目（01 §4.1）。

    返回解析后的记忆 id 列表（int）；不合法抛 ValueError（校验器拦截，04 §6.1 step4）。
    """
    if not cites:
        raise ValueError("gossip cites 必须 ≥1 条（不许无中生有，01 §4.1）")
    for c in cites:
        if not isinstance(c, str) or not c.isdigit():
            raise ValueError(f"cites 元素必须为裸 seq 数字字符串，得到 {c!r}（00 §4 红线 3）")
    ids = [int(c) for c in cites]
    owned = await pool.fetchval(
        "SELECT count(*) FROM memories WHERE id = ANY($1::bigint[]) AND agent_id=$2", ids, agent_id,
    )
    if owned != len(ids):
        raise ValueError("cites 必须指向本人记忆条目 id 列表（01 §4.1）")
    return ids


def caused_by(seq: int) -> str:
    """`payload.caused_by` 值形态 = 裸 seq 数字字符串（00 §4 红线 3；禁止 'e<seq>' 作值）。"""
    return str(int(seq))


# ---- gossip 失真链纯函数（01 §4.1；fidelity 为内核传播输入，不进 payload、不出站，00 §4 红线 8） ----


def fidelity_at_hop(hop: int) -> float:
    """第 hop 手转述的保真度：初始 1.0，每过一手 ×0.8（1.0→0.8→0.64→0.51→0.41，01 §4.1）。"""
    if hop < 1:
        raise ValueError("hop 从 1 起（一手 = 首个转述）")
    return FIDELITY_INIT * (FIDELITY_DECAY ** (hop - 1))


def detail_tier(fidelity: float) -> str:
    """细节保留档：'full' / 'no_details'（<0.7 丢数字时间字段）/ 'skeleton'（<0.55 只留主干）。"""
    if fidelity < FIDELITY_SKELETON:
        return "skeleton"
    if fidelity < FIDELITY_DROP_NUMBERS:
        return "no_details"
    return "full"


def can_retell(current_hop: int) -> bool:
    """传播深度上限统一 4 手（01 §4.1）：当前第 4 手不得再转述。"""
    return current_hop + 1 <= GOSSIP_DEPTH_CAP


@dataclass(frozen=True)
class Distortion:
    """一手转述的失真骰结果（01 §4.1 第 3~5 条）。"""

    polarize: bool       # 30%：情绪极性放大一档
    flip: bool           # 15%（N 高 20%）：关键事实翻转
    misattribute: bool   # 5%：主角错记为同场景另一人


def roll_distortion(rng: random.Random, *, neuroticism: float | None = None) -> Distortion:
    """按 01 §4.1 概率掷失真骰（`rng` 由调用方从 events.rng_seed 派生，可回放）。"""
    flip_prob = FLIP_PROB_N_HIGH if (neuroticism is not None and neuroticism >= N_HIGH_THRESHOLD) else FLIP_PROB
    return Distortion(
        polarize=rng.random() < POLARIZE_PROB,
        flip=rng.random() < flip_prob,
        misattribute=rng.random() < MISATTRIBUTE_PROB,
    )


def distortion_rng(rng_seed: int, event_seq: int, hop: int) -> random.Random:
    """失真骰子流（确定性）：事件 rng_seed + 上手事件 seq + 手数派生。"""
    return random.Random(f"distort:{rng_seed}:{event_seq}:{hop}")
