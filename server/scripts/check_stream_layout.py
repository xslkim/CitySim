"""T-LTV-01/04/06 直播页布局静态断言 + A7/零依赖红线检查。

从 CSS 常量计算各浮层矩形，断言两两不相交：
- 字幕条底边距 ≥ 228px（220px 平台安全区（02 §6.2）+ 8px 间距，原型口径）；
- normal 形态组：特写单人位（.pcard-single）/ 飘屏投票位（.float-vote）/ 字幕条 / 底部 220px 安全区；
- climax 形态组：双人特写栈（.pstack front/back）/ 飘屏致谢位（.gift-float）/ 名场面标签
  （.tag-climax）/ 字幕条 / 安全区。
CSS 文件未交付（任务未到）时对应组 SKIP；全部交付后零 SKIP 才算完整。
另含：A7 平台 UI 红线 grep（豁免清单见 A7_EXEMPT）与零依赖外引 grep（T-LTV-01 验收 3/4）。

用法：`cd server && uv run python scripts/check_stream_layout.py`
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
STREAM = REPO_ROOT / "stream"

PHONE_W, PHONE_H = 390, 844
SAFE_AREA_H = 220          # 底部平台安全区（02 §6.2）
SUBTITLE_MIN_BOTTOM = 228  # 220 + 8（原型口径）
SUBTITLE_MAX_H = 110       # 字幕条最大高度（工程上限：两行 16px 文本 + meta + 内边距）
FLOAT_VOTE_W, FLOAT_VOTE_H = 260, 26   # 投票位最大外接（工程上限）
GIFT_FLOAT_H = 50                      # 致谢位最大外接（工程上限）
TAG_CLIMAX_W, TAG_CLIMAX_H = 160, 24   # 名场面标签最大外接（工程上限）
PCARD_PAD_T, PCARD_PAD_X, PCARD_PAD_B, PCARD_CAPTION = 7, 7, 10, 9   # .pcard 结构与 figcaption 下突（原型）

# A7 豁免：class 名 float-vote 等不含禁词；画面文案"互动只影响下一段"不含禁词。
A7_EXEMPT: list[str] = []
A7_RE = re.compile(r"弹幕|关注|点赞|<button|<input", re.IGNORECASE)
EXTERNAL_RE = re.compile(r"<(?:script|link)[^>]+https?://", re.IGNORECASE)

Rect = tuple[int, int, int, int]  # x, y, w, h


def intersect(a: Rect, b: Rect) -> bool:
    return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0]
                or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])


def css_block(path: Path, selector: str) -> dict[str, int] | None:
    """提取 `selector { … }` 块内的 px 数值属性；文件/块缺失 → None。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", text)
    if not m:
        return None
    return {p.strip(): int(v) for p, v in
            re.findall(r"([\w-]+)\s*:\s*(-?\d+)px", m.group(1))}


def pcard_rect(left: int, top: int, width: int) -> Rect:
    """特写卡外接矩形（含 figcaption 下突 9px）：高 = pad_t + (w-2*pad_x) + pad_b + caption。"""
    h = PCARD_PAD_T + (width - 2 * PCARD_PAD_X) + PCARD_PAD_B + PCARD_CAPTION
    return (left, top, width, h)


def main() -> int:
    errors: list[str] = []
    skips: list[str] = []
    safe: Rect = (0, PHONE_H - SAFE_AREA_H, PHONE_W, SAFE_AREA_H)

    sub = css_block(STREAM / "css" / "screen.css", ".subtitle")
    subtitle: Rect | None = None
    if sub is None:
        errors.append("screen.css 缺 .subtitle 块")
    else:
        bottom = sub.get("bottom", 0)
        if bottom < SUBTITLE_MIN_BOTTOM:
            errors.append(f"字幕条 bottom={bottom} < {SUBTITLE_MIN_BOTTOM}（220 安全区+8）")
        bottom_edge = PHONE_H - bottom
        subtitle = (sub.get("left", 12), bottom_edge - SUBTITLE_MAX_H,
                    PHONE_W - sub.get("left", 12) - sub.get("right", 12), SUBTITLE_MAX_H)
        if intersect(subtitle, safe):
            errors.append("字幕条 ∩ 底部 220px 安全区 ≠ ∅")

    # ── normal 形态组 ──
    pc_css = css_block(STREAM / "css" / "pcard.css", ".pcard")
    single = css_block(STREAM / "css" / "pcard.css", ".pcard-single")
    fv = css_block(STREAM / "css" / "float.css", ".float-vote")
    if pc_css and single and subtitle:
        pcard = pcard_rect(single.get("left", 12), single.get("top", 44), pc_css.get("width", 120))
        for name, r in (("字幕条", subtitle), ("安全区", safe)):
            if intersect(pcard, r):
                errors.append(f"特写单人位 ∩ {name} ≠ ∅")
        if fv:
            vote: Rect = (fv.get("left", 12), fv.get("top", 180), FLOAT_VOTE_W, FLOAT_VOTE_H)
            if intersect(pcard, vote):
                errors.append("特写单人位 ∩ 飘屏投票位 ≠ ∅")
            if intersect(vote, subtitle) or intersect(vote, safe):
                errors.append("飘屏投票位 ∩ 字幕条/安全区 ≠ ∅")
        else:
            skips.append("float.css（T-LTV-06）未到，飘屏位 SKIP")
    else:
        skips.append("pcard.css（T-LTV-04）未到，特写位 SKIP")

    # ── climax 形态组 ──
    stack = css_block(STREAM / "css" / "climax.css", ".pstack")
    front = css_block(STREAM / "css" / "climax.css", ".pstack .pcard-front")
    back = css_block(STREAM / "css" / "climax.css", ".pstack .pcard-back")
    gift = css_block(STREAM / "css" / "float.css", ".gift-float")
    tagc = css_block(STREAM / "css" / "climax.css", ".tag-climax")
    if stack and front and back and subtitle:
        sx, sy = stack.get("left", 12), stack.get("top", 86)
        fr = pcard_rect(sx + front.get("left", 0), sy + front.get("top", 0), front.get("width", 120))
        bk = pcard_rect(sx + back.get("left", 96), sy + back.get("top", 44), back.get("width", 84))
        for name, r in (("特写栈 front", fr), ("特写栈 back", bk)):
            for other_name, o in (("字幕条", subtitle), ("安全区", safe)):
                if intersect(r, o):
                    errors.append(f"{name} ∩ {other_name} ≠ ∅")
        if gift:
            g: Rect = (PHONE_W - gift.get("right", 12) - gift.get("width", 192),
                       gift.get("top", 76), gift.get("width", 192), GIFT_FLOAT_H)
            for name, r in (("特写栈 front", fr), ("特写栈 back", bk)):
                if intersect(g, r):
                    errors.append(f"飘屏致谢位 ∩ {name} ≠ ∅")
        else:
            skips.append("float.css .gift-float（T-LTV-06）未到，致谢位 SKIP")
        if tagc:
            t: Rect = (tagc.get("left", 12), tagc.get("top", 46), TAG_CLIMAX_W, TAG_CLIMAX_H)
            for name, r in (("特写栈 front", fr), ("特写栈 back", bk)):
                if intersect(t, r):
                    errors.append(f"名场面标签 ∩ {name} ≠ ∅")
        else:
            skips.append("climax.css .tag-climax（T-LTV-05）未到，名场面标签 SKIP")
    else:
        skips.append("climax.css .pstack（T-LTV-05）未到，双人栈 SKIP")

    # ── A7 / 零依赖 grep（T-LTV-01 验收 3/4；T-LTV-06 同口径复跑）──
    for p in sorted(STREAM.rglob("*")):
        if not p.is_file() or p.suffix in (".png", ".svg"):
            continue
        text = p.read_text(encoding="utf-8")
        for line in text.splitlines():
            if any(x in line for x in A7_EXEMPT):
                continue
            if A7_RE.search(line):
                errors.append(f"A7 红线：{p.relative_to(REPO_ROOT)} 命中 {A7_RE.search(line).group(0)!r}：{line.strip()[:60]}")
            if EXTERNAL_RE.search(line):
                errors.append(f"零依赖红线：{p.relative_to(REPO_ROOT)} 外引 {line.strip()[:60]}")

    for s in skips:
        print(f"SKIP: {s}")
    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        return 1
    print("OK: 字幕条底边距 ≥228；各浮层矩形与安全区/字幕/飘屏两两不相交；A7/零依赖 grep 零命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())
