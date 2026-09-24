#!/usr/bin/env bash
# ============================================================================
# schema_freeze_check.sh —— 06 §4.5 冻结差集检查（常设流程）
# 用途：schema-v1 tag 前置闸门（T-DB-06 / 09 M0 E9）；此后每次契约变更（06 §4.4）复跑。
# 用法：bash server/scripts/schema_freeze_check.sh（cwd 任意）；两项差集均为空 → 退出码 0，
#       否则非零（差集非空不得打 tag：回登 06 或回改文档）。输出全文留痕于 commit / tag annotation。
#
# ① 事件类型差集（06 §4.5 grep 口径）：
#   候选 = docs/design/{01,03,04}*.md 全文反引号点号串 `[a-z]+(\.[a-z_]+)+`，逐条比对注册表；
#   注册表 = server/config/event_types.yaml（06 §1.2 导出镜像，单源生成链 00 §1 A11，间接核对 06 §1.2）；
#   语境过滤（引述/记录不计）：行首 `>`（文档头修订说明块）、`## 附` 起至文末（修复记录）、含"旧名"行（改名对照）；
#   非类型串（字段名/表名/文件名/payload 路径，06 §4.5 "剔除字段名/表名/文件名"）= TYPE_EXCLUDE 白名单（逐条人工核对）；
#   命中 LEGACY（06 §1.4 旧名，点号形态）一律视为差集（回改原文档，不登记入 §1.2）。
#
# ② 数值差集（工程化口径，01 文档 §6 D18 登记）：
#   范围（06 §4.5"阈值/配比/额度/频次/通过线"）= 含数字且命中契约关键词的行；
#   剔除标记行（"见 06"/"见持有方"/"持有方"/"实测回填"/"回填"/"读值"——06 §4.5：标注行不计入）；
#   预处理剥离：节号引用（xx §y.z / §y.z / 标题号）、版本号、评审编号（P2-10/R2 等）、时分（9:30）、千分位逗号；
#   差集 = 候选"数字@文档"对 ∖ 06 §3 数字登记表 ∖ NUM_EXCLUDE 排除表（排除表 = 逐条人工核对留痕，
#   全部为持有方原文/工程常量/示例值；字面全量数值对拍不可自动化——持有方文档的原文数字恒在）。
#   注意：排除表按"数字@文档"精确对；新文档/新语境引入未登记数字会立即变红——排除表膨胀须先过评审。
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

DOCS=(docs/design/01*.md docs/design/03*.md docs/design/04*.md)
REGISTRY_YAML=server/config/event_types.yaml
CONTRACT_06=docs/design/06-契约登记表.md
rc=0

# ---------- ① 事件类型差集 ----------
mapfile -t CAND_TYPES < <(
  for f in "${DOCS[@]}"; do
    awk '
      /^## 附/ {in_appendix=1}
      {
        line=$0
        if (line ~ /^>/) next
        if (in_appendix) next
        if (line ~ /旧名/) next
        while (match(line, /`[a-z]+(\.[a-z_]+)+`/)) {
          print substr(line, RSTART+1, RLENGTH-2)
          line = substr(line, RSTART+RLENGTH)
        }
      }' "$f"
  done | LC_ALL=C sort -u
)
mapfile -t REGISTRY < <(
  grep -oE '^[[:space:]]*- \{ type: [a-z._]+' "$REGISTRY_YAML" | grep -oE '[a-z._]+$' | LC_ALL=C sort -u
)

# 非类型串白名单（字段名/表名/文件名/payload 路径；2026-09-24 逐条人工核对，T-DB-06）
TYPE_EXCLUDE=(
  agents.cognition_tier agents.mood agents.needs            # agents 表列名（04 §5.2）
  debts.due_sim                                             # debts 表列名
  director.grade_revise.new_grade                           # payload 字段路径（06 §1.2 该行字段）
  events.actors events.rng_seed events.seq events.visibility # events 表列名
  health.cost_daily                                         # 05 §3.5 副本物化表名
  index.html metrics.py models.yaml worldsim.env            # 文件名
  memories.content memories.id                              # memories 表列名
  payload.about payload.caused_by payload.cites payload.distortion payload.teller
  payload.text_display payload.text_raw                     # payload 字段路径（06 §2）
  reflection.text_display                                   # 字段路径（03 §5.3 模板键）
  ui.grade                                                  # ui 字段（06 §2）
)
# 06 §1.4 旧名（点号形态；命中即差集）
LEGACY=(
  world.payday bill.rent bill.utility bill.rent.overdue bill.rent.notice
  work.overtime company.layoff_rumor company.crisis stock.tick world.stock
  perf.review promotion.window
  disturb.illness disturb.weather disturb.complaint disturb.lucky
  appointment.created appointment.remind appointment.stood_up
  agent.chat agent.gossip agent.argue agent.invite agent.refuse
  sim.day_summary time.catchup_start time.catchup_end llm.failover
)

in_set() { local x="$1"; shift; local e; for e in "$@"; do [ "$x" = "$e" ] && return 0; done; return 1; }

TYPE_DIFF=(); LEGACY_HIT=()
for c in ${CAND_TYPES[@]+"${CAND_TYPES[@]}"}; do
  in_set "$c" ${REGISTRY[@]+"${REGISTRY[@]}"} && continue
  if in_set "$c" ${LEGACY[@]+"${LEGACY[@]}"}; then LEGACY_HIT+=("$c"); fi
  in_set "$c" ${TYPE_EXCLUDE[@]+"${TYPE_EXCLUDE[@]}"} || TYPE_DIFF+=("$c")
done

echo "== ① 事件类型差集（01/03/04 ∖ 06 §1.2 注册表镜像；06 §4.5 口径）=="
echo "候选 ${#CAND_TYPES[@]} 条；注册表 ${#REGISTRY[@]} 条；非类型串白名单 ${#TYPE_EXCLUDE[@]} 条（字段/表/文件，已逐条核对）"
if [ "${#TYPE_DIFF[@]}" -eq 0 ] && [ "${#LEGACY_HIT[@]}" -eq 0 ]; then
  echo "差集：空（无未注册类型、无 §1.4 旧名残留）"
else
  rc=1
  [ "${#TYPE_DIFF[@]}" -gt 0 ] && printf '未注册类型: %s\n' "${TYPE_DIFF[@]}"
  [ "${#LEGACY_HIT[@]}" -gt 0 ] && printf '命中 06 §1.4 旧名（回改原文档）: %s\n' "${LEGACY_HIT[@]}"
fi
echo

# ---------- ② 数值差集 ----------
# 06 §3 数字登记表全量数字（剥离节号引用后提取）
mapfile -t NUM_REGISTRY < <(
  sed -n '/^## 3\. 数字登记表/,/^## 4\. 仲裁条款/p' "$CONTRACT_06" \
    | sed -E 's/[0-9]+ ?§[0-9.]+//g; s/§[0-9]+(\.[0-9]+)*//g' \
    | grep -oE '[0-9]+(\.[0-9]+)?' | LC_ALL=C sort -u
)

# 排除表（数字@文档，逐条人工核对 2026-09-24；全部为持有方原文/工程常量/示例值，理由随行登记）
NUM_EXCLUDE=(
  "0.62@04"   # 04 §4.2 持有方原文（PROMOTE_IN 迟滞带；models.yaml thresholds 镜像，T-CFG-02 对拍）
  "1.0@01"    # 01 §4.1 fidelity 初始值（06 §2 字段表登记项，非 §3 数字表）
  "1.2@01"    # 01 §3.5 调参杠杆表持有方原文（社交衰减 ×1.2 ≈ 接受率 +2~4%）
  "1.3@01"    # 01 §3.5 调参杠杆表持有方原文（±5 点 ≈ 接受率 ±1.3%）
  "1.7@01"    # 01 §6.1 统计注记（单日跌 >3% ≈ 1.7σ）
  "100@01"    # 01 §3.2 tension 回归率示例（tension=100 回归 10%/周）
  "2000@01"   # 01 §4 持有方原文（borrow_money 单笔 ¥2,000 上限；01 §2.1 拓扑豁免同源）
  "5000@01"   # 01 §2.1 持有方原文（初始债务 ≤¥5,000）
  "24@04"     # 04 §8.4 持有方原文（熔断升级：24 真实小时仍 >2× 暂停时钟）
  "256@03"    # 03 §3.1 坐标换算（持有方 02 §1.1.1，03 引用：320×256 像素）
  "30@01"     # 01 §3.2 tension 回归示例（tension=30）
  "30@03"     # 03 §3.1 世界设定常量引用（30 个地点节点，持有方 01 §1.2/§1.3）
  "30@04"     # 04 §7.3/§8.2 持有方原文（归档 30 模拟天 / 撞墙冷却 30 分钟）
  "320@03"    # 03 §3.1 坐标换算（持有方 02 §1.1.1，03 引用）
  "35@04"     # 04 §8.4 持有方原文（降速档深度反思阈值 20→35）
  "429@04"    # HTTP 状态码工程常量（04 §8.2 撞墙判定 429/5xx）
  "5000@04"   # 04 §12.3 持有方原文（sync 滞后告警阈值 max(seq)−last_acked_seq > 5000）
  "50@03"     # 03 §9 性能预算工程口径（内存增长 >50MB）
  "6.0@04"    # 04 §3.1 持有方原文（max_catchup_ratio；speed_table.yaml 镜像）
  "64@03"     # 03 §3.1 坐标换算（持有方 02 §1.1.1，03 引用：80×64 逻辑单位）
  "72@01"     # 01 §4 持有方原文（borrow_money 冷却 72h，01 §3.4 冷却表）
  "8.3@03"    # 03 §3.6 mock 示例值（健康面板渲染占位，非阈值）
  "80@01"     # 01 §3.2 tension 回归示例（tension=80）
  "80@03"     # 03 §3.1 坐标换算（持有方 02 §1.1.1，03 引用：80×64）
  "80@04"     # 04 §12.3 持有方原文（磁盘 >80% 告警）
  "90@04"     # 04 §8.2/§12.3 持有方原文（RPM >90% / GPU 显存 >90%）
  "95@04"     # 04 §2.2/§8.2/§9.2 工程口径（p95 延迟/预算占比）
)

mapfile -t NUM_PAIRS < <(
  for f in "${DOCS[@]}"; do
    tag="$(basename "$f" | cut -c1-2)"
    awk -v tag="$tag" '
      /^## 附/ {in_appendix=1}
      {
        line=$0
        if (line ~ /^>/) next
        if (in_appendix) next
        if (line ~ /见 06|见持有方|持有方|实测回填|回填|从 .*读值|不抄字面量/) next
        if (!(line ~ /[0-9]/ && line ~ /阈值|上限|下限|报警|熔断|干预率|接受率|通过线|复看率|配额|KPI|门禁|红线|单价|预算|额度|配比|频次/)) next
        gsub(/[0-9],([0-9]{3})/, "", line)                      # 千分位逗号（¥5,000 → ¥5000）
        gsub(/[0-9]+ ?§[0-9.]+/, "", line); gsub(/§[0-9]+(\.[0-9]+)*/, "", line)
        gsub(/^#+[[:space:]]*[0-9.]+/, "", line)
        gsub(/[vV][0-9]+(\.[0-9]+)+/, "", line)
        gsub(/N?-?P[0-9]+-[0-9]+/, "", line); gsub(/R[0-9]+/, "", line)
        gsub(/[0-9]{1,2}:[0-9]{2}/, "", line)
        gsub(/[0-9]+\.[0-9]+\.[0-9.]+/, "", line)
        while (match(line, /[0-9]+(\.[0-9]+)?/)) {
          print substr(line, RSTART, RLENGTH) "@" tag
          line = substr(line, RSTART+RLENGTH)
        }
      }' "$f"
  done | LC_ALL=C sort -u
)

NUM_DIFF=()
for p in ${NUM_PAIRS[@]+"${NUM_PAIRS[@]}"}; do
  num="${p%@*}"
  in_set "$num" ${NUM_REGISTRY[@]+"${NUM_REGISTRY[@]}"} && continue
  in_set "$p" ${NUM_EXCLUDE[@]+"${NUM_EXCLUDE[@]}"} || NUM_DIFF+=("$p")
done

echo "== ② 数值差集（01/03/04 契约关键词行数字 ∖ 06 §3；工程化口径 §6 D18）=="
echo "候选 ${#NUM_PAIRS[@]} 对（数字@文档）；06 §3 登记数字 ${#NUM_REGISTRY[@]} 个；排除表 ${#NUM_EXCLUDE[@]} 条（持有方原文/工程常量，已逐条核对）"
if [ "${#NUM_DIFF[@]}" -eq 0 ]; then
  echo "差集：空（无未登记的跨文档契约数字）"
else
  rc=1
  printf '未登记数字（回登 06 §3 或回改文档）: %s\n' "${NUM_DIFF[@]}"
fi
echo

if [ "$rc" -eq 0 ]; then
  echo "PASS：两项差集均为空，可打 schema-v1 tag（06 §4.5 / T-DB-06）"
else
  echo "FAIL：差集非空，禁止打 tag（06 §4.5）" >&2
fi
exit "$rc"
