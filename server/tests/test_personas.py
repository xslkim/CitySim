"""agents.yaml 人设校验器：T-CFG-04 8 人口径（test_p8_，作用域 A01~A08）+ T-CFG-06 全量口径（test_p40_）。

枚举域镜像 02 §4.1（hair 16 款/outfit 6 类/accessory 6 类/build 3 档）与 02 §4.3（16 色池，编号+HEX 双写互验）。
拓扑指标口径（01 §2.1）：边 = relations_initial 行去重后的无序对（多行同对算一条，多原型标签可叠加）；
负向原型 = {前任, 职场对手, 结怨, 黑名单, 立场对立, 欠款, 债主, 竞争者}；
跨场景边 = 两端既不同楼层（同为租客）也不同部门；入度 = 作为 target 被指向的次数。
"""

from __future__ import annotations

import itertools
import subprocess
from pathlib import Path

import pytest
import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
AGENTS_PATH = SERVER_ROOT / "config" / "agents.yaml"
WORLD_PATH = SERVER_ROOT / "config" / "world.yaml"
P8_IDS = [f"A{i:02d}" for i in range(1, 9)]

# —— 02 §4.1 取值域镜像 ——
HAIR_MALE = {f"M{i:02d}-{n}" for i, n in enumerate(
    ["短寸", "侧分短发", "背头", "碎盖刘海", "纹理烫", "中分微卷", "发髻", "蘑菇头"], start=1)}
HAIR_FEMALE = {f"F{i:02d}-{n}" for i, n in enumerate(
    ["黑长直", "波波头", "锁骨发", "高马尾", "丸子头", "空气刘海中长发", "羊毛卷", "精灵短发"], start=1)}
OUTFITS = {"西装", "商务休闲", "卫衣", "连衣裙", "工装", "运动"}
ACCESSORIES = {"眼镜", "发带", "耳环", "围巾", "耳机", "工牌"}
BUILDS = {"瘦", "标准", "壮"}

# —— 02 §4.3 十六色池镜像（编号 → (色名, HEX)）——
COLOR_POOL = {
    1: ("朱红", "#BE2A42"), 2: ("钴蓝", "#5583C9"), 3: ("翠绿", "#209C68"), 4: ("琥珀", "#BF700F"),
    5: ("青碧", "#209894"), 6: ("暗红", "#61191E"), 7: ("堇紫", "#4A2999"), 8: ("天青", "#1A5E80"),
    9: ("杏黄", "#FADB40"), 10: ("鼠尾草绿", "#A4C394"), 11: ("紫藤", "#A432A1"), 12: ("蔷薇", "#EB6999"),
    13: ("咖啡", "#7D4F30"), 14: ("雾蓝", "#C3CDDA"), 15: ("芥末", "#A8B82E"), 16: ("岩灰", "#6A6C6F"),
}

# 01 §2.3-6 重题材秘密（债务/背叛/身份隐瞒/职场黑历史）编辑判定集：校验"≤6 人"为程序化门槛，
# 分类本身属内容判定（01 §2.3-6 类目），逐人设 secret 行内注释同步标注。判定范围 = 明星层 8 人。
HEAVY_SECRET_AGENTS = {"A01", "A03", "A04", "A06"}

BIG_FIVE_KEYS = ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"]

# 01 §2.1 原型库（边原型标签封闭集；01 文档 §6 D3 注释：负债方向见 round2 §A.18）
NEGATIVE_TYPES = {"前任", "职场对手", "结怨", "黑名单", "立场对立", "欠款", "债主", "竞争者"}
DIRECTED_TYPES = {"暗恋", "师徒", "债主", "欠款", "结怨"}  # 单向原型（01 §2.1 方向列）


def _agents() -> list[dict]:
    with AGENTS_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["agents"]


def _p8() -> list[dict]:
    return [a for a in _agents() if a["agent_id"] in P8_IDS]


def _world() -> dict:
    with WORLD_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _gender_suffixes() -> tuple[set[int], set[int]]:
    rule = _world()["locations"]["apartment"]["rooms"]["gender_rule"]
    return set(rule["male_suffixes"]), set(rule["female_suffixes"])


def _base_persona_checks(a: dict) -> None:
    """01 §2.2 硬门槛逐人子集（p8/p40 共用）。"""
    vals = [a["big_five"][k] for k in BIG_FIVE_KEYS]
    assert all(isinstance(v, int) and 0 <= v <= 100 for v in vals), a["name"]
    assert not all(v > 60 for v in vals) and not all(v < 40 for v in vals), f"{a['name']} Big Five 扁平"
    assert 3 <= len(a["backstory"]) <= 5, a["name"]
    for field in ("signature_quirk", "contrast", "secret", "trigger_point"):
        assert a.get(field), f"{a['name']} 缺鲜明度字段 {field}"
    style = a["speech_style"]
    assert style.get("tone") and style.get("sentence_len") in {"短", "中", "长"}, a["name"]
    assert "catchphrase" in style and isinstance(style.get("taboo"), list), a["name"]
    app = a["appearance"]
    hair_pool = HAIR_MALE if a["gender"] == "M" else HAIR_FEMALE
    assert app["hair"] in hair_pool, f"{a['name']} hair 越界或性别不匹配: {app['hair']}"
    assert app["outfit"] in OUTFITS and app["build"] in BUILDS, a["name"]
    assert isinstance(app.get("palette_hint", []), list) and len(app["palette_hint"]) <= 3, a["name"]
    color = app["signature_color"]
    assert color["id"] in COLOR_POOL and COLOR_POOL[color["id"]] == (color["name"], color["hex"]), (
        f"{a['name']} 签名色编号/色名/HEX 不自洽: {color}"
    )
    assert a.get("goals_initial") and len(a["goals_initial"]) <= 3, a["name"]
    for rel in a.get("relations_initial", []):
        assert rel["target"] != a["agent_id"], f"{a['name']} 自指关系"


def _edges(agents: list[dict]) -> dict[frozenset, set[str]]:
    """无序边 → 原型标签集（多行同对合并）。"""
    edges: dict[frozenset, set[str]] = {}
    for a in agents:
        for r in a.get("relations_initial", []):
            key = frozenset((a["agent_id"], r["target"]))
            edges.setdefault(key, set()).add(r["type"])
    return edges


def _check_base_all(agents: list[dict]) -> None:
    lib = {a["agent_id"] for a in agents}
    for a in agents:
        _base_persona_checks(a)
        for rel in a.get("relations_initial", []):
            assert rel["target"] in lib, f"{a['name']} 关系目标越库: {rel['target']}"


# ======================= T-CFG-04：8 人口径（作用域 A01~A08） =======================

def test_p8_count_and_ids() -> None:
    agents = _p8()
    assert len(agents) == 8
    assert [a["agent_id"] for a in agents] == P8_IDS


def test_p8_gender_balance() -> None:
    genders = [a["gender"] for a in _p8()]
    assert abs(genders.count("M") - 4) <= 1 and abs(genders.count("F") - 4) <= 1
    assert set(genders) <= {"M", "F"}


def test_p8_rooms_unique_and_gender_layout() -> None:
    agents = _p8()
    rooms = [a["room"] for a in agents]
    assert len(rooms) == len(set(rooms)), "房间号须唯一"
    assert all(r is not None for r in rooms), "8 人全租客落位（01 文档 §6 D3）"
    male_sfx, female_sfx = _gender_suffixes()
    for a in agents:
        floor, suffix = int(a["room"][:-2]), int(a["room"][-2:])
        assert 1 <= floor <= 6 and suffix in male_sfx | female_sfx, f"{a['name']} 房间号越界: {a['room']}"
        if a["gender"] == "M":
            assert suffix in male_sfx, f"{a['name']}({a['room']}) 违反性别布局"
        else:
            assert suffix in female_sfx, f"{a['name']}({a['room']}) 违反性别布局"


def test_p8_names_unique() -> None:
    names = [a["name"] for a in _p8()]
    assert len(names) == len(set(names))


def test_p8_hard_gates() -> None:
    """01 §2.2 硬门槛子集（Big Five 防扁平/backstory 3~5/鲜明度/枚举域/签名色互验/目标在库内）。"""
    agents = _p8()
    lib = {a["agent_id"] for a in agents}
    for a in agents:
        _base_persona_checks(a)
        for rel in a.get("relations_initial", []):
            assert rel["target"] in lib, f"{a['name']} 关系目标越库: {rel['target']}"


def test_p8_signature_color_unique() -> None:
    ids = [a["appearance"]["signature_color"]["id"] for a in _p8()]
    assert len(ids) == len(set(ids)), "8 人内签名色须唯一（02 §4.2-1 尽力唯一）"


def test_p8_star_accessory_required() -> None:
    for a in _p8():
        acc = a["appearance"]["accessory"]
        assert acc in ACCESSORIES, f"{a['name']} 明星层强制配饰（02 §4.2-4）: {acc}"


def test_p8_heavy_secret_cap() -> None:
    agents = _p8()
    lib = {a["agent_id"] for a in agents}
    assert HEAVY_SECRET_AGENTS <= lib, "重题材判定集须在库内"
    assert len(HEAVY_SECRET_AGENTS) <= 6, "重题材 secret ≤6 人（01 §2.3-6）"
    assert len(agents) - len(HEAVY_SECRET_AGENTS) >= 2, "轻量题材 ≥2 人（01 §2.3-6）"


def test_p8_topology_fragment_edges() -> None:
    """01 §2.1 示例片段关系保留（01 文档 T-CFG-04 验收 3）。"""
    rels = {a["agent_id"]: {r["target"]: r for r in a.get("relations_initial", [])} for a in _p8()}

    def has(src: str, dst: str, rtype: str) -> bool:
        return dst in rels.get(src, {}) and rels[src][dst]["type"] == rtype

    assert has("A01", "A02", "暗恋"), "林晚→周叙 暗恋"
    assert has("A02", "A07", "师徒"), "周叙→赵启 师徒"
    assert has("A03", "A04", "前任") and has("A04", "A03", "前任"), "苏蔓—陈屿 前任"
    assert has("A06", "A07", "职场对手") and has("A07", "A06", "职场对手"), "韩彻↔赵启 职场对手"
    debt = rels["A01"].get("A06")
    assert debt and debt["type"] == "债主" and "4000" in debt["note"], "林晚→韩彻 债主（¥4,000）"


def test_p8_portrait_files() -> None:
    agents = _p8()
    present = [a for a in agents if a["appearance"]["portrait_file"]]
    absent = [a for a in agents if not a["appearance"]["portrait_file"]]
    assert len(present) == 6 and len(absent) == 2, "6 张已入库 + 2 条 null（01 文档 §6 D6）"
    for a in present:
        p = REPO_ROOT / a["appearance"]["portrait_file"]
        assert p.is_file(), f"{a['name']} 头像不存在: {p}"


# ======================= T-CFG-06：40 人全量口径 =======================

def test_p40_count_and_ids() -> None:
    agents = _agents()
    assert len(agents) == 40
    assert [a["agent_id"] for a in agents] == [f"A{i:02d}" for i in range(1, 41)]


def test_p40_a01_a08_verbatim() -> None:
    """A01~A08 与 M0 冻结版逐条相等（01 文档 T-CFG-06 验收 2；T-DB-04 增项口径）。

    基线 = `schema-v1` tag（M0 冻结点，T-DB-06/09 E9）：T-DB-04 按明星层 3 条/人（01 §3.3）
    补齐 A01~A08 goals_initial，原"首次入库提交"基线随之失效；冻结后 40 人文件前 8 人
    任何漂移即红（01 文档 §6 D15）。tag 未打前 skip（同 test_embed_config_matches_ddl_vector_dim 惯例）。
    """
    tag = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "refs/tags/schema-v1"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if tag.returncode != 0:
        pytest.skip("schema-v1 tag 未打（M0 出口 E9），A01~A08 冻结对拍随 tag 自动激活")
    old = subprocess.run(
        ["git", "show", "schema-v1:server/config/agents.yaml"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    old8 = {a["agent_id"]: a for a in yaml.safe_load(old)["agents"] if a["agent_id"] in P8_IDS}
    cur8 = {a["agent_id"]: a for a in _agents() if a["agent_id"] in P8_IDS}
    assert set(old8) == set(P8_IDS)
    for aid in P8_IDS:
        assert cur8[aid] == old8[aid], f"{aid} 与 M0 冻结版不一致"


def test_p40_gender_balanced() -> None:
    genders = [a["gender"] for a in _agents()]
    assert genders.count("M") == 20 and genders.count("F") == 20, "男女各半（01 §2.2 硬约束）"


def test_p40_rooms_24_tenants_16_null() -> None:
    agents = _agents()
    tenants = [a for a in agents if a["room"] is not None]
    npcs = [a for a in agents if a["room"] is None]
    assert len(tenants) == 24 and len(npcs) == 16, "24 租客 + 16 公司 NPC（01 §1.3/§1.6）"
    rooms = [a["room"] for a in tenants]
    assert len(set(rooms)) == 24, "房间号唯一"
    male_sfx, female_sfx = _gender_suffixes()
    for a in tenants:
        floor, suffix = int(a["room"][:-2]), int(a["room"][-2:])
        assert 1 <= floor <= 6 and suffix in male_sfx | female_sfx, a["name"]
        if a["gender"] == "M":
            assert suffix in male_sfx, f"{a['name']}({a['room']}) 违反性别布局"
        else:
            assert suffix in female_sfx, f"{a['name']}({a['room']}) 违反性别布局"


def test_p40_managers_6_npc() -> None:
    agents = _agents()
    managers = [a for a in agents if a["position"] == "M"]
    assert len(managers) == 6, "经理恰 6（01 §1.3）"
    assert all(m["room"] is None for m in managers), "经理全部为 NPC（room: null）"
    assert all(30 <= m["age"] <= 38 for m in managers), "经理年龄 30~38（01 §2.2）"


def test_p40_dept_and_rank_headcount() -> None:
    agents = _agents()
    world_depts = {d["key"]: d for d in _world()["company"]["departments"]}

    def dept_key(department: str) -> str | None:
        for k, d in world_depts.items():
            if d["name"] == department or d["name"].startswith(department):
                return k
        return None

    for a in agents:
        assert dept_key(a["department"]) is not None, f"{a['name']} 部门越枚举: {a['department']}"
    for d in world_depts.values():
        members = [a for a in agents if dept_key(a["department"]) == d["key"]]
        mgr = [a for a in members if a["position"] == "M"]
        npc_staff = [a for a in members if a["room"] is None and a["position"] != "M"]
        tenants = [a for a in members if a["room"] is not None]
        assert len(members) == d["total"], f"{d['name']} 总人数 {len(members)} != {d['total']}"
        assert len(mgr) == d["managers"] and len(npc_staff) == d["npc_staff"] and len(tenants) == d["tenants"], d["name"]
    ranks = {"M": 0, "P2": 0, "P1": 0}
    for a in agents:
        ranks[a["position"]] += 1
    assert ranks == _world()["company"]["ranks"], f"职级编制不符: {ranks}"
    # P2 入职 ≥2 年；P1 <2 年（01 §1.3）
    for a in agents:
        if a["position"] == "P2":
            assert a["hire_months"] >= 24, f"{a['name']} P2 入职不足 2 年"
        elif a["position"] == "P1":
            assert a["hire_months"] < 24, f"{a['name']} P1 入职超 2 年"


def test_p40_base_gates_all() -> None:
    """01 §2.2 硬门槛子集全量化（Big Five/backstory/鲜明度/枚举域/签名色互验/关系目标在库内）。"""
    _check_base_all(_agents())


def test_p40_names_unique() -> None:
    names = [a["name"] for a in _agents()]
    assert len(names) == len(set(names))


def test_p40_density_metrics() -> None:
    """01 §2.1 拓扑密度六指标逐项断言。"""
    agents = _agents()
    by_id = {a["agent_id"]: a for a in agents}
    edges = _edges(agents)
    e = len(edges)
    assert 72 - 8 <= e <= 72 + 8, f"强关系边总数 {e} 不在 72±8"

    in_degree = {a["agent_id"]: 0 for a in agents}
    for a in agents:
        for r in a.get("relations_initial", []):
            in_degree[r["target"]] += 1
    assert all(d >= 1 for d in in_degree.values()), f"入度为 0: {[k for k, d in in_degree.items() if d == 0]}"

    degree = {a["agent_id"]: 0 for a in agents}
    for pair in edges:
        for nid in pair:
            degree[nid] += 1
    hubs = [nid for nid, d in degree.items() if d >= 4]
    assert 4 <= len(hubs) <= 6, f"度≥4 节点 {len(hubs)} 个（{sorted(hubs)}），须 4~6"

    def is_neg(pair: frozenset) -> bool:
        return bool(edges[pair] & NEGATIVE_TYPES)

    nodes = list(by_id)
    neg_triangles = 0
    for tri in itertools.combinations(nodes, 3):
        pairs = [frozenset((tri[0], tri[1])), frozenset((tri[0], tri[2])), frozenset((tri[1], tri[2]))]
        if all(p in edges for p in pairs) and any(is_neg(p) for p in pairs):
            neg_triangles += 1
    assert neg_triangles >= 5, f"负向三角 {neg_triangles} < 5"

    def scene_internal(pair: frozenset) -> bool:
        x, y = (by_id[n] for n in pair)
        same_floor = x["room"] is not None and y["room"] is not None and x["room"][:-2] == y["room"][:-2]
        same_dept = x["department"] == y["department"]
        return same_floor or same_dept

    cross = sum(1 for p in edges if not scene_internal(p))
    assert cross / e >= 0.40, f"跨场景边占比 {cross}/{e}={cross / e:.0%} < 40%"

    same_gender = sum(1 for p in edges if len({by_id[n]["gender"] for n in p}) == 1)
    ratio = same_gender / e
    assert 0.45 <= ratio <= 0.55, f"同性边占比 {ratio:.0%} 不在 45~55%"


def test_p40_prototype_quotas() -> None:
    """01 §2.1 原型库数量配额（暗恋 8~12 / 前任 ≥3 / 对手 ≥4 / 师徒 4~6 / 老乡 ≥2 组 / 闺蜜兄弟 ≥5 /
    债主欠款 2~4 / 竞争者 ≥2 / 黑名单 3~5 / 立场对立 ≥3 / 双向暗恋 ≤2 / 暗恋链 ≥3 节点至少 2 条）。"""
    agents = _agents()
    by_id = {a["agent_id"]: a for a in agents}
    edges = _edges(agents)

    def count(t: str) -> int:
        return sum(1 for types in edges.values() if t in types)

    assert 8 <= count("暗恋") <= 12, f"暗恋 {count('暗恋')}"
    assert count("前任") >= 3
    assert count("职场对手") >= 4
    assert 4 <= count("师徒") <= 6, f"师徒 {count('师徒')}"
    assert count("闺蜜") + count("兄弟") >= 5
    assert 2 <= count("债主") <= 4, f"债主 {count('债主')}"
    assert count("竞争者") >= 2
    assert 3 <= count("结怨") + count("黑名单") <= 5
    assert count("立场对立") >= 3

    # 老乡组（≥3 人连通组）≥2
    laoxiang_adj: dict[str, set[str]] = {}
    for pair, types in edges.items():
        if "老乡" in types:
            x, y = pair
            laoxiang_adj.setdefault(x, set()).add(y)
            laoxiang_adj.setdefault(y, set()).add(x)
    seen: set[str] = set()
    groups = 0
    for nid in laoxiang_adj:
        if nid in seen:
            continue
        stack, comp = [nid], set()
        while stack:
            cur = stack.pop()
            if cur in comp:
                continue
            comp.add(cur)
            stack.extend(laoxiang_adj.get(cur, ()))
        seen |= comp
        if len(comp) >= 3:
            groups += 1
    assert groups >= 2, f"老乡组 {groups} < 2"

    # 双向暗恋 ≤2 对
    crush_dir: set[tuple[str, str]] = set()
    for a in agents:
        for r in a.get("relations_initial", []):
            if r["type"] == "暗恋":
                crush_dir.add((a["agent_id"], r["target"]))
    mutual = [(s, t) for s, t in crush_dir if (t, s) in crush_dir and s < t]
    assert len(mutual) <= 2, f"双向暗恋 {mutual} 超过 2 对"

    # 长度 ≥3（节点）的暗恋链至少 2 条
    adj: dict[str, list[str]] = {}
    for s, t in crush_dir:
        adj.setdefault(s, []).append(t)
    chains: set[tuple[str, ...]] = set()

    def walk(path: tuple[str, ...]) -> None:
        if len(path) >= 3:
            chains.add(path)
        for nxt in adj.get(path[-1], []):
            if nxt not in path and len(path) < 6:
                walk(path + (nxt,))

    for nid in adj:
        walk((nid,))
    assert len(chains) >= 2, f"≥3 节点暗恋链 {len(chains)} < 2"

    # 立场对立强制同楼层或同部门（01 §2.1）
    for pair, types in edges.items():
        if "立场对立" in types:
            x, y = (by_id[n] for n in pair)
            same_floor = x["room"] is not None and y["room"] is not None and x["room"][:-2] == y["room"][:-2]
            assert same_floor or x["department"] == y["department"], f"立场对立未落位: {sorted(pair)}"


def test_p40_art_rules() -> None:
    """02 §4.2 四条分配规则：签名色尽力唯一+撞色拉开、同部门服装相邻不相同、同楼层发型不重复、明星层配饰。"""
    agents = _agents()
    by_color: dict[int, list[dict]] = {}
    for a in agents:
        by_color.setdefault(a["appearance"]["signature_color"]["id"], []).append(a)
    for cid, group in by_color.items():
        for x, y in itertools.combinations(group, 2):
            same_floor = x["room"] is not None and y["room"] is not None and x["room"][:-2] == y["room"][:-2]
            assert not (same_floor or x["department"] == y["department"]), (
                f"高频同框撞色 {cid}: {x['name']}/{y['name']}（02 §4.2-1 优先不撞色）"
            )
            assert x["appearance"]["hair"] != y["appearance"]["hair"] and x["appearance"]["outfit"] != y["appearance"]["outfit"], (
                f"撞色未拉开（发型+服装两维）: {x['name']}/{y['name']} 色 {cid}"
            )
    # 同部门（租客间）服装类型相邻但不相同
    by_dept: dict[str, list[dict]] = {}
    for a in agents:
        if a["room"] is not None:
            by_dept.setdefault(a["department"], []).append(a)
    for dept, group in by_dept.items():
        outfits = [a["appearance"]["outfit"] for a in group]
        assert len(outfits) == len(set(outfits)), f"{dept} 租客服装类型重复: {outfits}"
    # 同楼层发型不重复
    by_floor: dict[str, list[dict]] = {}
    for a in agents:
        if a["room"] is not None:
            by_floor.setdefault(a["room"][:-2], []).append(a)
    for floor, group in by_floor.items():
        hairs = [a["appearance"]["hair"] for a in group]
        assert len(hairs) == len(set(hairs)), f"{floor} 层发型重复: {hairs}"
    # 明星层（A01~A08）强制配饰
    for a in agents:
        if a["agent_id"] in P8_IDS:
            assert a["appearance"]["accessory"] in ACCESSORIES, a["name"]


def test_p40_initial_debts() -> None:
    """初始债务 ≤¥5,000 且 2~4 条（01 §2.1；豁免 §4 borrow 单笔上限，评审 P2-10）。"""
    agents = _agents()
    edges = _edges(agents)
    debt_edges = [p for p, types in edges.items() if types & {"债主", "欠款"}]
    assert 2 <= len(debt_edges) <= 4, f"初始债务 {len(debt_edges)} 条"
    for a in agents:
        for r in a.get("relations_initial", []):
            if r["type"] == "债主":
                import re
                amounts = [int(m) for m in re.findall(r"欠\s*(\d+)", r["note"])]
                assert amounts and amounts[0] <= 5000, f"{a['name']} 债主行金额超 ¥5,000: {r['note']}"
