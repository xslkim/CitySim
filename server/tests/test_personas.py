"""T-CFG-04 agents.yaml 人设校验器（01 §2.2 硬门槛子集，8 人口径，用例前缀 test_p8_）。

T-CFG-06 复用本校验器并增 test_p40_ 前缀用例（全量拓扑/美术分配规则）。
枚举域镜像 02 §4.1（hair 16 款/outfit 6 类/accessory 6 类/build 3 档）与 02 §4.3（16 色池，编号+HEX 双写互验）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
AGENTS_PATH = SERVER_ROOT / "config" / "agents.yaml"
WORLD_PATH = SERVER_ROOT / "config" / "world.yaml"

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
# 分类本身属内容判定（01 §2.3-6 类目），逐人设 secret 行内注释同步标注。
HEAVY_SECRET_AGENTS = {"A01", "A03", "A04", "A06"}

BIG_FIVE_KEYS = ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"]


def _agents() -> list[dict]:
    with AGENTS_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["agents"]


def _world_gender_rule() -> tuple[set[int], set[int]]:
    with WORLD_PATH.open(encoding="utf-8") as f:
        rule = yaml.safe_load(f)["locations"]["apartment"]["rooms"]["gender_rule"]
    return set(rule["male_suffixes"]), set(rule["female_suffixes"])


def test_p8_count_and_ids() -> None:
    agents = _agents()
    assert len(agents) == 8
    assert [a["agent_id"] for a in agents] == [f"A{i:02d}" for i in range(1, 9)]


def test_p8_gender_balance() -> None:
    genders = [a["gender"] for a in _agents()]
    assert abs(genders.count("M") - 4) <= 1 and abs(genders.count("F") - 4) <= 1
    assert set(genders) <= {"M", "F"}


def test_p8_rooms_unique_and_gender_layout() -> None:
    agents = _agents()
    rooms = [a["room"] for a in agents]
    assert len(rooms) == len(set(rooms)), "房间号须唯一"
    assert all(r is not None for r in rooms), "8 人全租客落位（01 文档 §6 D3）"
    male_sfx, female_sfx = _world_gender_rule()
    for a in agents:
        floor, suffix = int(a["room"][:-2]), int(a["room"][-2:])
        assert 1 <= floor <= 6 and suffix in male_sfx | female_sfx, f"{a['name']} 房间号越界: {a['room']}"
        if a["gender"] == "M":
            assert suffix in male_sfx, f"{a['name']}({a['room']}) 违反性别布局"
        else:
            assert suffix in female_sfx, f"{a['name']}({a['room']}) 违反性别布局"


def test_p8_names_unique() -> None:
    names = [a["name"] for a in _agents()]
    assert len(names) == len(set(names))


def test_p8_big_five_not_flat() -> None:
    for a in _agents():
        vals = [a["big_five"][k] for k in BIG_FIVE_KEYS]
        assert all(isinstance(v, int) and 0 <= v <= 100 for v in vals), a["name"]
        assert not all(v > 60 for v in vals), f"{a['name']} 五项全 >60（扁平人设）"
        assert not all(v < 40 for v in vals), f"{a['name']} 五项全 <40（扁平人设）"


def test_p8_backstory_count() -> None:
    for a in _agents():
        assert 3 <= len(a["backstory"]) <= 5, a["name"]


def test_p8_sharpness_fields_nonempty() -> None:
    for a in _agents():
        for field in ("signature_quirk", "contrast", "secret", "trigger_point"):
            assert a.get(field), f"{a['name']} 缺鲜明度字段 {field}"
        style = a["speech_style"]
        assert style.get("tone") and style.get("sentence_len"), a["name"]
        assert style["sentence_len"] in {"短", "中", "长"}, a["name"]
        assert "catchphrase" in style and "taboo" in style, a["name"]  # catchphrase 允许为空（01 §2.2）
        assert isinstance(style["taboo"], list), a["name"]


def test_p8_appearance_enum_domains() -> None:
    for a in _agents():
        app = a["appearance"]
        hair_pool = HAIR_MALE if a["gender"] == "M" else HAIR_FEMALE
        assert app["hair"] in hair_pool, f"{a['name']} hair 越界或性别不匹配: {app['hair']}"
        assert app["outfit"] in OUTFITS, a["name"]
        assert app["build"] in BUILDS, a["name"]
        hint = app.get("palette_hint", [])
        assert isinstance(hint, list) and len(hint) <= 3, a["name"]


def test_p8_signature_color_pool() -> None:
    seen: set[int] = set()
    for a in _agents():
        color = a["appearance"]["signature_color"]
        cid, name, hexv = color["id"], color["name"], color["hex"]
        assert cid in COLOR_POOL, f"{a['name']} 签名色编号越界: {cid}"
        assert COLOR_POOL[cid] == (name, hexv), f"{a['name']} 签名色编号/色名/HEX 不自洽: {color}"
        seen.add(cid)
    assert len(seen) == len(_agents()), "8 人内签名色须唯一（02 §4.2-1 尽力唯一）"


def test_p8_star_accessory_required() -> None:
    for a in _agents():
        acc = a["appearance"]["accessory"]
        assert acc in ACCESSORIES, f"{a['name']} 明星层强制配饰（02 §4.2-4）: {acc}"


def test_p8_relations_targets_in_library() -> None:
    agents = _agents()
    lib = {a["agent_id"] for a in agents}
    for a in agents:
        for rel in a.get("relations_initial", []):
            assert rel["target"] in lib, f"{a['name']} 关系目标越库: {rel['target']}"
            assert rel["target"] != a["agent_id"], f"{a['name']} 自指关系"


def test_p8_heavy_secret_cap() -> None:
    agents = _agents()
    lib = {a["agent_id"] for a in agents}
    assert HEAVY_SECRET_AGENTS <= lib, "重题材判定集须在库内"
    assert len(HEAVY_SECRET_AGENTS) <= 6, "重题材 secret ≤6 人（01 §2.3-6）"
    assert len(agents) - len(HEAVY_SECRET_AGENTS) >= 2, "轻量题材 ≥2 人（01 §2.3-6）"


def test_p8_topology_fragment_edges() -> None:
    """01 §2.1 示例片段关系保留（01 文档 T-CFG-04 验收 3）。"""
    rels = {a["agent_id"]: {r["target"]: r for r in a.get("relations_initial", [])} for a in _agents()}

    def has(src: str, dst: str, rtype: str) -> bool:
        return dst in rels.get(src, {}) and rels[src][dst]["type"] == rtype

    assert has("A01", "A02", "暗恋"), "林晚→周叙 暗恋"
    assert has("A02", "A07", "师徒"), "周叙→赵启 师徒"
    assert has("A03", "A04", "前任") and has("A04", "A03", "前任"), "苏蔓—陈屿 前任"
    assert has("A06", "A07", "职场对手") and has("A07", "A06", "职场对手"), "韩彻↔赵启 职场对手"
    debt = rels["A01"].get("A06")
    assert debt and debt["type"] == "债主" and "4000" in debt["note"], "林晚→韩彻 债主（¥4,000）"


def test_p8_portrait_files() -> None:
    agents = _agents()
    present = [a for a in agents if a["appearance"]["portrait_file"]]
    absent = [a for a in agents if not a["appearance"]["portrait_file"]]
    assert len(present) == 6 and len(absent) == 2, "6 张已入库 + 2 条 null（01 文档 §6 D6）"
    for a in present:
        p = REPO_ROOT / a["appearance"]["portrait_file"]
        assert p.is_file(), f"{a['name']} 头像不存在: {p}"
