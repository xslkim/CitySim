# stream/ 直播画面页（M5 · 06-直播画面与美术接入）

零依赖静态页（00 §1 A7 / 00 §3「直播页」行）：原生 HTML/CSS/ESM JS，无构建步骤、无 npm 依赖
（`package.json` 仅钉 `type=module` 供 `node --test`）。竖屏 390×844，normal / climax 两形态，
WS 直连 obs-api（03 §5.2），token 鉴权（03 §8.3）。**不做平台直播 UI**（弹幕/礼物是平台的，A7）。

## 启动

```bash
bash deploy/start_local.sh        # 一条命令起全栈（PG+内核+obs_refresh+obs-api+web dev；05 T-WEB-20）
# 直播页由 obs-api 托管（T-ART-03 三挂载），随 obs-api 起停：
#   http://127.0.0.1:8080/stream/?token=<dev token>
# dev token 签发：cd server && uv run python -m worldsim.observe.tokens_cli issue --label <name>
# OBS 浏览器源 / 直播伴侣截取：390×844 打开上 URL
```

## URL 参数

| 参数 | 作用 |
|---|---|
| `?token=` | dev token（03 §8.3；缺省仅静态画面） |
| `?from=<seq>` | hello 携带 last_seq 补推（验收钩子，03 §5.2 resume） |
| `?mock=<chat\|fallback\|confess\|gradeA>` | 本地录制脚本驱动（截图验收，不触 WS；js/mock.js） |
| `?anim=0` | 禁用动画（headless virtual-time 截图确定性钩子） |
| `?float=0` | 飘屏占位整组隐藏 |
| `?soak=1` | 长挂打点：每 30s console 输出 heap/事件计数（T-LTV-07 验收 6） |

## 模块结构

```
index.html            骨架（舞台底图/标签/水印/挂载点/字幕条）
css/  tokens.css(02 §7.1 原值) screen.css pcard.css climax.css float.css
js/lib/ palette-data.js(gpl 生成物) roster.js(T-ART-02) ws-client.js(T-LTV-02)
        subtitle.js(T-LTV-03) mode.js(T-LTV-05)
js/ui/  subtitle-view.js pcard-view.js(T-LTV-04) float-view.js(T-LTV-06)
js/     main.js(引导) dispatch.js(派发) mock.js(录制脚本)
assets/ stage-normal.svg(2F 厨房夜) stage-climax.svg(天台夜)（剥离叙事元素，06 §7 B5）
tests/  node --test（node --test tests/*.test.js）
```

## 截图检查清单（chromium headless，390×844；证据存 `var/shots/m5/`）

```bash
# 实时驱动（obs-api → WS）：注入合成事件（append-only INSERT，T-LTV-07 验收 2 允许）
cd server && uv run python ../var/m5_inject.py chat     # chat|confess|probe|argue|day|single
node var/m5_shot.mjs "http://127.0.0.1:8080/stream/?token=$T&from=<seq>&anim=0" var/shots/m5/x.png 6000 /tmp/console.log
```

1. 骨架：底图/左上地点+Day·HH:MM/右上 AI 水印/字幕默认隐藏（`t-ltv-01-skeleton.png`）
2. 特写单人位：写实特写（`t-ltv-04-pcard-portrait.png`）/签名色圆底+首字兜底（`…-fallback.png`）
3. 字幕：底缘距画面下缘 228px（220 安全区+8）；chip=签名色；meta 只放已出站键
4. climax：名场面标签/双人栈 front=当前说话人/字幕签名色描边/滤镜（`t-ltv-05-climax.png`、`m5-e3-real-climax.png`）
5. 飘屏占位可见且零遮挡（`t-ltv-06-float.png`）
6. 静态断言：`cd server && uv run python scripts/check_stream_layout.py`、`check_stream_hex.py`、`check_delta_e.py` 全绿
