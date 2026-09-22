---
feature: ui-clarity
status: designed
updated: 2026-09-23
branch: feature/ui-clarity
commits: 
---

# UI Clarity（结论句 / 缺数提示 / 时间范围持久化）

## Report

## [S1] Problem

外部参考（68hub / cc-switch）评审后确认三处与「统计口径准确、界面一眼看清」直接相关、且当前面板缺失：

1. 分析页「调用分布」「调用排名」、概览「性能健康」只有数字/列表，没有一句话结论，用户需自行心算。
2. 缺字段（尤其 `avg_latency_ms` 缺失）时可能显示 `0ms` / `NaNs`，把「无数据」伪装成测量值 —— 违反「无数据 ≠ 0」。
3. 分析页时间范围（小时/天/月）刷新或重开面板后回到默认「天」，不记住用户选择。

## [S2] Design

### S2.1 面板底部一句话结论

- **调用分布**：面板 body 底部（图例下方）增加一行 takeaway（10px、`var(--muted)`）。有数据时输出：请求最多的模型名 + 占比（`fmtPct`）；无 `per_model` 或 `total_requests` 为 0 时不渲染该行。
- **调用排名**：同上，基于 `top_models[0]` 的请求占比（与列表内请求数同源）。
- **性能健康（概览）**：在 top models 列表之后一行结论：成功率用窗口 `sRateText`（与 hero 同口径）；错误数、平均延迟用现有 `stats` 字段。无请求时不渲染（整卡本就仅在有请求时出现）。
- 文案 i18n 中英各一，占位符固定顺序，**不改任何统计计算**。
- 结论只陈述已有数字，百分比一律 `fmtPct`。

### S2.2 缺数显示为 `—` 并可解释

- 修正 `fmtMs`：入参非有限数或 `null`/`undefined` → 返回 `'—'`；`0` 视为无有效延迟 → `'—'`（真实端到端延迟不会为 0ms）。
- 概览「平均延迟」：去掉 `||0`，直接走 `fmtMs` 缺数分支。
- 模型明细 / 供应商性能中的延迟、生成速度：缺字段统一为 `'—'`；不引入新 `||0`。
- **不**给每个 `—` 强制加 Info 图标；协议级缓存写入 N/A 长说明本轮不做。
- 结论句新增 i18n；`—` 不翻译。

### S2.3 分析页时间范围持久化

- 存储键：`aigate.analyticsSpan`，值：`hour` | `day` | `month`。
- `setSpan` 更新状态后 `localStorage.setItem`（try/catch 静默失败）。
- Alpine `init()` 读取并校验枚举，非法/缺失用默认 `day`；读到合法值后同步 `analyticsRange` / `trendGranularity`（复用 map，init 内直接赋值，避免重复请求）。
- 不在读取时额外 `fetchStats`（仍走既有首载路径）。

### 契约摘要

| 项 | 契约 |
|---|---|
| 结论行 | 仅 UI + i18n；数据源 = 现有 `stats` |
| `fmtMs` | 缺数/非有限/0 → `'—'`；有效延迟格式不变 |
| 持久化 | 仅 `analyticsSpan` 三值；写在 `setSpan`，读在 init |

## [S3] Out of Scope

- 不引入框架、毛玻璃、动画、里程表数字。
- 不做概览「最近 5 条」、Hero 全精度双写、成本双轴、中文亿/万。
- 不改 rollup / 命中率 / 费用等任何后端或统计口径。
- 不重加 Token 活动面板。
- 不在本分支处理 stash 中的 0.5.7 实验徽标改动。
- 不做协议级缓存写入 N/A 长说明。

## Tasks

- [ ] T1: 分析页调用分布/排名 + 概览性能健康补一句话结论 — acceptance: 有数据时面板底部出现中英文结论行且占比走 `fmtPct`；无数据不渲染；`node --check` 通过 (covers: S2.1)
- [ ] T2: `fmtMs` 与平均延迟缺数显示 `—` — acceptance: `fmtMs(null|undefined|0|NaN)` 为 `'—'`；概览平均延迟不再 `||0` 显示 0ms；有效值格式与现网一致 (covers: S2.2)
- [ ] T3: 分析页 `analyticsSpan` localStorage 持久化 — acceptance: `setSpan` 写入；刷新/重开后选中项与窗口映射恢复；非法值回退 day；JS 语法检查通过 (covers: S2.3)
