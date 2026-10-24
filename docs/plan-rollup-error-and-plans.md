# 方案：rollup 存失败原因 + 套餐型供应商的表达

状态：**待拍板**（2026-10-24）。本文只给方案与取舍，不改代码。

---

## 一、rollup 存失败原因

### 问题（已实测确认）

供应商行上现在能显示「最近一次失败」（`ProviderStats.last_error`），但这个字段
**只覆盖 `logs.jsonl` 当前窗口**。实测：

- `logs.jsonl` 5000 条滚动窗口里 **zen 一条都没有**；
- zen 的 43 次调用全部来自 `daily_stats.jsonl` 的 rollup；
- rollup 只存 `errors` 计数，**不存错误正文、不存状态码**；
- 结果：zen 显示「43 次 / 成功率 0.0%」但**没有原因胶囊**，用户仍然不知道为什么。

这不是显示 bug，是数据缺口。

### 现状数据结构

`rollup.rs` 的 `RollupEntry`（供应商 × 上游模型 × 单日）已有 `errors: u64`，
穷举了 token/耗时/省量/计费各维度，**唯独没有错误的定性信息**。

`add_log()` 里 `if is_log_error(log) { self.errors += 1; }` —— 计数即丢弃。

### 三个候选方案

#### 方案 A：只存「最近一次错误的摘要」（推荐）

`RollupEntry` 新增两个字段：

```rust
/// 该日最近一次失败的错误摘要（按 timestamp 取最大）。
/// 截断到 160 字符，与 ProviderStats.last_error 同口径。
#[serde(default)]
pub last_error: Option<String>,
/// 该日最近一次失败的 HTTP 状态码。
#[serde(default)]
pub last_error_status: u16,
/// 该错误的时间戳，用于跨天合并时判断谁更新。
#[serde(default)]
pub last_error_ts: u64,
```

`add_log()` 里追加：

```rust
if crate::admin::is_log_error(log) {
    self.errors += 1;
    if log.timestamp >= self.last_error_ts {
        self.last_error_ts = log.timestamp;
        self.last_error_status = log.status;
        self.last_error = log.error.as_deref().map(crate::admin::truncate_err);
    }
}
```

合并多天时按 `last_error_ts` 取最大（不是按天序，天序与错误时间不完全一致）。

**成本**：每个 (供应商×模型×天) 最多多约 200 字节。按当前数据量
（10 家 × 平均 4 模型 × 90 天）估算 **< 1 MB**，可忽略。

**取舍**：只知道「最近一次」是什么，不知道各类错误的分布。
但对面板要回答的问题（「这家现在为什么红」）已经够了。

#### 方案 B：存分类计数（错误码 → 次数）

```rust
#[serde(default)]
pub error_kinds: std::collections::BTreeMap<u16, u64>,
```

**优点**：能看到「404 出现 40 次、429 出现 3 次」这种分布。
**缺点**：看不到**具体错误正文**（上游的 `msg` 往往比状态码有用得多，
比如 workbuddy 的 11-128 与普通 400 都是 400 族，但含义完全不同）。
且 `BTreeMap` 在 rollup 里会显著增加序列化体积与合并复杂度。

#### 方案 C：A + B 都存

信息最全，但体积翻倍，且两个字段要各自维护合并逻辑（易漂移）。
**当前阶段不推荐**：没有消费方需要分布，属于「为假设的未来需求设计」。

### 推荐：方案 A

理由：面板现有的一处消费方（供应商行胶囊）只需要「最近一次为什么失败」。
按本项目一贯的「不为假设的未来需求设计」，先做最小可用的那一层。

### 关键约束（血泪教训，必须遵守）

`rollup.rs:61-62` 已经写明：

> ⚠️ 新增字段一律带 `#[serde(default)]`：落盘的旧行没有该字段，缺默认值会让
> 整行反序列化失败 → 该天所有历史统计静默消失（已踩过 `d:0` 化石行那次）。

所以：
1. **三个新字段全部 `#[serde(default)]`**；
2. 必须先写单测：用**不含新字段的旧 JSON** 反序列化，断言不报错且旧字段值正确；
3. `last_error: Option<String>` 的 `default` 是 `None`，`last_error_status`/`last_error_ts` 是 `0`
   —— 与「未采集」语义一致（不能显示成「0 次错误」）。

> ⚠️ **实测修正（2026-10-24 已落地后验证）**：`Option<T>` 字段即使**去掉**
> `#[serde(default)]` 也能从缺失字段反序列化 —— serde 对 `Option` 的默认行为本就是
> 「缺失 → None」。实测：只去掉 `last_error_status: u16` 的 default 才报
> `missing field 'last_error_status'`，而 `Option<String>` 那条不报。
>
> 结论：`#[serde(default)]` 对 `Option` 是**风格统一 + 防将来改类型**，不是当前的必要条件；
> 而 `u16` / `u64` 两个字段**必须有**，它们是真正守兼容性的那两个。
> 不要宣称"测试守住了 Option 字段" —— 那条测试对它没有判别力。

### 兼容性

- 旧数据读进来后 `last_error = None` → 面板对历史天显示「无原因」而不是编造。
- 新数据落盘后，旧版本二进制读它：未知字段会被 serde **忽略**（不是默认行为失败），
  旧版本照常工作。这点要写单测确认（`serde` 默认忽略未知字段）。

### 实施步骤

1. `RollupEntry` 加三字段（全部 `#[serde(default)]`）；
2. `add_log()` 里按 `timestamp` 更新；
3. `compute_stats` 的 rollup 合并分支：按 `last_error_ts` 取最大合并；
4. **删掉**当前 rollup 分支里那句 `last_error: None`（那是占位，现在能填真值了）；
5. 单测：
   - 旧 JSON（无新字段）能反序列化；
   - 新字段能往返序列化；
   - 多天合并取的是**时间戳最大**那条（把旧错误的天放在后面，防「取最后一天」）；
   - 旧版本读新 JSON 不炸（未知字段忽略）；
6. 探针：`probe_prov_cols.py` 加一条 —— 只有 rollup 数据的供应商（如 zen）
   也必须有胶囊。**这条是本方案唯一的验收标准**。

### 风险

| 风险 | 缓解 |
|---|---|
| 旧行反序列化失败 → 历史统计整片消失 | 全字段 `#[serde(default)]` + 专项单测 |
| 错误正文含用户内容被长期落盘 | 与 `ProviderStats` 同口径截断 160 字符；且此字段本就来自上游响应体，不新增暴露面 |
| 存储膨胀 | 每行 ≤200 字节，实测数据量下 < 1 MB |

### 落地结果（2026-10-24 已实施）

- `RollupEntry` 三个字段 + `ProviderStats.last_error_ts` 已加，`add_log()` 按
  `timestamp` 取最大，`merge_rollup_days` 的 existing / new 两分支都按
  `last_error_ts` 取更新那条。
- 端到端实测：构造「只有 rollup、logs 里没有 zen」的隔离实例，
  `range=29d` 下 `per_provider` 出现 zen 且三个字段值完整正确。
- 单测 6 条（旧 JSON 兼容、往返、忽略未知字段、取最大时间戳、成功/499 不污染、截断）。
- 改坏验证：u16/u64 去掉 default、改无条件覆盖、去掉截断 —— **4 项转红**。

**判据边界（必须如实记录）**：`Option<String>` 去掉 `#[serde(default)]` 后
测试**仍然通过** —— serde 对 `Option` 的缺失本就返回 `None`。所以那条测试
对 Option 字段没有判别力；真正守兼容性的是 `u16` / `u64` 两个字段的 default。
`default` 对 Option 仍保留（风格统一 + 防将来改类型），但不宣称它被测试守住。

### 顺带发现的既有缺陷（**非本次引入，未修**）

`admin.rs:4746` 是 `if let Some(cover_ts) = state.log_buffer.oldest_ts().await`，
而 `oldest_ts()` 在**缓冲区为空时返回 `None`** → 整个 rollup 合并被跳过。

后果：日志被清空（或全部轮转走）后，即使 `daily_stats.jsonl` 里有完整历史，
`per_provider` / `per_model` 也**完全看不到历史数据**。

实测复现：logs.jsonl 为空 + rollup 有一天的 zen → `range=29d` 仍返回 `per_provider = []`；
往 logs 里塞一条无关记录后立刻正常。

**修法（待定）**：`oldest_ts()` 为 `None` 时不应跳过整段合并，而应视为
「日志侧没有数据可防重复」→ 直接合并整个查询区间。
需注意与 `log_side.retain(...)` 的交互（空集 retain 是空操作，安全）。
本条仅供决策，本轮不动。

---

## 二、套餐型供应商（opencode / workbuddy / buddy / Joule）

### 参考项目怎么做的

读了 `.openbitfun/tmp/wali-full`（MIT）：

- **`AuthAccount.plan_type`**：账号级显式字段，卡片上直接显示「账号类型 <plan_type>」。
- **`AuthAccount.quota`**：结构化的额度窗口，不是单个数字：
  - `limits[]` 每项有 `primary` / `secondary` 两个窗口；
  - 每个窗口有 `used_percent`、`window_minutes`（5H=300 / 周=10080 / 月=43200）、`reset_at`；
  - `quotaDisplayState(usedPercent)` 统一算「剩余 %」与色调：
    **剩余 0 → destructive，≤30% → warning，其余 success，未知 → unknown**。
- **`quota.exceeded`**：不是只显示 —— 它**参与路由**，耗尽的账号被踢出候选，
  卡片上直接标「已踢出路由 · 订阅限额已耗尽」。
- **`invalidation_reason`**：把失败原因映射成**可操作的中文建议**，例如
  `payment_required` → 「订阅未激活或权益无法验证（Kimi 侧返回 402）。请先在 Kimi
  确认订阅生效，再重新登录或同步模型。」—— 注意它写清了**去哪、做什么**。

### 我们的现状与结构差异

这一点必须先说清楚，否则会照抄出不合适的东西：

| | 参考项目 | 我们 |
|---|---|---|
| 主体 | `AuthAccount`（一个账号一条） | `ProviderConfig`（一个供应商一条） |
| 额度 | 上游 API 返回结构化 quota | `BalanceInfo` + `QuotaProgress`（已有） |
| 多个账号 | 每个账号独立卡片 | Buddy 系走**账号列表**（`accounts.rs`） |
| 套餐概念 | `plan_type` 字段 | **没有**这个字段 |

我们已有的、不要重造的：

- `QuotaProgress`（`balance.rs:230`）+ `QUOTA_LOW_PCT = 80.0`；
- 「只算还有剩余的包」的合计口径（2026-10-10 定下）；
- `BalanceState::{Ok, Manual, Failed}` —— 已经能区分「实测/自填/查询失败」。

### 缺的是什么（按价值排序）

**① 失败原因 → 可操作建议（最高价值，且可立即做）**

参考项目的 `invalidationText()` 是纯前端查表：`reason → 一句「怎么办」`。
我们现在只显示原始错误正文（`429 [quota_exceeded]: 该免费模型今…`），
用户看完仍然不知道该做什么。

可以做：给 `last_error_status` + 正文做一层**模式匹配**，映射成建议，
同时**保留原文**（不能因为映射失败就丢掉证据）：

| 特征 | 建议文案 |
|---|---|
| 402 / `quota_exceeded` / `Insufficient` | 额度已用尽：换号、充值，或等配额重置 |
| 429 且含「频率/rate」 | 触发限流：稍后重试，或调低并发 |
| 429 且含「quota/额度」 | 额度耗尽：与 402 同 |
| 401 / 403 | 凭据失效或无权：重新登录该供应商 |
| 404 且响应是 HTML | 端点路径可能已变更：核对供应商文档（我们踩过 opencode 改路径） |
| 11-128 / `request illegal` | 渠道校验未通过：WorkBuddy 需重新登录（我们踩过） |
| TLS / handshake / unexpected EOF | 网络层：检查代理或上游可达性 |

**这一条不需要后端改结构**，纯前端查表即可，且立刻能让「红字」变成「下一步做什么」。

**② 套餐型供应商的「计费性质」显式化**

我们已有 `ProviderStats.billing`（按量计费 or 免费/月套餐），但它是**统计层**字段，
供应商配置里没有对应的显式声明。后果：面板上「优化省了多少钱」对套餐型供应商
只能显示 "—"，但用户看不出**为什么**是 "—"。

可以做：`ProviderConfig` 增加 `plan: Option<String>`（自由文本，如 `opencode Go` /
`CodeBuddy 月套餐`），仅供展示；或更小改动——在供应商行上把 `billing=false`
显示成一个「套餐/免费」徽章，让 "—" 有了解释。

**③ 额度窗口的粒度（需要先确认上游给不给）**

参考项目能显示 5H/周/月三档，是因为 codex/gemini 的上游接口**返回** `window_minutes`。
我们的 `resolve_balance_*` 解析的是各家的余额接口，形态不同。
**在确认某个上游真的返回窗口信息之前，不做这个** —— 否则就是编造。

**④ `quota.exceeded` 参与路由**

参考项目把「额度耗尽」直接用于剔除候选账号。我们是「一供应商一端点」，没有多候选
failover，所以**不适用**（此前已核实 `ProviderConfig` 没有 priority/weight）。

### 建议的落地顺序

1. **先做 ①**（纯前端，零风险，立刻改善「红字看不懂」）；
2. 再做 ② 的徽章版（一行 + 一个 i18n 键）；
3. ③ 先做**取证实测**：拉一次真实上游响应，看究竟有没有窗口字段，再决定。

### 明确不做

- 不引入 `plan_type` 的枚举体系（我们没有账号级订阅模型，硬套会得到空字段）；
- 不做 quota 参与路由（无多候选）；
- 不照抄它的额度进度条样式（我们 `QuotaProgress` 已有自己的口径与 `QUOTA_LOW_PCT`）。

---

## 需要拍板的三点

1. **rollup 方案 A / B / C**（推荐 A）；
2. **失败原因映射**：是否按上表做查表建议？（推荐做，保守起见映射不上时仍显示原文）；
3. **套餐标识**：`ProviderConfig.plan` 自由文本 vs 只加一个「套餐/免费」徽章（推荐后者，改动更小）。
