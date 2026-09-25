"""T-ART-01 签名色池 ΔE 全对校验（02 §4.3 约束 2；纯 stdlib CIEDE2000）。

- 读 `ui/assets/palette/worldsim.gpl`（唯一机读源），与 02 §4.3/§7.1 的镜像表逐字对拍
  （色名、HEX、"雾蓝"唯一指向 #14）。
- 校验：池内 C(16,2)=120 对全对 ΔE(CIEDE2000) ≥15；16 色 × 6 语义色 96 对全对 ≥15。
- 同步断言：`allowed_hex.txt` 覆盖 gpl 全部 HEX；`stream/js/lib/palette-data.js` 与 gpl 同步。
- `--export`：从 gpl 重写 `stream/js/lib/palette-data.js`（06 T-ART-02 消费）。

用法：`cd server && uv run python scripts/check_delta_e.py [--export]`
"""

from __future__ import annotations

import math
import sys
from itertools import combinations
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
GPL = REPO_ROOT / "ui" / "assets" / "palette" / "worldsim.gpl"
ALLOWED = REPO_ROOT / "ui" / "assets" / "palette" / "allowed_hex.txt"
PALETTE_JS = REPO_ROOT / "stream" / "js" / "lib" / "palette-data.js"

# 02 §4.3 签名色 16 色池（镜像表，行序 = 编号）
SIG_POOL: list[tuple[str, str]] = [
    ("#BE2A42", "朱红"), ("#5583C9", "钴蓝"), ("#209C68", "翠绿"), ("#BF700F", "琥珀"),
    ("#209894", "青碧"), ("#61191E", "暗红"), ("#4A2999", "堇紫"), ("#1A5E80", "天青"),
    ("#FADB40", "杏黄"), ("#A4C394", "鼠尾草绿"), ("#A432A1", "紫藤"), ("#EB6999", "蔷薇"),
    ("#7D4F30", "咖啡"), ("#C3CDDA", "雾蓝"), ("#A8B82E", "芥末"), ("#6A6C6F", "岩灰"),
]
# 02 §7.1 六语义色 + 底色/文字/描边 token（镜像表）
SEMANTIC: list[tuple[str, str]] = [
    ("#2EFA8D", "positive"), ("#F75752", "negative"), ("#FEAE3E", "warn"),
    ("#8A93A0", "neutral"), ("#BF86E8", "director"), ("#5AC3ED", "accent"),
]
TOKENS: list[tuple[str, str]] = [
    ("#0E1116", "bg-0"), ("#161B22", "bg-1"), ("#1F2630", "bg-2"),
    ("#2D333D", "border"), ("#E6E9EF", "text-0"), ("#9AA4B2", "text-1"),
]

DE_MIN = 15.0  # 02 §4.3 约束 2 阈值


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def rgb_to_lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """sRGB → CIE L*a*b*（D65/2°）。"""

    def pivot_rgb(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (pivot_rgb(c) for c in rgb)
    x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / 0.95047
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = (r * 0.0193339 + g * 0.1191920 + b * 0.9503041) / 1.08883

    def pivot_xyz(c: float) -> float:
        e = 216 / 24389
        k = 24389 / 27
        return c ** (1 / 3) if c > e else (k * c + 16) / 116

    fx, fy, fz = pivot_xyz(x), pivot_xyz(y), pivot_xyz(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """CIEDE2000（Sharma et al. 2005，kL=kC=kH=1）。"""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    avg_C = (C1 + C2) / 2
    g = 0.5 * (1 - math.sqrt(avg_C**7 / (avg_C**7 + 25**7)))
    a1p, a2p = a1 * (1 + g), a2 * (1 + g)
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)

    def hp(ap: float, b: float) -> float:
        if ap == 0 and b == 0:
            return 0.0
        h = math.degrees(math.atan2(b, ap))
        return h + 360 if h < 0 else h

    h1p, h2p = hp(a1p, b1), hp(a2p, b2)
    dLp, dCp = L2 - L1, C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp / 2))
    avg_Lp, avg_Cp = (L1 + L2) / 2, (C1p + C2p) / 2
    if C1p * C2p == 0:
        avg_hp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        avg_hp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        avg_hp = (h1p + h2p + 360) / 2
    else:
        avg_hp = (h1p + h2p - 360) / 2
    t = (1 - 0.17 * math.cos(math.radians(avg_hp - 30))
         + 0.24 * math.cos(math.radians(2 * avg_hp))
         + 0.32 * math.cos(math.radians(3 * avg_hp + 6))
         - 0.20 * math.cos(math.radians(4 * avg_hp - 63)))
    dtheta = 30 * math.exp(-(((avg_hp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(avg_Cp**7 / (avg_Cp**7 + 25**7))
    sl = 1 + 0.015 * (avg_Lp - 50) ** 2 / math.sqrt(20 + (avg_Lp - 50) ** 2)
    sc, sh = 1 + 0.045 * avg_Cp, 1 + 0.015 * avg_Cp * t
    rt = -math.sin(math.radians(2 * dtheta)) * rc
    return math.sqrt((dLp / sl) ** 2 + (dCp / sc) ** 2 + (dHp / sh) ** 2
                     + rt * (dCp / sc) * (dHp / sh))


def parse_gpl(path: Path) -> list[tuple[str, str]]:
    """gpl → [(hex, name)]，跳过注释/头。"""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(("GIMP", "Name:", "Columns:")):
            continue
        parts = s.split("\t") if "\t" in s else s.split(None, 3)
        rgb = parts[0].split()
        name = parts[1].strip() if len(parts) > 1 else parts[-1]
        if len(rgb) == 3 and len(parts) > 1:
            name = parts[1].strip()
        entries.append((rgb_to_hex(int(rgb[0]), int(rgb[1]), int(rgb[2])), name))
    return entries


def render_palette_js(sig: list[tuple[str, str]], sem: list[tuple[str, str]],
                      tokens: list[tuple[str, str]]) -> str:
    """从 gpl 派生 `stream/js/lib/palette-data.js`（06 T-ART-02 签名色解析数据源）。"""
    lines = [
        "// 生成物勿手改（由 server/scripts/check_delta_e.py --export 自 ui/assets/palette/worldsim.gpl 派生，06 T-ART-01/02）",
        "/** 签名色池（02 §4.3）：编号字符串 → {name, hex} */",
        "export const SIGNATURE_COLORS = {",
    ]
    for i, (hex_, name) in enumerate(sig, 1):
        lines.append(f"  '{i}': {{ name: '{name}', hex: '{hex_}' }},")
    lines += ["};", "", "/** 语义色（02 §7.1，只做边/条/图标） */", "export const SEMANTIC_COLORS = {"]
    for hex_, name in sem:
        lines.append(f"  {name}: '{hex_}',")
    lines += ["};", "", "/** 底色/文字/描边 token（02 §7.1） */", "export const TOKENS = {"]
    for hex_, name in tokens:
        lines.append(f"  '{name}': '{hex_}',")
    lines += ["};", ""]
    # 修正语义色行内的 key 引号（标识符合法，无需引号）
    return "\n".join(lines)


def main() -> int:
    entries = parse_gpl(GPL)
    errors: list[str] = []
    n_expected = len(SIG_POOL) + len(SEMANTIC) + len(TOKENS)
    if len(entries) != n_expected:
        errors.append(f"gpl 条目数 {len(entries)} != 期望 {n_expected}")
    sig, sem, tok = entries[:16], entries[16:22], entries[22:]

    # 逐字对拍 02 §4.3/§7.1 镜像表
    for (hex_e, name_e), (hex_a, name_a) in zip(SIG_POOL, sig):
        if hex_e != hex_a or name_e != name_a:
            errors.append(f"签名色对拍失败：期望 {hex_e} {name_e}，实际 {hex_a} {name_a}")
    for (hex_e, name_e), (hex_a, name_a) in zip(SEMANTIC, sem):
        if hex_e != hex_a or name_e != name_a:
            errors.append(f"语义色对拍失败：期望 {hex_e} {name_e}，实际 {hex_a} {name_a}")
    for (hex_e, name_e), (hex_a, name_a) in zip(TOKENS, tok):
        if hex_e != hex_a or name_e != name_a:
            errors.append(f"token 对拍失败：期望 {hex_e} {name_e}，实际 {hex_a} {name_a}")
    wulan = [(i + 1, h) for i, (h, n) in enumerate(sig) if n == "雾蓝"]
    if wulan != [(14, "#C3CDDA")]:
        errors.append(f'"雾蓝"必须唯一指向 #14 #C3CDDA，实际 {wulan}')

    # ΔE 全对校验
    sig_lab = [(n, h, rgb_to_lab(hex_to_rgb(h))) for h, n in sig]
    pool_pairs = [(ciede2000(a[2], b[2]), a, b) for a, b in combinations(sig_lab, 2)]
    pool_min = min(pool_pairs, key=lambda p: p[0])
    if pool_min[0] < DE_MIN:
        errors.append(f"池内最小 ΔE {pool_min[0]:.1f} < {DE_MIN}（{pool_min[1][0]}/{pool_min[2][0]}）")
    cross = [(ciede2000(s[2], rgb_to_lab(hex_to_rgb(h))), s, (n, h))
             for s in sig_lab for h, n in sem]
    cross_min = min(cross, key=lambda p: p[0])
    if cross_min[0] < DE_MIN:
        errors.append(f"语义色对拍最小 ΔE {cross_min[0]:.1f} < {DE_MIN}（{cross_min[1][0]}/{cross_min[2][0]}）")

    # allowed_hex.txt 覆盖 gpl 全部 HEX
    allowed = {ln.split()[0].upper() for ln in ALLOWED.read_text(encoding="utf-8").splitlines()
               if ln.strip().startswith("#") and len(ln.split()[0]) == 7}
    for hex_, name in entries:
        if hex_.upper() not in allowed:
            errors.append(f"allowed_hex.txt 缺 {hex_}（{name}）")

    # palette-data.js 同步
    expected_js = render_palette_js(sig, sem, tok)
    if "--export" in sys.argv:
        PALETTE_JS.parent.mkdir(parents=True, exist_ok=True)
        PALETTE_JS.write_text(expected_js, encoding="utf-8")
        print(f"已导出 {PALETTE_JS.relative_to(REPO_ROOT)}")
    elif not PALETTE_JS.exists() or PALETTE_JS.read_text(encoding="utf-8") != expected_js:
        errors.append("stream/js/lib/palette-data.js 与 gpl 不同步（跑 --export 重建）")

    print(f"池内 120 对最小 ΔE = {pool_min[0]:.2f}（{pool_min[1][0]}/{pool_min[2][0]}）")
    print(f"签名色×语义色 96 对最小 ΔE = {cross_min[0]:.2f}（{cross_min[1][0]}/{cross_min[2][0]}）")
    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        return 1
    print("OK: gpl 对拍 / ΔE 全对 ≥15 / allowed 覆盖 / palette-data 同步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
