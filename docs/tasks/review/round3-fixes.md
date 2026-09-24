# 任务文档评审 · 第 3 轮（终审，2026-09-24）修复指令

> R3 两位终审报告：P0 = 0；P1 去重后 14 条，全部为局部修补。仲裁者已修 00/09（00：goals.yaml 入 §2 清单、三检查脚本入清单、部署期 env 行补登；09：升 v1.3、M0 E7 点名 T-CFG-02、M4 E9/M6 E14 env 增量复查、step⑤ admin/lite 端口钉 5173）。本表派 4 个 fixer 修模块文档。**修完即定稿，开始 M0 开发。**

## F1（01、02）

**01**：
1. T-CFG-02 `thresholds:` 枚举补"干预率上限 `intervention_rate_cap`（数值见源方案 §4.8/06 §3）"——04 T-DIR-03 与 08 T-AUD-05 都读它，无写入方会读空（R3-A P1-7）。
2. T-CFG-02 验收 3 扩展为 pytest 用例：embed 配置（`local`/bge-m3/1024）与 `ddl/schema_v1.sql` 文本的 `vector(1024)` 对拍（静态、从 yaml 读值）——09 M0 E7 的执行体（R3-B P2-2）。
3. v1.2 修订说明中裸 `(A5)/(A3)/(A18)/(A4)/(A8)/(A12)` 补 `R2 §` 前缀（R3-B P1-5）。

**02**：
4. T-TIME-02 交付物删 `server/config/speed_table.yaml`（M0 唯一归 01 T-CFG-01，本文消费既有文件）；验收 1 注"数值校验归 01，本文持加载/段切换/热更行为"（R3-A P1-1）。
5. 目标系统任务（goals 段）载体定名 `server/config/goals.yaml`（已入 00 §2 清单），行文修正悬空引用（R3-A P1-2）。
6. T-ADJ-09 补一句：canonical.py 同持 **events 流** `canonical(payload)` 与 `digest_events`（05 §2.2 公式），07 T-SYN-01/09 消费（R3-A P1-8）。
7. T-ADJ-03 收编 debts 行写入/核销（属 step5 动作结算，04 §5.2 设计口径）；D2 改写为"仅逾期日结算扫描归 04 T-WA-04"（R3-A P1-9）。

## F2（03、04）

**03**：
8. T-LLM-03 验收 3 括注"（09 M0 E7 同口径）"→"（09 M2 E1 同口径）"（R3-A P1-3 / R3-B #6）。

**04**：
9. T-DIR-01 验收 5 `test_budget_enforcement` 移归 T-DIR-03 验收（配额耗尽被 T-DIR-03 拒绝，构建序 DIR-01 先于 DIR-03）；04 §3 依赖图补 T-WA-04→T-DIR-03 边（R3-A P1-6 + P2-1）。
10. v1.2 修订说明裸 A 编号检查补 `R2 §` 前缀（如有）。

## F3（05、06）

**05**：
11. T-WEB-08 vite.config.ts 钉 `server.port = 5173`；T-WEB-20 start_local.sh 就绪检查含 5173（09 step⑤ 已改指 5173）（R3-A P1-5 / R3-B #1）。
12. T-WEB-13 依赖行补 02 T-LOD-04（offsite 角标数据源，R2 §A.10 已点名的链）。
13. portraits.ts 里程碑倒挂修复：§3 依赖表补 T-ART-02（脚本先行段）（R3-B #3）。
14. v1.2 修订说明裸 `(A1)/(A17)/(A3)/(A15)/(A12)` 补 `R2 §` 前缀。

**06**：
15. T-ART-02 标注"生成脚本（`gen_portrait_manifest.py` 含 manifest.json 与 `web/src/lib/portraits.ts` 派生、`--check`）**M0 后即可、M4 前必须交付**（脚本先行，05 T-WEB-11 消费）；manifest/portraits 在 stream 页的消费留 M5 既有"（R3-B #3）。
16. T-LTV-07 的 start_local.sh 形态描述改"形态以 05 T-WEB-20 为准（PG + 内核 + obs_refresh 常驻 + obs-api + web dev server）"（R3-B #7）。

## F4（07、08）

**07**：
17. T-SYN-10 改"`.env` 恰一行值变更（指向 `worldsim_replica`）；`.env.example` 条目 M4 已由 T-WEB-02 创建，本任务不动"；D2 将 `WSIM_OBS_PG_DSN` 从 T-SYN-03 追加范围剔除（T-SYN-03 七键清单本就不含它）（R3-B #4）。

**08**：
18. T-OPS-05（"09 §8 七步逐字实现"段）对齐 09 §8 现文：① 栈枚举补 `obs_refresh 常驻`；④ 前置"经 05 T-WEB-02 tokens_cli 签发 dev token"；⑤ 三 URL 写死（admin/lite = `http://127.0.0.1:5173/map`、`/lite/home`，stream = `http://127.0.0.1:8080/stream/?token=<dev token>`）（R3-B #2）。
19. A 编号三处：`08:15` 的 `(A5)` → `R1 §A.5`；`08:252`、`08:286` 的 `(A5)` → `00 §1 A5`（同文档同号两义修复，R3-B #5）。
20. D12 表述对齐："已登记 00 §2 附表（部署期行，仲裁者已补登）"。

## 定稿标准

全部落地后各 fixer grep 自检（裸 A 编号/旧引用），仲裁者抽查销项 → 任务文档定稿，进入 M0。
