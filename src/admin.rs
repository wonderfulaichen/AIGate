//! 管理面板 — 请求日志环形缓冲区 + 可视化 Web 面板.
//!
//! 访问 http://127.0.0.1:8787/admin 打开面板.
//! 功能: 实时请求日志 / 使用统计 / 路由配置查看 / 健康检查.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::extract::{Path, State};
use axum::http::{HeaderName, HeaderValue, StatusCode};
use axum::response::{Html, IntoResponse, Response};
use axum::body::Bytes;
use axum::Json;
use futures::future::join_all;
use serde::Serialize;
use tokio::sync::Mutex;
use tracing::warn;

use crate::store::LogStore;
use crate::tooltip::{self, TooltipConfig};
use crate::balance::ProviderBalanceConfig;
use crate::pricing::{self, ModelPrice};

/// 面板「最近请求」实时展示保留的条数 (仅前端展示, 不影响全量统计/持久化).
/// 默认 1000, 可覆盖日均 ~500 请求约 2 天的窗口.
const LOG_CAPACITY: usize = 1000;

/// 面板「错误」独立保留条数 (按"错误"维度, 与最近请求窗口完全分离).
/// 正常请求再多也不会挤掉错误展示——错误从全量 inner 按错误维度过滤返回.
const ERROR_CAPACITY: usize = 100;

/// 实时统计 (供任务栏 tooltip 展示), 基于最近 N 秒日志聚合.
#[derive(Debug, Clone, Serialize)]
pub struct RealtimeStats {
    /// 最近 10 秒请求速度 (req/s).
    pub requests_per_second: f64,
    /// 最近 10 秒平均延迟 (ms).
    pub avg_latency_ms: f64,
    /// 最近 10 秒缓存命中率 (0.0~1.0).
    pub cache_hit_rate: f64,
    /// 最近 10 秒生成速度 (tok/s).
    pub gen_speed: f64,
    /// 今日总请求数.
    pub today_requests: usize,
    /// 当前时间戳 (秒).
    pub timestamp: u64,
}

/// 单条请求日志.
#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct RequestLog {
    pub timestamp: u64,
    pub model: String,
    pub provider: String,
    pub endpoint: String,
    pub status: u16,
    pub latency_ms: u64,
    pub body_len: usize,
    pub error: Option<String>,
    /// 输入 token 数 (从上游响应 usage 中提取).
    #[serde(default)]
    pub prompt_tokens: u32,
    /// 输出 token 数 (从上游响应 usage 中提取).
    #[serde(default)]
    pub completion_tokens: u32,
    /// 是否命中本地响应缓存 (命中则未真实请求上游, 省 token + 延迟).
    #[serde(default)]
    pub cached: bool,
    /// 上游 KV Cache 命中 token 数 (usage.prompt_cache_hit_tokens, DeepSeek 等).
    #[serde(default)]
    pub prompt_cache_hit_tokens: u32,
    /// 上游 KV Cache 未命中 token 数 (usage.prompt_cache_miss_tokens).
    #[serde(default)]
    pub prompt_cache_miss_tokens: u32,
    /// 上游 KV Cache 首次写入 (creation) token 数 (Anthropic `cache_creation_input_tokens`
    /// / OpenAI `prompt_tokens_details.cache_creation_tokens`). 多数供应商 (DeepSeek 等)
    /// 无独立 creation 口径, 记 0, 此时 creation 计入未命中按 input 价计.
    #[serde(default)]
    pub prompt_cache_creation_tokens: u32,
    /// 首 token 延迟 (ms): 从请求开始到上游吐出第一个增量内容的耗时.
    /// 用于计算"纯生成吐字速度" = 输出 token / (总耗时 - 首 token 延迟), 排除排队与 TTFT.
    /// 非流式/命中本地缓存的请求此项为 None.
    #[serde(default)]
    pub first_token_ms: Option<u64>,
    /// 上游真实模型 ID (由 providers.json 的 upstream_model 透传). 用于按"供应商/上游模型"聚合统计, 避免按中转 ID 统计造成的混乱.
    /// 路由未查到/解析失败等未知场景为 None, 统计时回退到中转 model.
    #[serde(default)]
    pub upstream_model: Option<String>,
    /// 转发优化省下的输入 token 数: 剥离历史推理链 (字符数/4 估算).
    /// 这些 token 在发往上游客前已被移除, 不计入 prompt_tokens; 单独记账供"优化省量"展示.
    #[serde(default)]
    pub strip_saved_tokens: u32,
    /// 转发优化省下的输入 token 数: 长会话历史裁剪 (字符数/4 估算).
    #[serde(default)]
    pub trim_saved_tokens: u32,
    /// 本地响应缓存命中省下的 token 数 (命中时本应消耗/计费的 prompt+completion).
    /// 仅 cached=true 时有值; 命中时 prompt_tokens 已是缓存体, 不与计费基数重复.
    #[serde(default)]
    pub resp_cache_saved_tokens: u32,
    /// 省量审计: 带 tool_calls 的 assistant 消息中被豁免剥离的推理链 token 估算.
    /// 只统计不改写, 用于评估放开豁免还能省多少.
    #[serde(default)]
    pub audit_exempt_reasoning_tokens: u32,
    /// 省量审计: 历史中字节级重复的大块内容可省略的 token 估算 (保留首次).
    #[serde(default)]
    pub audit_dup_block_tokens: u32,
    /// 省量审计: 上述重复块的个数.
    #[serde(default)]
    pub audit_dup_block_count: u32,
    /// 本次实际剥离的「带 tool_calls 推理链」token 数 (仅开启该开关的模型非零).
    #[serde(default)]
    pub strip_toolcall_saved_tokens: u32,
    /// 本条日志是否真的做过省量审计.
    /// 旧日志 (升级前) 与未经过请求体分析的路径 (响应缓存回放 / 原生直通) 为 false ——
    /// 此时上面三个 audit_* 字段是"未测量", 不是"测得为 0", 聚合时须区分开.
    #[serde(default)]
    pub audit_observed: bool,
    /// 价格快照: **记录本条日志时**该模型生效的单价 (元/百万 token).
    ///
    /// 请求记录是历史账单 —— 费用在写入时结算并随本条落盘, 之后改价 / 删除模型 /
    /// 改高峰表都不再影响它. 缺失 (旧日志) 时回退按当前配置现算, 与升级前行为一致;
    /// 该回退只对"升级前写入的记录"生效, 新记录一律带快照.
    ///
    /// 注意这里存的是**配置价**而非最终单价: 高峰/空闲由查询期按 `timestamp` 与
    /// 当前高峰表取值 —— 即改高峰表仍会重估历史 (与 rollup 模块文档所述语义一致).
    #[serde(default)]
    pub price: Option<crate::pricing::ModelPrice>,
    /// 本条日志的 token 是否为**估算/未上报** (上游没返回 usage).
    ///
    /// 上游未给 usage 时, 流式路径按已生成文本长度估算, 非流式路径则只能记 0 ——
    /// 两者都不是实测值. 若不标记, 面板上估算值与实测值长得一模一样, 会被当成精确数据读,
    /// 这正是"看似精确的假数"的来源 (实测踩过: 先按响应字节估出 50 万, 后改成纯思考记 0).
    /// 旧日志无此字段, 默认 false (升级前的值确实多数来自上游上报).
    #[serde(default)]
    pub usage_estimated: bool,
}

#[inline]
fn is_log_error(log: &RequestLog) -> bool {
    log.status >= 400 || log.error.is_some()
}

/// 内存日志缓冲区 — 内存仅保留最近 `LOG_CAPACITY` 条用于面板实时展示;
/// 文件 `logs.jsonl` 为全量权威数据源 (受 `store::MAX_LINES` 上限约束).
/// 统计/聚合一律基于 [`LogBuffer::drain_all`] 从文件加载的全量数据, 避免跨天/跨月数据被内存容量截断.
#[derive(Clone)]
pub struct LogBuffer {
    inner: Arc<Mutex<VecDeque<RequestLog>>>,
    /// 写入序列号: 每次 push/clear 递增, 供统计缓存做失效判断 (读端纳秒级, 无需克隆全量).
    seq: Arc<AtomicU64>,
    store: Option<LogStore>,
    /// 日级 rollup 账本 (跨日志滚动窗口的持久化统计, 供月视图等长跨度查询).
    rollup: Option<std::sync::Arc<crate::rollup::RollupBook>>,
    /// 路由表句柄: 仅用于在 push 时取"该模型此刻生效的价格"做历史账单快照.
    /// 用 `Weak` 而非 `Arc` —— 路由表由 AppState 持有, 日志缓冲区不该延长其生命周期
    /// (否则热重载后旧表仍被引用, 且易形成引用环).
    registry: Option<std::sync::Weak<tokio::sync::RwLock<crate::providers::ProviderRegistry>>>,
    // ─── 本轮 (进程启动以来) 累计计数, 在 push 时原子累加, 不受 5000 条滚动窗口封顶影响 ───
    /// 本轮请求总数 (含错误/缓存命中).
    session_requests: Arc<AtomicU64>,
    /// 本轮成功请求数.
    session_success: Arc<AtomicU64>,
    /// 本轮输入 token 累计.
    session_prompt_tokens: Arc<AtomicU64>,
    /// 本轮输出 token 累计.
    session_completion_tokens: Arc<AtomicU64>,
    /// 本轮上游 KV Cache 命中 token 累计.
    session_cache_hit_tokens: Arc<AtomicU64>,
    /// 本轮上游 KV Cache 未命中 token 累计.
    session_cache_miss_tokens: Arc<AtomicU64>,
}

impl LogBuffer {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(VecDeque::with_capacity(crate::store::MAX_LINES))),
            seq: Arc::new(AtomicU64::new(0)),
            store: None,
            rollup: None,
            registry: None,
            session_requests: Arc::new(AtomicU64::new(0)),
            session_success: Arc::new(AtomicU64::new(0)),
            session_prompt_tokens: Arc::new(AtomicU64::new(0)),
            session_completion_tokens: Arc::new(AtomicU64::new(0)),
            session_cache_hit_tokens: Arc::new(AtomicU64::new(0)),
            session_cache_miss_tokens: Arc::new(AtomicU64::new(0)),
        }
    }

    /// 当前写入序列号 (单调增). 统计缓存以 (seq, granularity, providers_mtime) 判断是否失效.
    pub fn seq(&self) -> u64 {
        self.seq.load(Ordering::Acquire)
    }

    /// 附加持久化存储 (启动时调用), 并从文件加载全量日志到内存.
    pub fn with_store(mut self, store: LogStore) -> Self {
        // 启动加载全量日志 (统计基于全量, 不受展示容量限制).
        let logs = store.load(usize::MAX);
        if !logs.is_empty() {
            if let Ok(mut buf) = self.inner.try_lock() {
                for log in logs {
                    if buf.len() >= crate::store::MAX_LINES {
                        buf.pop_front();
                    }
                    buf.push_back(log);
                }
            }
        }
        self.store = Some(store);
        self
    }

    /// 附加路由表句柄 (启动时调用): 供 `push` 取"该模型此刻生效的价格"写成历史账单快照.
    ///
    /// 只存 `Weak` —— 见字段注释. 未附加时 `push` 不做快照, 行为退回"查询期按当前配置现算".
    pub fn with_registry(
        mut self,
        registry: &std::sync::Arc<tokio::sync::RwLock<crate::providers::ProviderRegistry>>,
    ) -> Self {
        self.registry = Some(std::sync::Arc::downgrade(registry));
        self
    }

    /// 取该中转 ID 此刻生效的配置价 (路由表的 model 配置即本次请求实际使用的价格).
    ///
    /// 刻意用**非阻塞** `try_read` 而非 `read().await`: `push` 由请求处理路径调用, 而
    /// 那些路径可能正持有 registry 读锁 (路由查找), 此时若有写者 (面板保存配置) 在排队,
    /// tokio 的写优先策略会阻塞新的读者 —— 同任务内二次取读锁即死锁, 整个网关卡死.
    /// 取不到就返回 None (该条退回"按当前配置现算"的旧行为), 配置写者极短暂, 影响可忽略.
    async fn snapshot_price(&self, model: &str) -> Option<crate::pricing::ModelPrice> {
        let weak = self.registry.as_ref()?;
        let registry = weak.upgrade()?;
        let guard = match registry.try_read() {
            Ok(g) => g,
            Err(_) => {
                warn!("log price snapshot skipped (registry busy): model={model}");
                return None;
            }
        };
        guard.price_of(model)
    }

    /// 附加日级 rollup 账本 (启动时调用, 须在 [`Self::with_store`] 之后 —
    /// 回填依赖已加载到内存的全量日志).
    pub fn with_rollup(mut self, book: crate::rollup::RollupBook) -> Self {
        book.load_from_file();
        // 启动回填: 日志完整覆盖的天以日志为准重建 (修正崩溃丢失的增量);
        // 首次启用 (账本为空) 时连边界天一起尽力回填.
        let logs: Vec<RequestLog> = {
            if let Ok(buf) = self.inner.try_lock() {
                buf.iter().cloned().collect()
            } else {
                Vec::new()
            }
        };
        book.backfill_from_logs(&logs);
        book.flush_blocking();
        self.rollup = Some(std::sync::Arc::new(book));
        self
    }

    /// rollup 账本句柄 (统计查询合并历史用).
    pub fn rollup(&self) -> Option<&std::sync::Arc<crate::rollup::RollupBook>> {
        self.rollup.as_ref()
    }

    /// 内存中最早日志的时间戳 (缓冲区为空时返回 None).
    pub async fn oldest_ts(&self) -> Option<u64> {
        let buf = self.inner.lock().await;
        buf.iter().map(|l| l.timestamp).min()
    }

    /// 同步落盘 rollup 账本 (关机时调用).
    pub fn rollup_flush_blocking(&self) {
        if let Some(book) = &self.rollup {
            book.flush_blocking();
        }
    }

    /// 清空 rollup 账本并删除落盘文件 (与清空日志联动).
    pub async fn rollup_clear(&self) {
        if let Some(book) = &self.rollup {
            book.clear().await;
        }
    }

    pub async fn push(&self, mut log: RequestLog) {
        // 不变式: 命中本地响应缓存的请求未真实调用上游, 不产生任何上游 token 消耗.
        // 统一在此清零 (而非依赖各调用点), 否则这些 token 会被重复计入总量与命中率分母,
        // 也与 resp_cache_saved_tokens(省量口径) 重复; 并发去重时同一请求还会被 N 个等待者各记一遍.
        // 省下的量仍由 resp_cache_saved_tokens 单独记账, 面板"优化省量"不受影响.
        if log.cached {
            log.prompt_tokens = 0;
            log.completion_tokens = 0;
            log.prompt_cache_hit_tokens = 0;
            log.prompt_cache_miss_tokens = 0;
            log.prompt_cache_creation_tokens = 0;
        }
        // 历史账单结算: 把"此刻该模型生效的价格"随日志一起落盘.
        // 否则费用是查询期按当前配置现算的 —— 改价会改写历史金额, 删掉模型更会让
        // 历史费用直接归零 (请求记录应当是已成事实的账单, 不是按现价的重估).
        // 已有快照则不覆盖 (记录路径重放/测试构造的日志自带价格).
        if log.price.is_none() {
            log.price = self.snapshot_price(&log.model).await;
        }
        let mut buf = self.inner.lock().await;
        if buf.len() >= crate::store::MAX_LINES {
            buf.pop_front();
        }
        buf.push_back(log.clone());
        // 写入序列号递增 (缓存失效信号).
        self.seq.fetch_add(1, Ordering::Release);
        // 本轮 (进程级) 累计计数: 独立于滚动窗口, 重启清零.
        self.session_requests.fetch_add(1, Ordering::Relaxed);
        if !is_log_error(&log) {
            self.session_success.fetch_add(1, Ordering::Relaxed);
        }
        self.session_prompt_tokens
            .fetch_add(log.prompt_tokens as u64, Ordering::Relaxed);
        self.session_completion_tokens
            .fetch_add(log.completion_tokens as u64, Ordering::Relaxed);
        self.session_cache_hit_tokens
            .fetch_add(log.prompt_cache_hit_tokens as u64, Ordering::Relaxed);
        self.session_cache_miss_tokens
            .fetch_add(log.prompt_cache_miss_tokens as u64, Ordering::Relaxed);
        // 日级 rollup 实时累加 (跨滚动窗口的持久化统计), 节流触发异步落盘.
        if let Some(book) = &self.rollup {
            book.record(&log);
            if book.should_flush() {
                let book = std::sync::Arc::clone(book);
                tokio::spawn(async move { book.flush().await });
            }
        }
        // 异步持久化 (文件为全量权威源, 不受内存容量限制).
        if let Some(store) = &self.store {
            let store = store.clone();
            tokio::spawn(async move {
                store.append(&log).await;
            });
        }
    }

    /// 本轮 (进程启动以来) 累计统计快照, 不受日志滚动窗口封顶影响.
    pub fn session_stats(&self) -> SessionStats {
        SessionStats {
            requests: self.session_requests.load(Ordering::Relaxed),
            success: self.session_success.load(Ordering::Relaxed),
            prompt_tokens: self.session_prompt_tokens.load(Ordering::Relaxed),
            completion_tokens: self.session_completion_tokens.load(Ordering::Relaxed),
            cache_hit_tokens: self.session_cache_hit_tokens.load(Ordering::Relaxed),
            cache_miss_tokens: self.session_cache_miss_tokens.load(Ordering::Relaxed),
        }
    }

    /// 返回全量日志 (用于统计/聚合), 基于内存中加载的全量数据, 不消费.
    pub async fn drain_all(&self) -> Vec<RequestLog> {
        let buf = self.inner.lock().await;
        buf.iter().cloned().collect()
    }

    /// 只读遍历 (不克隆全量): 持锁期间对每个日志调用 `f`, 用于实时聚合等只需流式消费的场景.
    /// 相较 `drain_all` 省去一次 5000 条深拷贝 (含多个 String 字段) 的堆分配,
    /// 且锁持有时间仅覆盖遍历本身, 不覆盖后续聚合, 降低对写入路径 (proxy 响应链路) 的阻塞.
    pub fn for_each_recent<F>(&self, start_ts: u64, mut f: F)
    where
        F: FnMut(&RequestLog),
    {
        if let Ok(buf) = self.inner.try_lock() {
            for log in buf.iter() {
                if log.timestamp >= start_ts {
                    f(log);
                }
            }
        }
    }

    /// 将当前内存全量日志同步重写到文件 (退出/清空前调用, 确保尾写不丢).
    pub async fn flush(&self) {
        if let Some(store) = &self.store {
            let store = store.clone();
            let logs = self.drain_all().await;
            store.rewrite(&logs).await;
        }
    }

    /// 同步刷新全量日志到磁盘 (事件循环/退出等非 async 上下文使用, 阻塞当前线程).
    pub fn flush_blocking(&self) {
        if let Some(store) = &self.store {
            // 同步取出全量 (Mutex 在同步上下文锁定).
            let logs = {
                match self.inner.try_lock() {
                    Ok(buf) => buf.iter().cloned().collect::<Vec<_>>(),
                    Err(_) => return,
                }
            };
            store.rewrite_blocking(&logs);
        }
    }

    /// 取最近 N 条 (用于前端展示, 不消费).
    pub async fn recent(&self, n: usize) -> Vec<RequestLog> {
        let buf = self.inner.lock().await;
        buf.iter().rev().take(n).cloned().collect()
    }

    /// 取最近 N 条**错误**日志 (按"错误"维度独立留存, 不受 `recent` 请求窗口冲刷).
    /// 错误判定: status>=400 或 error 字段非空. 内存 inner 保留全量(MAX_LINES),
    /// 故错误数量与"最近 N 条请求"完全解耦——正常请求再多也不会挤掉错误展示.
    pub async fn recent_errors(&self, n: usize) -> Vec<RequestLog> {
        let buf = self.inner.lock().await;
        buf.iter()
            .rev()
            .filter(|l| l.status >= 400 || l.error.is_some())
            .take(n)
            .cloned()
            .collect()
    }

    /// 当前内存中日志条数.
    pub async fn len(&self) -> usize {
        self.inner.lock().await.len()
    }

    /// 清空缓冲区并重写持久化文件.
    pub async fn clear(&self) {
        let mut buf = self.inner.lock().await;
        buf.clear();
        // 清空也是数据变更, 递增序列号使统计缓存失效.
        self.seq.fetch_add(1, Ordering::Release);
        if let Some(store) = &self.store {
            let store = store.clone();
            tokio::spawn(async move {
                store.rewrite(&[]).await;
            });
        }
    }
}

/// 获取当前 Unix 时间戳 (秒).
pub fn now_ts() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

/// 记录一次请求结果到日志缓冲区 (供 proxy.rs 调用).
pub async fn record_request(
    log_buffer: &LogBuffer,
    model: &str,
    provider: &str,
    endpoint: &str,
    upstream_model: Option<&str>,
    status: u16,
    start: Instant,
    body_len: usize,
    error: Option<String>,
) {
    let log = RequestLog {
        timestamp: now_ts(),
        model: model.to_string(),
        provider: provider.to_string(),
        endpoint: endpoint.to_string(),
        status,
        latency_ms: start.elapsed().as_millis() as u64,
        body_len,
        error,
        prompt_tokens: 0,
        completion_tokens: 0,
        cached: false,
        prompt_cache_hit_tokens: 0,
        prompt_cache_miss_tokens: 0,
        prompt_cache_creation_tokens: 0,
        strip_saved_tokens: 0,
        trim_saved_tokens: 0,
        resp_cache_saved_tokens: 0,
        strip_toolcall_saved_tokens: 0,
        audit_exempt_reasoning_tokens: 0,
        audit_dup_block_tokens: 0,
        audit_dup_block_count: 0,
        audit_observed: false,
        price: None,
        first_token_ms: None,
        upstream_model: upstream_model.map(|s| s.to_string()),
        // 失败路径不解析 usage, 记的 0 是"未上报"而非"实测为 0".
        usage_estimated: true,
    };
    log_buffer.push(log).await;
}

/// 带 token 统计的日志记录 (成功响应用).
///
/// `cached` 标记该响应是否来自本地缓存命中 (命中时未真实请求上游).
pub async fn record_request_with_tokens(
    log_buffer: &LogBuffer,
    model: &str,
    provider: &str,
    endpoint: &str,
    upstream_model: Option<&str>,
    start: Instant,
    prompt_tokens: u32,
    completion_tokens: u32,
    response_body_len: usize,
    cached: bool,
    cache_hit_tokens: u32,
    cache_miss_tokens: u32,
    cache_creation_tokens: u32,
    strip_saved_tokens: u32,
    trim_saved_tokens: u32,
    resp_cache_saved_tokens: u32,
    audit: Option<crate::proxy::TokenAudit>,
    first_token_ms: Option<u64>,
    error: Option<String>,
    usage_estimated: bool,
) {
    record_request_with_tokens_status(
        log_buffer,
        model,
        provider,
        endpoint,
        upstream_model,
        200,
        start,
        prompt_tokens,
        completion_tokens,
        response_body_len,
        cached,
        cache_hit_tokens,
        cache_miss_tokens,
        cache_creation_tokens,
        strip_saved_tokens,
        trim_saved_tokens,
        resp_cache_saved_tokens,
        audit,
        first_token_ms,
        error,
        usage_estimated,
    )
    .await;
}

/// 同 [`record_request_with_tokens`], 但可指定 HTTP 状态码.
///
/// 用于流中途失败等场景: 原先一律记 200 且清零 token, 导致已生成用量与真实
/// 失败状态双双丢失 (面板看起来是成功请求).
#[allow(clippy::too_many_arguments)]
pub async fn record_request_with_tokens_status(
    log_buffer: &LogBuffer,
    model: &str,
    provider: &str,
    endpoint: &str,
    upstream_model: Option<&str>,
    status: u16,
    start: Instant,
    prompt_tokens: u32,
    completion_tokens: u32,
    response_body_len: usize,
    cached: bool,
    cache_hit_tokens: u32,
    cache_miss_tokens: u32,
    cache_creation_tokens: u32,
    strip_saved_tokens: u32,
    trim_saved_tokens: u32,
    resp_cache_saved_tokens: u32,
    audit: Option<crate::proxy::TokenAudit>,
    first_token_ms: Option<u64>,
    error: Option<String>,
    usage_estimated: bool,
) {
    let audit_is_observed = audit.is_some();
    let audit = audit.unwrap_or_default();
    let log = RequestLog {
        timestamp: now_ts(),
        model: model.to_string(),
        provider: provider.to_string(),
        endpoint: endpoint.to_string(),
        status,
        latency_ms: start.elapsed().as_millis() as u64,
        body_len: response_body_len,
        error,
        prompt_tokens,
        completion_tokens,
        cached,
        prompt_cache_hit_tokens: cache_hit_tokens,
        prompt_cache_miss_tokens: cache_miss_tokens,
        prompt_cache_creation_tokens: cache_creation_tokens,
        strip_saved_tokens,
        trim_saved_tokens,
        resp_cache_saved_tokens,
        strip_toolcall_saved_tokens: audit.strip_toolcall_saved_tokens,
        audit_exempt_reasoning_tokens: audit.exempt_reasoning_tokens,
        audit_dup_block_tokens: audit.dup_block_tokens,
        audit_dup_block_count: audit.dup_block_count,
        audit_observed: audit_is_observed,
        price: None,
        first_token_ms,
        upstream_model: upstream_model.map(|s| s.to_string()),
        usage_estimated,
    };
    log_buffer.push(log).await;
}

// ─── API 路由 ───

/// GET /admin — 返回管理面板 HTML.
///
/// 将 API 鉴权令牌注入页面 (仅同源 WebView 可见), 使本地面板能带令牌调用受保护的
/// /admin/api/* 接口; 未配置 AIGATE_ADMIN_TOKEN 时注入 `null`, 不鉴权.
/// 响应强制 `no-store`: wry/WebView2 默认持久的 HTTP 缓存会把旧版页面 (含旧版本号)
/// 在重启后继续命中, 造成"新版面上还显示旧版本"的假象.
pub async fn admin_page(State(state): State<super::proxy::AppState>) -> Response {
    let token_json = serde_json::to_string(&state.admin_token).unwrap_or_else(|_| "null".to_string());
    let html = ADMIN_HTML
        .replace("/*__AIGATE_TOKEN__*/", &format!("window.AIGATE_TOKEN = {token_json};"))
        .replace(
            "/*__AIGATE_LANG__*/",
            &format!("window.AIGATE_LANG = \"{}\";", crate::i18n::lang_code()),
        )
        .replace(
            "/*__AIGATE_VERSION__*/",
            &format!("window.AIGATE_VERSION = {};", crate::version::to_json()),
        );
    let mut resp = Html(html).into_response();
    resp.headers_mut().insert(
        HeaderName::from_static("cache-control"),
        HeaderValue::from_static("no-store, no-cache, must-revalidate"),
    );
    resp.headers_mut().insert(
        HeaderName::from_static("expires"),
        HeaderValue::from_static("0"),
    );
    resp
}

/// GET /admin/static/:file — 返回随包内联的前端依赖 (Alpine.js / Tailwind),
/// 走本地服务而非境外 CDN, 解决国内打不开 jsdelivr/tailwindcss 导致的面板黑屏.
/// `:file` 仅允许白名单文件名, 杜绝路径穿越.
pub async fn admin_static(Path(file): Path<String>) -> impl IntoResponse {
    let body: &'static str = match file.as_str() {
        "alpine.min.js" => ALPINE_JS,
        "tailwind.js" => TAILWIND_JS,
        _ => {
            return (
                StatusCode::NOT_FOUND,
                [(axum::http::header::CONTENT_TYPE, "text/plain; charset=utf-8")],
                "not found",
            )
                .into_response();
        }
    };
    (
        [(axum::http::header::CONTENT_TYPE, "application/javascript; charset=utf-8")],
        Bytes::from_static(body.as_bytes()),
    )
        .into_response()
}

/// 管理面板前端页面 (编译时嵌入).
const ADMIN_HTML: &str = include_str!("admin.html");
/// 更新日志 (Keep a Changelog 风格), 烤入二进制供关于页展示.
const CHANGELOG: &str = include_str!("../CHANGELOG.md");
/// Alpine.js (随包内联, 替代 cdn.jsdelivr.net) — 面板渲染引擎, 缺失会导致整页不渲染(黑屏).
const ALPINE_JS: &str = include_str!("admin_static/alpine.min.js");
/// Tailwind 运行时 JIT (随包内联, 替代 cdn.tailwindcss.com) — 负责工具类样式生成.
const TAILWIND_JS: &str = include_str!("admin_static/tailwind.js");

/// 给展示用日志附加 `free` / `reasoning_effort` 标记 (免费模型 / 思考强度配置),
/// 判定与路由配置页完全一致 (注册表配置), 不污染磁盘持久化的 RequestLog 结构,
/// 仅在 API 响应层注入.
fn request_logs_with_enrichment(
    logs: Vec<RequestLog>,
    free_ids: &std::collections::HashSet<String>,
    reasoning_map: &std::collections::HashMap<String, Option<String>>,
    price_table: &PriceTable,
) -> Vec<serde_json::Value> {
    let memo = PriceMemo::new(price_table);
    logs.into_iter()
        .map(|l| {
            let mut v = serde_json::to_value(&l).unwrap_or(serde_json::Value::Null);
            if let Some(obj) = v.as_object_mut() {
                obj.insert("free".into(), serde_json::Value::Bool(free_ids.contains(&l.model)));
                obj.insert(
                    "reasoning_effort".into(),
                    serde_json::Value::String(
                        reasoning_map.get(&l.model).and_then(|r| r.clone()).unwrap_or_default(),
                    ),
                );
                // 单条费用与省量: 前端请求详情要展示"这次花了多少 / 省了多少",
                // 不能让前端自己算 (价格表在后端, 且需与统计页同口径).
                let cost = log_cost(&memo, &l);
                obj.insert("cost".into(), serde_json::json!(cost));
                let cache_saved = log_cache_saved(&memo, &l);
                obj.insert("cache_saved".into(), serde_json::json!(cache_saved));
                // 优化省量折算费用 (剥离推理链 + 历史裁剪 + 响应缓存命中).
                let opt_tokens = (l.strip_saved_tokens as u64)
                    + (l.trim_saved_tokens as u64)
                    + (l.resp_cache_saved_tokens as u64);
                let opt_fee = match memo.resolve(&l) {
                    Some(p) if opt_tokens > 0 => saved_tokens_fee(
                        p,
                        l.timestamp,
                        opt_tokens,
                        l.prompt_tokens as u64,
                        l.prompt_cache_hit_tokens as u64,
                    ),
                    _ => 0.0,
                };
                obj.insert("opt_saved_fee".into(), serde_json::json!(opt_fee));
                // 省量来源拆分 (tokens): 前端据此列出"省了哪些类型".
                // 注意: strip_saved_tokens 已包含 tool_calls 轮次的部分, 故 "strip" 须扣除,
                // 否则前端合计会把它重复计入 (1009 显示成 2018).
                obj.insert(
                    "saved_breakdown".into(),
                    serde_json::json!({
                        "strip": l
                            .strip_saved_tokens
                            .saturating_sub(l.strip_toolcall_saved_tokens),
                        "strip_toolcall": l.strip_toolcall_saved_tokens,
                        "trim": l.trim_saved_tokens,
                        "resp_cache": l.resp_cache_saved_tokens,
                    }),
                );
            }
            v
        })
        .collect()
}

/// GET /admin/api/logs — 返回最近 100 条请求日志 (展示用, 内存限长), 附带免费/思考强度标记.
pub async fn api_logs(
    State(state): State<super::proxy::AppState>,
) -> Json<Vec<serde_json::Value>> {
    let logs = state.log_buffer.recent(LOG_CAPACITY).await;
    let registry = state.registry.read().await;
    let mut free_ids: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut reasoning_map: std::collections::HashMap<String, Option<String>> =
        std::collections::HashMap::new();
    for provider in registry.providers() {
        for (model_id, mcfg) in provider.models {
            if mcfg.is_free(&model_id) {
                free_ids.insert(model_id.to_string());
            }
            reasoning_map.insert(model_id.clone(), mcfg.reasoning_effort.clone());
        }
    }
    let price_table = PriceTable::from_registry(&registry.providers());
    drop(registry);
    Json(request_logs_with_enrichment(logs, &free_ids, &reasoning_map, &price_table))
}

/// DELETE /admin/api/logs — 清空日志缓冲区.
pub async fn api_logs_delete(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let count = state.log_buffer.len().await;
    // 清空内存展示缓冲与持久化文件. 删除前先同步落盘已有数据, 避免异步尾写丢失.
    state.log_buffer.flush().await;
    state.log_buffer.clear().await;
    state.log_buffer.rollup_clear().await;
    Json(serde_json::json!({ "message": crate::i18n::msg_logs_cleared(count) }))
}

/// GET /admin/api/errors — 返回最近 N 条错误日志 (独立维度, 与请求窗口分离, 不被冲刷), 附带免费标记.
pub async fn api_errors(
    State(state): State<super::proxy::AppState>,
) -> Json<Vec<serde_json::Value>> {
    let logs = state.log_buffer.recent_errors(ERROR_CAPACITY).await;
    let registry = state.registry.read().await;
    let mut free_ids: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut reasoning_map: std::collections::HashMap<String, Option<String>> =
        std::collections::HashMap::new();
    for provider in registry.providers() {
        for (model_id, mcfg) in provider.models {
            if mcfg.is_free(&model_id) {
                free_ids.insert(model_id.to_string());
            }
            reasoning_map.insert(model_id.clone(), mcfg.reasoning_effort.clone());
        }
    }
    let price_table = PriceTable::from_registry(&registry.providers());
    drop(registry);
    Json(request_logs_with_enrichment(logs, &free_ids, &reasoning_map, &price_table))
}

/// 路由配置的脱敏视图.
#[derive(Serialize)]
pub struct RouteInfo {
    model_id: String,
    provider: String,
    endpoint: String,
    /// 该模型实际转发的端点路径: 与 proxy.rs 运行时一致,
    /// 模型级 api_format=anthropic 且供应商端点为 /chat/completions 时改写为 /messages.
    api_format: String,
    upstream_model: Option<String>,
    reasoning_effort: Option<String>,
    /// 是否免费模型 (显式 free 标记或 upstream_model 含 free/免费).
    free: bool,
}

/// GET /admin/api/routes — 返回当前路由配置.
pub async fn api_routes(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let registry = state.registry.read().await;
    let provider_names: Vec<String> = registry
        .providers()
        .iter()
        .map(|p| p.name.clone())
        .collect();
    let routes: Vec<RouteInfo> = registry
        .model_ids()
        .into_iter()
        .filter_map(|id| {
            let entry = registry.lookup(id)?;
            // 复刻 proxy.rs 运行时的端点改写逻辑: 模型级 api_format=anthropic
            // 且供应商端点仍是 /chat/completions 时, 改写为 /messages 或 /responses.
            let mut endpoint = entry.provider.endpoint.clone();
            let anthropic_mode = entry.model.is_anthropic(&entry.provider);
            let responses_mode = entry.model.is_responses(&entry.provider);
            if anthropic_mode {
                if let Some(ep) = &entry.provider.endpoint_anthropic {
                    if !ep.trim().is_empty() {
                        endpoint = ep.clone();
                    }
                } else if endpoint.ends_with("/chat/completions") {
                    endpoint = endpoint.replace("/chat/completions", "/messages");
                }
            } else if responses_mode {
                if let Some(ep) = &entry.provider.endpoint_responses {
                    if !ep.trim().is_empty() {
                        endpoint = ep.clone();
                    }
                } else if endpoint.ends_with("/chat/completions") {
                    endpoint = endpoint.replace("/chat/completions", "/responses");
                }
            }
            let api_format = if anthropic_mode { "anthropic" } else if responses_mode { "responses" } else { "openai" }.to_string();
            Some(RouteInfo {
                model_id: id.to_string(),
                provider: entry.provider.name.clone(),
                endpoint,
                api_format,
                upstream_model: entry.model.upstream_model.clone(),
                reasoning_effort: entry.model.reasoning_effort.clone(),
                free: entry.model.is_free(id),
            })
        })
        .collect();
    drop(registry);
    Json(serde_json::json!({
        "providers": provider_names,
        "model_count": routes.len(),
        "routes": routes,
    }))
}

/// GET /admin/api/providers — 返回 providers.json 的原始 JSON (用于编辑器).
pub async fn api_providers_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let registry = state.registry.read().await;
    match registry.to_json() {
        Ok(json_str) => {
            let parsed: serde_json::Value =
                serde_json::from_str(&json_str).unwrap_or(serde_json::Value::Null);
            Json(serde_json::json!({ "json": json_str, "parsed": parsed }))
        }
        Err(e) => Json(serde_json::json!({ "error": e })),
    }
}

/// POST /admin/api/providers/save — 保存 providers.json 并热重载.
pub async fn api_providers_save(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    let json_str = match payload.get("json").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => return Json(serde_json::json!({ "error": crate::i18n::msg_missing_json_field() })),
    };

    // 1) 写前校验: 解析 + 结构化语义校验, 防止坏配置覆盖落盘 (包含 name 唯一/非空, endpoint 非空).
    //    重复中转 ID 只告警不阻断 —— 存量配置本就有历史重复, 硬拦会让用户连无关字段都存不下去.
    let (new_names, dup_warnings) = match validate_providers_json(json_str) {
        Ok(v) => v,
        Err(e) => return Json(serde_json::json!({ "error": e })),
    };

    // 2) 备份当前文件 (覆盖写盘前保留上一版, 便于回滚).
    let _ = std::fs::copy("providers.json", "providers.json.bak");

    // 3) 取旧供应商名 (用于重命名迁移 + 级联清理孤儿 key).
    let old_names: Vec<String> = state
        .registry
        .read()
        .await
        .providers()
        .iter()
        .map(|p| p.name.clone())
        .collect();

    // 3.1) 前端传入 oldNames (与 providersFormData 位置对齐), 用于检测重命名.
    let frontend_old_names: Vec<String> = payload
        .get("oldNames")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();

    // 4) 原子写盘: 临时文件 + rename, 避免写入中断留下半截文件.
    let tmp = "providers.json.tmp";
    if let Err(e) = std::fs::write(tmp, json_str) {
        return Json(serde_json::json!({ "error": crate::i18n::msg_write_file_failed(&e) }));
    }
    if let Err(e) = std::fs::rename(tmp, "providers.json") {
        return Json(serde_json::json!({ "error": crate::i18n::msg_write_file_failed(&e) }));
    }

    // 5) 热重载
    {
        let mut registry = state.registry.write().await;
        if let Err(e) = registry.reload("providers.json") {
            return Json(serde_json::json!({ "error": e }));
        }
    }
    sync_breakers_for(&state).await;

    // 6) 重命名迁移: 前端 oldNames[i] → new_names[i] 按位置对齐, 若不同则迁移 API key.
    if !frontend_old_names.is_empty() {
        for (i, new_name) in new_names.iter().enumerate() {
            if let Some(old_name) = frontend_old_names.get(i) {
                if old_name != new_name {
                    // 迁移 key: 读旧名 → 写新名 → 删旧名
                    if let Some(key_val) = state.key_store.get_for_provider(old_name).await {
                        let _ = state
                            .key_store
                            .set_for_provider(new_name, &key_val)
                            .await;
                        let _ = state.key_store.remove_for_provider(old_name).await;
                    }
                }
            }
        }
    }

    // 7) 级联清理: 删除已从配置中移除的供应商的密钥 (包含关系: key 随 provider 消失).
    //    排除已在步骤 6 中迁移过的旧名 (已删除, 不需再清理).
    let removed: Vec<String> = old_names
        .iter()
        .filter(|n| !new_names.contains(n))
        .cloned()
        .collect();
    if !removed.is_empty() {
        let _ = state.key_store.remove_many(&removed).await;
    }

    Json(serde_json::json!({
        "message": crate::i18n::msg_config_saved(),
        // 重复中转 ID 告警 (非阻断): 前端据此提示哪些条目改了不会生效.
        "duplicate_ids": dup_warnings,
    }))
}

/// 校验 providers.json 文本: 解析为 {providers:[...]}, 每个供应商 name 非空且唯一, endpoint 非空.
///
/// 另**检测**跨供应商的模型中转 ID 重复 (不阻断保存): 路由表是 `HashMap<中转ID, 路由>`,
/// 同一 ID 只能有一条路由, 后加载者会静默覆盖前者 —— 被覆盖的条目在面板上可见可改却永不
/// 生效 (改协议/思考档位/价格都没反应), 且原先无任何提示. 这里返回告警供前端展示.
///
/// **为什么不直接拒绝保存**: 存量配置里已存在大量历史重复 (纯上游命名习惯遗留), 一旦
/// 硬性拦截, 用户连"改个无关字段"都存不下去, 只能被迫大改配置 —— 那是把体验绑死。
/// 新拉取的模型已按「供应商/上游ID」生成, 不会新增冲突; 存量由用户按提示自行清理。
///
/// 返回 `(供应商名列表, 重复中转 ID 告警列表)`; 前者供调用方做孤儿 key 清理.
fn validate_providers_json(json_str: &str) -> Result<(Vec<String>, Vec<String>), String> {
    let v: serde_json::Value = serde_json::from_str(json_str)
        .map_err(|e| format!("JSON 解析失败: {e}"))?;
    let arr = v
        .get("providers")
        .and_then(|p| p.as_array())
        .ok_or_else(|| "providers.json 缺少顶层 providers 数组".to_string())?;
    let mut names: Vec<String> = Vec::with_capacity(arr.len());
    // 中转 ID -> 首次出现的供应商名 (用于报告冲突双方)
    let mut owner: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    let mut dup_warnings: Vec<String> = Vec::new();
    for (i, p) in arr.iter().enumerate() {
        let name = p
            .get("name")
            .and_then(|n| n.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| format!("第 {i} 个供应商 name 为空 (必填)"))?;
        if names.iter().any(|n| n == name) {
            return Err(format!("供应商 name 重复: '{name}'"));
        }
        let endpoint = p
            .get("endpoint")
            .and_then(|e| e.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| format!("供应商 '{name}' 的 endpoint 为空 (必填)"))?;
        let _ = endpoint;
        if let Some(models) = p.get("models").and_then(|m| m.as_object()) {
            for mid in models.keys() {
                match owner.get(mid) {
                    Some(prev) => dup_warnings.push(format!(
                        "中转 ID '{mid}' 同时属于 '{prev}' 与 '{name}' —— 只有后者生效, \
                         '{prev}' 那条改了不会有任何效果; 建议改成 '{name}/{mid}' 形式区分"
                    )),
                    None => {
                        owner.insert(mid.clone(), name.to_string());
                    }
                }
            }
        }
        names.push(name.to_string());
    }
    Ok((names, dup_warnings))
}

/// 配置热重载后, 按当前熔断阈值同步熔断表 (新增供应商补齐, 删除的清理).
async fn sync_breakers_for(state: &super::proxy::AppState) {
    let names: Vec<String> = state
        .registry
        .read()
        .await
        .providers()
        .iter()
        .map(|p| p.name.clone())
        .collect();
    super::proxy::sync_breakers(&state.breakers, &names, &state.breaker);
}

/// POST /admin/api/providers/reload — 从磁盘重读 providers.json.
pub async fn api_providers_reload(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    {
        let mut registry = state.registry.write().await;
        if let Err(e) = registry.reload("providers.json") {
            return Json(serde_json::json!({ "error": e }));
        }
    }
    sync_breakers_for(&state).await;
    Json(serde_json::json!({ "message": crate::i18n::msg_config_reloaded() }))
}

/// GET /admin/api/keys — 返回所有供应商的密钥脱敏视图 (包含关系: 每行对应一个 provider).
pub async fn api_keys_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    let registry = state.registry.read().await;
    let providers = registry.providers();
    let views = state.key_store.masked_view_for_providers(&providers).await;
    drop(registry);
    Json(serde_json::json!({ "providers": views }))
}

/// PUT /admin/api/keys — 更新某供应商的密钥 (Key 是 provider 的子资源).
#[derive(serde::Deserialize)]
pub struct KeyUpdate {
    /// 归属的供应商名.
    pub provider: String,
    pub value: String,
}

pub async fn api_keys_put(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<KeyUpdate>,
) -> Json<serde_json::Value> {
    match state
        .key_store
        .set_for_provider(&payload.provider, &payload.value)
        .await
    {
        Ok(()) => {
            if payload.value.is_empty() {
                Json(serde_json::json!({ "message": crate::i18n::msg_key_cleared(&payload.provider) }))
            } else {
                Json(serde_json::json!({ "message": crate::i18n::msg_key_updated(&payload.provider) }))
            }
        }
        Err(e) => Json(serde_json::json!({ "error": e })),
    }
}

/// 健康检查结果.
#[derive(Serialize)]
pub struct HealthEntry {
    provider: String,
    endpoint: String,
    /// 健康等级: ok / error (供面板 CSS 配色).
    status_level: String,
    /// 健康状态中文文案 (供面板展示).
    status_text: String,
    latency_ms: u64,
    /// 熔断状态原始值: closed / open / half-open (供面板 CSS 配色).
    circuit: String,
    /// 熔断状态中文文案 (供面板展示).
    circuit_text: String,
}

/// POST /admin/api/providers/test — 测试单个供应商连通性.
#[derive(serde::Deserialize)]
pub struct TestProviderReq {
    endpoint: String,
}

pub async fn api_providers_test(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<TestProviderReq>,
) -> Json<serde_json::Value> {
    let start = std::time::Instant::now();
    let base_url = payload.endpoint.replace("/chat/completions", "/models");
    let status = match state
        .client
        .get(&base_url)
        .header("Authorization", "Bearer probe")
        .timeout(Duration::from_secs(8))
        .send()
        .await
    {
        Ok(resp) => {
            let code = resp.status().as_u16();
            if code == 401 || code == 403 {
                "ok".to_string()
            } else if code < 500 {
                "ok".to_string()
            } else {
                format!("error {code}")
            }
        }
        Err(e) => format!("unreachable: {e}"),
    };
    let latency_ms = start.elapsed().as_millis() as u64;
    Json(serde_json::json!({
        "success": status == "ok",
        "status": status,
        "latency_ms": latency_ms,
    }))
}

/// POST /admin/api/providers/:name/fetch-models
/// 从上游 `/v1/models` 拉取模型列表, 合并进 provider 并持久化到 providers.json.
///
/// 拉取到的模型 `reasoning_effort` 留空, 由客户端 (opencode / CodeBuddy 等) 自行调节思考档位.
/// 已存在的模型 ID 不会被覆盖 (保留用户自定义的 upstream_model / reasoning_effort).
pub async fn api_providers_fetch_models(
    State(state): State<super::proxy::AppState>,
    Path(name): Path<String>,
) -> Json<serde_json::Value> {
    // 1) 取 provider 配置 + 真实 key (只读锁, 取完即释放, 不跨 await 持有)
    let (provider, key) = {
        let registry = state.registry.read().await;
        let provider = match registry.providers().into_iter().find(|p| p.name == name) {
            Some(p) => p,
            None => return Json(serde_json::json!({ "error": crate::i18n::msg_provider_not_found(&name) })),
        };
        let key = match registry.api_key(&provider, &state.key_store).await {
            Ok(k) => k,
            Err(e) => return Json(serde_json::json!({ "error": e })),
        };
        (provider, key)
    };

    // 2) 向上游拉取模型 (用真实 key 鉴权)
    let ids = match crate::providers::fetch_models_from_upstream(&state.client, &provider, &key).await
    {
        Ok(ids) => ids,
        Err(e) => return Json(serde_json::json!({ "error": e })),
    };

    // 3) 合并进内存注册表 (新增未存在的, 跳过已有) + 计算已下架.
    //    下架判定必须基于【上游模型ID】(upstream_model, 缺省回落中转ID):
    //    中转ID 是用户自取别名可随时改名, 不能作为与上游清单比对的依据 ——
    //    否则一改中转ID 就会被误标"已下架".
    let (added, skipped, removed) = {
        let mut registry = state.registry.write().await;
        let mut existing: Vec<String> = registry
            .providers()
            .iter()
            .find(|p| p.name == name)
            .map(|p| {
                let mut v: Vec<String> = p
                    .models
                    .iter()
                    .map(|(k, m)| m.upstream_model.clone().unwrap_or_else(|| k.clone()))
                    .collect();
                v.sort();
                v.dedup(); // 多个中转ID 可别名到同一上游模型, 去重避免重复计数
                v
            })
            .unwrap_or_default();
        let removed: Vec<String> = existing.drain(..).filter(|id| !ids.contains(id)).collect();
        let (added, skipped) = registry.add_models(&name, &ids);
        (added, skipped, removed)
    };

    // 注: 不再直接写盘/重载. 拉取的模型仅写入内存注册表 (运行期即时生效),
    // 并由前端合并进供应商表单, 待用户点 "保存配置" 才持久化.
    // 这样不会冲掉用户在表单里尚未保存的其它改动 (包含关系: 保存由用户主导).

    Json(serde_json::json!({
        "success": true,
        "provider": name,
        "models": ids,
        "added": added,
        "skipped": skipped,
        "removed": removed.clone(),
        "removed_count": removed.len(),
        "message": crate::i18n::msg_models_fetched(ids.len(), added, skipped),
    }))
}

/// 模拟测试数据 — 生成假请求日志用于前端调试, 不消耗上游 token.
pub async fn api_mock(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    #[derive(Clone)]
    struct MockRng(u64);
    impl MockRng {
        fn next_u64(&mut self) -> u64 {
            self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            self.0
        }
        fn gen_range(&mut self, lo: u64, hi: u64) -> u64 {
            lo + (self.next_u64() % (hi - lo))
        }
    }
    let mut rng = MockRng(12345);

    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    let models = [
        ("big-pickle-ZEN", "zen", "https://api.example.com/zen/v1/chat/completions"),
        ("deepseek-v4-flash-free-ZEN", "zen", "https://api.example.com/zen/v1/chat/completions"),
        ("mimo-v2.5-free-ZEN", "zen", "https://api.example.com/zen/v1/chat/completions"),
        ("north-mini-code-free-ZEN", "zen", "https://api.example.com/zen/v1/chat/completions"),
        ("deepseek-v4-pro-GO", "go", "https://api.example.com/go/v1/chat/completions"),
        ("kimi-k2.7-code-GO", "go", "https://api.example.com/go/v1/chat/completions"),
        ("glm-5.2-GO", "go", "https://api.example.com/go/v1/chat/completions"),
        ("deepseek-v4-flash-DS", "deepseek", "https://api.deepseek.com/v1/chat/completions"),
    ];

    // 生成 80 条成功 + 20 条错误 = 100 条, 分布在最近 7 天
    let mut logs = Vec::with_capacity(100);
    for i in 0..80 {
        let (model, prov, ep) = models[i % models.len()];
        let seconds_ago = rng.gen_range(0, 604800);
        logs.push(RequestLog {
            timestamp: now - seconds_ago,
            model: model.to_string(),
            provider: prov.to_string(),
            endpoint: ep.to_string(),
            status: 200,
            latency_ms: rng.gen_range(300, 6000),
            body_len: rng.gen_range(50, 2000) as usize,
            error: None,
            prompt_tokens: rng.gen_range(50, 500) as u32,
            completion_tokens: rng.gen_range(100, 2000) as u32,
            cached: false,
            prompt_cache_hit_tokens: rng.gen_range(0, 400) as u32,
            prompt_cache_miss_tokens: rng.gen_range(0, 100) as u32,
            prompt_cache_creation_tokens: 0,
            strip_saved_tokens: 0,
            trim_saved_tokens: 0,
            resp_cache_saved_tokens: 0,
            audit_exempt_reasoning_tokens: 0,
            audit_dup_block_tokens: 0,
            audit_dup_block_count: 0,
            strip_toolcall_saved_tokens: 0,
            audit_observed: false,
            price: None,
            usage_estimated: false,
            first_token_ms: None,
            upstream_model: None,
        });
    }

    let err_msgs = [
        "upstream returned 429: Too Many Requests",
        "upstream returned 500: Internal server error",
        "connection reset by peer",
        "upstream timeout after 30s",
    ];
    for i in 0..20 {
        let (model, prov, ep) = models[(i + 3) % models.len()];
        let seconds_ago = rng.gen_range(0, 604800);
        logs.push(RequestLog {
            timestamp: now - seconds_ago,
            model: model.to_string(),
            provider: prov.to_string(),
            endpoint: ep.to_string(),
            status: if i % 2 == 0 { 429 } else { 502 },
            latency_ms: rng.gen_range(100, 3000),
            body_len: rng.gen_range(50, 2000) as usize,
            error: Some(err_msgs[i % err_msgs.len()].to_string()),
            prompt_tokens: 0,
            completion_tokens: 0,
            cached: false,
            prompt_cache_hit_tokens: 0,
            prompt_cache_miss_tokens: 0,
            prompt_cache_creation_tokens: 0,
            strip_saved_tokens: 0,
            trim_saved_tokens: 0,
            resp_cache_saved_tokens: 0,
            audit_exempt_reasoning_tokens: 0,
            audit_dup_block_tokens: 0,
            audit_dup_block_count: 0,
            strip_toolcall_saved_tokens: 0,
            audit_observed: false,
            price: None,
            usage_estimated: false,
            first_token_ms: None,
            upstream_model: None,
        });
    }

    // 写入缓冲区
    for log in &logs {
        state.log_buffer.push(log.clone()).await;
    }

    let count = logs.len();
    Json(serde_json::json!({
        "message": crate::i18n::msg_mock_generated(count),
        "success_count": 80,
        "error_count": 20,
        "model_count": 4,
        "provider_count": 3,
    }))
}

/// GET /admin/api/health — 复用熔断状态 + TCP 连通性预检 (不再打真实 /models, 省 token).
pub async fn api_health(
    State(state): State<super::proxy::AppState>,
) -> Json<Vec<HealthEntry>> {
    let providers = state.registry.read().await.providers();
    let precheck_timeout = Duration::from_secs(5);

    // 并发: 读取熔断状态并做 HTTP 连通性预检 (复用 reqwest 客户端, 与真实请求同路径).
    let client = &state.client;
    let mut futs = Vec::new();
    for p in &providers {
        let ep = p.endpoint.clone();
        let cb_state = {
            let g = state.breakers.lock().unwrap();
            g.get(&p.name)
                .map(|cb| cb.peek_state().as_str().to_string())
                .unwrap_or_else(|| "closed".to_string())
        };
        futs.push(async move {
            let start = Instant::now();
            let reachable = super::proxy::precheck_provider(client, &ep, precheck_timeout).await;
            (p.name.clone(), cb_state, reachable, start.elapsed())
        });
    }

    let results = join_all(futs).await;
    let entries = results
        .into_iter()
        .map(|(name, cb, reachable, elapsed)| {
            let endpoint = providers
                .iter()
                .find(|p| p.name == name)
                .map(|p| p.endpoint.clone())
                .unwrap_or_default();
            let latency_ms = elapsed.as_millis() as u64;
            HealthEntry {
                provider: name,
                endpoint,
                status_level: crate::i18n::health_level(&cb, reachable).to_string(),
                status_text: crate::i18n::health_status_text(&cb, reachable),
                latency_ms,
                circuit_text: crate::i18n::circuit_state_cn(&cb).to_string(),
                circuit: cb,
            }
        })
        .collect();
    Json(entries)
}

/// POST /admin/api/circuit/reset — 手动强制关闭某供应商的熔断 (运维用).
#[derive(serde::Deserialize)]
pub struct CircuitResetReq {
    provider: String,
}

pub async fn api_circuit_reset(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<CircuitResetReq>,
) -> Json<serde_json::Value> {
    let mut g = state.breakers.lock().unwrap();
    match g.get_mut(&payload.provider) {
        Some(cb) => {
            cb.force_close();
            Json(serde_json::json!({ "message": crate::i18n::msg_circuit_reset(&payload.provider) }))
        }
        None => Json(serde_json::json!({ "message": crate::i18n::msg_circuit_none(&payload.provider) })),
    }
}

/// GET /admin/api/lang — 返回当前界面语言 (供前端初始化下拉).
pub async fn api_lang() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "lang": crate::i18n::lang_code() }))
}

/// POST /admin/api/lang — 切换并持久化界面语言 (写入 lang.json + 更新运行态).
#[derive(serde::Deserialize)]
pub struct LangReq {
    lang: String,
}

pub async fn api_lang_set(Json(payload): Json<LangReq>) -> Json<serde_json::Value> {
    match crate::i18n::parse_lang(&payload.lang) {
        Some(lang) => match crate::lang::save_lang(lang) {
            Ok(()) => Json(serde_json::json!({ "success": true, "lang": crate::i18n::lang_code() })),
            Err(e) => Json(serde_json::json!({ "error": crate::i18n::msg_lang_save_failed(&e) })),
        },
        None => Json(serde_json::json!({ "error": crate::i18n::msg_lang_unsupported(&payload.lang) })),
    }
}

/// GET /admin/api/version — 返回结构化版本信息 (供前端关于页/页脚展示).
pub async fn api_version() -> Json<serde_json::Value> {
    Json(crate::version::to_json())
}

/// GET /admin/api/proxy-config — 返回当前代理策略状态 (启动时由环境变量决定).
pub async fn api_proxy_config() -> Json<serde_json::Value> {
    Json(serde_json::json!(crate::proxy_cfg::proxy_status()))
}

/// GET /admin/api/changelog — 返回解析后的更新日志 (供关于页展示).
///
/// 解析烤入的 `CHANGELOG.md` (Keep a Changelog 风格): 按 `## [` 切分版本块,
/// 提取版本号/日期, 再按 `### ` 切分小节, 收集 `- ` 条目.
pub async fn api_changelog() -> Json<serde_json::Value> {
    Json(parse_changelog())
}

/// 读取用户已看过「更新亮点」的版本号（空字符串表示从未记录）。
pub async fn api_seen_version_get() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "version": crate::seen_version::load_seen() }))
}

/// 写入已见版本号，标记当前版本更新日志已读。
pub async fn api_seen_version_post(Json(body): Json<serde_json::Value>) -> Json<serde_json::Value> {
    let v = body
        .get("version")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    crate::seen_version::save_seen(&v);
    Json(serde_json::json!({ "ok": true, "version": v }))
}

/// 解析 `CHANGELOG.md` 为结构化 JSON: { versions: [{ version, date, sections:[{title,items}] }] }.
fn parse_changelog() -> serde_json::Value {
    let mut versions = Vec::new();
    for raw in CHANGELOG.split("\n## [") {
        let raw = raw.trim();
        if raw.is_empty() {
            continue;
        }
        // 首行形如 "[0.3.0] - 2026-08-08"; 注意 split("\n## [") 已吞掉 "## [",
        // 因此段首可能无 '[' 前缀 (如 "0.3.0] - 2026-08-08"), 统一用 find(']') 解析.
        let nl = raw.find('\n').unwrap_or(raw.len());
        let header = raw[..nl].strip_prefix('[').unwrap_or(&raw[..nl]);
        let mut version = String::new();
        let mut date = String::new();
        if let Some(close) = header.find(']') {
            version = header[..close].trim().to_string();
            let after = &header[close + 1..];
            if let Some(d) = after.strip_prefix(" - ") {
                date = d.trim().to_string();
            }
        }
        if version.is_empty() {
            continue; // 跳过文件头等非版本块
        }
        let body = if nl < raw.len() { &raw[nl + 1..] } else { "" };
        let mut sections = Vec::new();
        let mut cur_title = String::new();
        let mut cur_items: Vec<String> = Vec::new();
        for line in body.lines() {
            let line = line.trim_end();
            if let Some(title) = line.strip_prefix("### ") {
                if !cur_title.is_empty() || !cur_items.is_empty() {
                    sections.push(serde_json::json!({ "title": cur_title, "items": cur_items }));
                    cur_items = Vec::new();
                }
                cur_title = title.trim().to_string();
            } else if let Some(item) = line.strip_prefix("- ") {
                cur_items.push(item.trim().to_string());
            }
        }
        if !cur_title.is_empty() || !cur_items.is_empty() {
            sections.push(serde_json::json!({ "title": cur_title, "items": cur_items }));
        }
        versions.push(serde_json::json!({
            "version": version,
            "date": date,
            "sections": sections
        }));
    }
    serde_json::json!({ "versions": versions })
}

#[cfg(test)]
mod changelog_tests {
    use super::*;

    /// 回归测试: 烤入的 CHANGELOG.md 必须能解析出全部版本块.
    /// 曾因 split("\n## [") 吞掉 "## [" 前缀导致版本块全部被跳过 (versions 恒为空).
    #[test]
    fn parse_changelog_returns_all_versions() {
        let json = parse_changelog();
        let versions = json.get("versions").and_then(|v| v.as_array()).unwrap();
        assert!(!versions.is_empty(), "versions 不应为空");
        // 校验版本号格式与字段完整性
        for v in versions {
            let version = v.get("version").and_then(|x| x.as_str()).unwrap_or("");
            assert!(version.starts_with('v') || version.chars().next().is_some_and(|c| c.is_ascii_digit()),
                "版本号格式异常: {version}");
            assert!(v.get("date").is_some(), "缺少 date");
            assert!(v.get("sections").and_then(|s| s.as_array()).is_some(), "缺少 sections");
        }
        // 首个版本应为最新版 = 当前 Cargo 版本 (动态断言, 版本号升级不再破坏测试).
        assert_eq!(
            versions[0].get("version").and_then(|x| x.as_str()),
            Some(env!("CARGO_PKG_VERSION"))
        );
    }
}

/// GET /admin/api/tooltip-config — 返回当前 tooltip 配置.
pub async fn api_tooltip_config_get() -> Json<serde_json::Value> {
    let config = tooltip::load_config();
    Json(serde_json::to_value(config).unwrap_or_default())
}

/// POST /admin/api/tooltip-config — 保存 tooltip 配置.
#[derive(serde::Deserialize)]
pub struct TooltipConfigReq {
    pub config: TooltipConfig,
}

pub async fn api_tooltip_config_set(
    Json(payload): Json<TooltipConfigReq>,
) -> Json<serde_json::Value> {
    match tooltip::save_config(&payload.config) {
        Ok(()) => Json(serde_json::json!({ "success": true })),
        Err(e) => Json(serde_json::json!({ "error": format!("保存失败: {}", e) })),
    }
}

/// GET /admin/api/currency — 返回当前费用显示币种配置.
pub async fn api_currency_get() -> Json<serde_json::Value> {
    let config = crate::currency::load_config();
    Json(serde_json::to_value(config).unwrap_or_default())
}

/// POST /admin/api/currency — 保存费用显示币种配置.
#[derive(serde::Deserialize)]
pub struct CurrencyConfigReq {
    pub config: crate::currency::CurrencyConfig,
}

pub async fn api_currency_set(
    Json(payload): Json<CurrencyConfigReq>,
) -> Json<serde_json::Value> {
    match crate::currency::save_config(&payload.config) {
        Ok(()) => Json(serde_json::json!({ "success": true })),
        Err(e) => Json(serde_json::json!({ "error": format!("保存失败: {}", e) })),
    }
}

/// GET /admin/api/peak-schedule — 返回高峰时段配置 (周几 / 时间段 / 时区).
pub async fn api_peak_schedule_get() -> Json<serde_json::Value> {
    Json(serde_json::to_value(crate::peak::current()).unwrap_or_default())
}

/// POST /admin/api/peak-schedule — 保存并热更新高峰时段配置.
#[derive(serde::Deserialize)]
pub struct PeakScheduleReq {
    pub config: crate::peak::PeakSchedule,
}

pub async fn api_peak_schedule_set(
    Json(payload): Json<PeakScheduleReq>,
) -> Json<serde_json::Value> {
    match crate::peak::save_config(&payload.config) {
        Ok(()) => Json(serde_json::json!({ "success": true })),
        Err(e) => Json(serde_json::json!({ "error": format!("保存失败: {}", e) })),
    }
}

/// GET /admin/api/balance — 返回各供应商余额信息 (API 查询 + 手动余额合并).
pub async fn api_balance(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    // 余额管理器为 AppState 常驻实例 (缓存跨请求复用), 不再每次重建.
    let balance_manager = &state.balance_manager;

    let registry = state.registry.read().await;
    let providers = registry.providers();
    
    // 构建供应商余额配置
    let mut provider_configs = std::collections::HashMap::new();
    for config in providers {
        if let Some(balance_endpoint) = &config.balance_endpoint {
            // 获取 API key (面板值 > 环境变量 > 默认值)
            let api_key = state.key_store.get_for_provider(&config.name).await
                .or_else(|| std::env::var(&config.api_key_env).ok().filter(|s| !s.is_empty()))
                .or_else(|| config.api_key_default.clone())
                .unwrap_or_default();
            
            provider_configs.insert(
                config.name.clone(),
                ProviderBalanceConfig {
                    balance_endpoint: Some(balance_endpoint.clone()),
                    api_key,
                },
            );
        }
    }
    
    // 查询余额
    let balances = balance_manager.query_all_balances(&state.client, &provider_configs).await;
    
    Json(serde_json::json!({
        "balances": balances,
    }))
}

/// POST /admin/api/balance/manual — 设置或清除某供应商的手动余额.
#[derive(serde::Deserialize)]
pub struct ManualBalanceReq {
    /// 供应商名称.
    pub provider: String,
    /// 余额 (元). 传 None 表示清除手动余额, 回退到 API 查询.
    pub balance: Option<f64>,
}

pub async fn api_balance_manual_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<ManualBalanceReq>,
) -> Json<serde_json::Value> {
    let balance_manager = &state.balance_manager;

    let result = if let Some(balance) = payload.balance {
        balance_manager.set_manual_balance(&payload.provider, balance).await
    } else {
        balance_manager.clear_manual_balance(&payload.provider).await
    };
    // 手动设置/清除后, 清掉该供应商的 API 缓存, 使其回退或下次重查.
    let _ = balance_manager.clear_cache(&payload.provider).await;

    match result {
        Ok(()) => Json(serde_json::json!({ "success": true })),
        Err(e) => Json(serde_json::json!({ "error": e })),
    }
}

/// GET /admin/api/realtime — 返回最近 10 秒的实时指标 (供任务栏 tooltip 展示).
pub async fn api_realtime(
    State(state): State<super::proxy::AppState>,
) -> Json<RealtimeStats> {
    // 就地聚合: 不克隆全量日志, 仅持锁遍历一次 (省去每秒一次 5000 条深拷贝).
    Json(state.log_buffer.realtime_stats())
}

/// 计算实时统计 (同步版本, 供事件循环 tooltip 更新使用).
pub fn compute_realtime_stats_sync(log_buffer: &LogBuffer) -> RealtimeStats {
    log_buffer.realtime_stats()
}

/// 实时统计: 在 LogBuffer 内部持锁遍历一次完成聚合, 避免调用方先 drain_all 全量克隆.
impl LogBuffer {
    /// 就地聚合最近 N 秒 + 今日窗口指标, 单次遍历, 不分配全量 Vec.
    pub fn realtime_stats(&self) -> RealtimeStats {
        let now = now_ts();
        let window = 10u64; // 最近 10 秒
        let start = now.saturating_sub(window);
        let today_start = ((now as i64 + TZ_OFFSET_SECS) / 86400 * 86400 - TZ_OFFSET_SECS) as u64;

        let mut r_count: u64 = 0;
        let mut r_latency_sum: u64 = 0;
        let mut r_hit: u64 = 0;
        let mut r_prompt: u64 = 0;
        let mut r_gen_ct: u64 = 0;
        let mut r_gen_ms: u64 = 0;
        let mut today_count: u64 = 0;

        self.for_each_recent(0, |l| {
            if l.timestamp >= today_start {
                today_count += 1;
            }
            if l.timestamp < start {
                return;
            }
            r_count += 1;
            r_latency_sum += l.latency_ms;
            r_hit += l.prompt_cache_hit_tokens as u64;
            r_prompt += l.prompt_tokens as u64;
            if let Some(ft) = l.first_token_ms {
                if ft < l.latency_ms && l.completion_tokens > 0 {
                    r_gen_ct += l.completion_tokens as u64;
                    r_gen_ms += l.latency_ms - ft;
                }
            }
        });

        let count = r_count as f64;
        let requests_per_second = if window > 0 { count / window as f64 } else { 0.0 };
        let avg_latency_ms = if count > 0.0 {
            r_latency_sum as f64 / count
        } else {
            0.0
        };
        // 命中率口径必须与统计页一致: 命中 / **总输入 token** (含缓存首次写入).
        //
        // 曾用 `hit / (hit + miss)`, 而 creation 已从 miss 中拆出, 故 hit+miss = prompt - creation,
        // 分母偏小 → 有缓存写入的模型 (Anthropic 类) 命中率被系统性高估.
        // 两处同名字段用不同分母会让「托盘窗口」与「控制台统计页」对同一批流量给出不同数字,
        // 在 creation 为 0 时二者恰好相等, 因此长期未被发现.
        let cache_hit_rate = if r_prompt > 0 {
            r_hit as f64 / r_prompt as f64
        } else {
            0.0
        };
        let gen_speed = if r_gen_ms > 0 {
            r_gen_ct as f64 / r_gen_ms as f64 * 1000.0
        } else {
            0.0
        };
        RealtimeStats {
            requests_per_second,
            avg_latency_ms,
            cache_hit_rate,
            gen_speed,
            today_requests: today_count as usize,
            timestamp: now,
        }
    }
}

// ─── 响应缓存 (实验功能) ───

/// 缓存配置请求体 (运行时可调): 开关 / TTL / 条目上限. 字段均可选, 仅更新提供项.
#[derive(serde::Deserialize)]
pub struct CacheConfigReq {
    pub enabled: Option<bool>,
    pub ttl_secs: Option<u64>,
    pub max_entries: Option<usize>,
}

/// GET /admin/api/cache — 返回缓存当前状态与统计 (命中/未命中/条目数).
pub async fn api_cache_get(
    State(state): State<super::proxy::AppState>,
) -> Json<crate::cache::CacheStats> {
    Json(state.cache.stats())
}

/// POST /admin/api/cache — 运行时更新缓存配置 (面板"性能与优化"设置页).
/// 开关 / TTL / 条目上限独立可选; 仅更新请求中提供的字段, 其余保持原值.
pub async fn api_cache_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<CacheConfigReq>,
) -> Json<crate::cache::CacheStats> {
    if let Some(enabled) = payload.enabled {
        state.cache.set_enabled(enabled);
    }
    if let Some(ttl_secs) = payload.ttl_secs {
        state.cache.set_ttl(ttl_secs);
    }
    if let Some(max_entries) = payload.max_entries {
        state.cache.set_max_entries(max_entries);
    }
    Json(state.cache.stats())
}

/// POST /admin/api/cache/clear — 手动清空全部缓存条目 (实验功能调试用).
pub async fn api_cache_clear(
    State(state): State<super::proxy::AppState>,
) -> Json<crate::cache::CacheStats> {
    state.cache.clear();
    Json(state.cache.stats())
}

// ─── 历史推理链瘦身开关 ───

/// GET /admin/api/strip-reasoning — 返回当前是否剥离历史推理链.
pub async fn api_strip_reasoning_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "enabled": state.strip_history_reasoning.load(std::sync::atomic::Ordering::Relaxed) }))
}

/// POST /admin/api/strip-reasoning — 运行时切换历史推理链剥离开关.
#[derive(serde::Deserialize)]
pub struct StripReasoningReq {
    pub enabled: bool,
}

pub async fn api_strip_reasoning_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<StripReasoningReq>,
) -> Json<serde_json::Value> {
    state.strip_history_reasoning.store(payload.enabled, std::sync::atomic::Ordering::Relaxed);
    Json(serde_json::json!({ "enabled": payload.enabled }))
}

// ─── 「带 tool_calls 的历史推理链」协议白名单 ───

/// GET /admin/api/strip-toolcall-protocols — 返回按协议剥离的开关状态.
///
/// 注意这里返回的是**协议**维度 (chat / responses), 不是供应商维度:
/// 供应商只是换 endpoint, 协议才决定剥离是否安全. `anthropic` 不在此列,
/// 因为该协议下剥离必然导致上游 400, 无开关可言.
pub async fn api_strip_toolcall_protocols_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "chat": state.strip_toolcall_on_chat.load(std::sync::atomic::Ordering::Relaxed),
        "responses": state.strip_toolcall_on_responses.load(std::sync::atomic::Ordering::Relaxed),
    }))
}

/// POST /admin/api/strip-toolcall-protocols — 运行时切换协议白名单.
/// 两个字段均可选, 只更新传入的项 (便于前端只提交变化的那个).
#[derive(serde::Deserialize)]
pub struct StripToolcallProtocolsReq {
    #[serde(default)]
    pub chat: Option<bool>,
    #[serde(default)]
    pub responses: Option<bool>,
}

pub async fn api_strip_toolcall_protocols_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<StripToolcallProtocolsReq>,
) -> Json<serde_json::Value> {
    use std::sync::atomic::Ordering;
    if let Some(v) = payload.chat {
        state.strip_toolcall_on_chat.store(v, Ordering::Relaxed);
    }
    if let Some(v) = payload.responses {
        state.strip_toolcall_on_responses.store(v, Ordering::Relaxed);
    }
    Json(serde_json::json!({
        "chat": state.strip_toolcall_on_chat.load(Ordering::Relaxed),
        "responses": state.strip_toolcall_on_responses.load(Ordering::Relaxed),
    }))
}

// ─── 长会话历史裁剪开关 ───

/// GET /admin/api/max-history-turns — 返回当前保留的最近 user 轮数 (0 = 不裁剪).
pub async fn api_max_history_turns_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "turns": state.max_history_turns.load(std::sync::atomic::Ordering::Relaxed) }))
}

/// POST /admin/api/max-history-turns — 运行时设置保留轮数 (0 = 关闭裁剪, 推荐 10~30).
#[derive(serde::Deserialize)]
pub struct MaxHistoryTurnsReq {
    pub turns: usize,
}

pub async fn api_max_history_turns_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<MaxHistoryTurnsReq>,
) -> Json<serde_json::Value> {
    let turns = payload.turns;
    state.max_history_turns.store(turns, std::sync::atomic::Ordering::Relaxed);
    Json(serde_json::json!({ "turns": turns }))
}

// ─── 流截断自动续写 ───

/// GET /admin/api/auto-continue — 返回当前流截断自动续写次数上限 (0 = 关闭).
pub async fn api_auto_continue_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "count": state.auto_continue.load(std::sync::atomic::Ordering::Relaxed) }))
}

/// POST /admin/api/auto-continue — 运行时设置自动续写上限 (0 = 关闭, 推荐 1~3).
/// 上游断流且无 finish_reason 时, 网关自动带已输出正文重发"继续"请求并拼接新响应.
#[derive(serde::Deserialize)]
pub struct AutoContinueReq {
    pub count: usize,
}

pub async fn api_auto_continue_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<AutoContinueReq>,
) -> Json<serde_json::Value> {
    // 上限保护: 续写链过长会成倍放大延迟与 token 消耗.
    let count = payload.count.min(5);
    state.auto_continue.store(count, std::sync::atomic::Ordering::Relaxed);
    Json(serde_json::json!({ "count": count }))
}

// ─── 流空闲超时 / 重试参数 (运行时可调) ───

/// GET /admin/api/stream-timeout — 流式响应空闲超时秒数.
pub async fn api_stream_timeout_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "secs": state.stream_idle_timeout_secs.load(std::sync::atomic::Ordering::Relaxed) }))
}

#[derive(serde::Deserialize)]
pub struct StreamTimeoutReq {
    pub secs: u64,
}

pub async fn api_stream_timeout_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<StreamTimeoutReq>,
) -> Json<serde_json::Value> {
    // 合理区间: 太小误伤长思考静默期, 太大失去假死保护.
    let secs = payload.secs.clamp(30, 600);
    state.stream_idle_timeout_secs.store(secs, std::sync::atomic::Ordering::Relaxed);
    Json(serde_json::json!({ "secs": secs }))
}

/// GET /admin/api/retry — 瞬态失败重试参数 (次数 + 退避基数毫秒).
pub async fn api_retry_get(
    State(state): State<super::proxy::AppState>,
) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "max": state.retry_max.load(std::sync::atomic::Ordering::Relaxed),
        "backoff_ms": state.retry_backoff_ms.load(std::sync::atomic::Ordering::Relaxed),
    }))
}

#[derive(serde::Deserialize)]
pub struct RetryReq {
    pub max: u32,
    pub backoff_ms: u64,
}

pub async fn api_retry_set(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<RetryReq>,
) -> Json<serde_json::Value> {
    let mx = payload.max.min(5);
    let backoff = payload.backoff_ms.min(10_000);
    state.retry_max.store(mx, std::sync::atomic::Ordering::Relaxed);
    state.retry_backoff_ms.store(backoff, std::sync::atomic::Ordering::Relaxed);
    Json(serde_json::json!({ "max": mx, "backoff_ms": backoff }))
}

// ─── 模型元信息 (models.dev) ───

/// POST /admin/api/model-meta — 批量解析模型元信息 (上下文/输出限制/视觉等).
/// 请求体 `{names: ["kimi-k3-free", ...]}`; 响应 `{meta: {名称: ModelMeta|null}}`,
/// null = 未收录 (前端隐藏标签). 首次调用触发 models.dev 拉取 (24h 缓存, 走系统代理),
/// 拉取失败返回全 null 不阻塞面板.
#[derive(serde::Deserialize)]
pub struct ModelMetaReq {
    pub names: Vec<String>,
}

pub async fn api_model_meta(
    State(state): State<super::proxy::AppState>,
    Json(payload): Json<ModelMetaReq>,
) -> Json<serde_json::Value> {
    // 上限保护: 名单异常大时截断 (正常配置 <500 个模型).
    let names: Vec<String> = payload.names.into_iter().take(2000).collect();
    let map = state.model_meta.resolve_many(&state.client, &names).await;
    Json(serde_json::json!({ "meta": map }))
}

// ─── 使用统计 ───

/// 本轮 (进程启动以来) 累计统计 — 在 push 时原子累加, 不受日志 5000 条滚动窗口封顶影响.
#[derive(Debug, Clone, Default, Serialize)]
pub struct SessionStats {
    /// 本轮请求总数 (含错误/缓存命中).
    pub requests: u64,
    /// 本轮成功请求数 (status < 400).
    pub success: u64,
    /// 本轮输入 token 累计.
    pub prompt_tokens: u64,
    /// 本轮输出 token 累计.
    pub completion_tokens: u64,
    /// 本轮上游 KV Cache 命中 token 累计.
    pub cache_hit_tokens: u64,
    /// 本轮上游 KV Cache 未命中 token 累计.
    pub cache_miss_tokens: u64,
}

/// 单个模型的聚合统计.
#[derive(Debug, Clone, Serialize)]
pub struct ModelStats {
    /// 上游模型名（不再把 provider 拼进模型身份）.
    pub model: String,
    /// 产生该聚合项的供应商.
    pub provider: String,
    /// 上游模型名的显式字段，供前端按字段筛选.
    pub upstream_model: String,
    pub requests: usize,
    pub errors: usize,
    pub avg_latency_ms: f64,
    pub total_body_bytes: usize,
    pub total_prompt_tokens: u64,
    pub total_completion_tokens: u64,
    /// 上游 KV Cache 命中/未命中 token 数 (DeepSeek 等).
    pub total_cache_hit_tokens: u64,
    pub total_cache_miss_tokens: u64,
    /// 纯生成吐字速度 (tok/s): 仅统计首 token 延迟已知的流式请求,
    /// = Σ输出token / Σ(总耗时-首token延迟) × 1000. 排除排队与 TTFT, 比 avg_latency 反推更准.
    pub gen_speed: f64,
    /// Σ输出 token / Σ生成耗时 (合并 rollup 统计时重算 gen_speed 用, 不下发给前端).
    #[serde(skip)]
    pub gen_output_tokens: u64,
    #[serde(skip)]
    pub gen_time_sum_ms: u64,
    /// 映射到本聚合项的中转 ID 集合 (去重). 因统计按"供应商/上游模型"聚合,
    /// 多个中转 ID 可映射到同一上游模型, 此字段便于对照溯源.
    pub aliases: Vec<String>,
    /// 该模型累计费用 (元), 见 `crate::pricing`.
    pub total_cost: f64,
    /// 应用于本聚合项的单价（元/百万tokens）. 来自 providers.json 覆盖或内置表;
    /// 取组内首条日志解析的结果作代表（同组内多中转 ID 映射到同一上游, 单价通常一致）.
    /// `None` 表示未配置价格（费用记 0）.
    pub price: Option<ModelPrice>,
    /// 是否免费模型 (显式 free 标记或上游/中转模型名含 free/免费). 与路由配置页判定完全一致,
    /// 由调用方从注册表预计算 free_ids 传入, 组内任一中转 ID 命中即标记.
    pub free: bool,
}

/// 单个供应商的聚合统计.
#[derive(Debug, Clone, Serialize)]
pub struct ProviderStats {
    pub provider: String,
    pub requests: usize,
    pub errors: usize,
    pub avg_latency_ms: f64,
    pub total_body_bytes: usize,
    pub total_prompt_tokens: u64,
    pub total_completion_tokens: u64,
    /// 上游 KV Cache 命中/未命中 token 数 (DeepSeek 等).
    pub total_cache_hit_tokens: u64,
    pub total_cache_miss_tokens: u64,
    /// 纯生成吐字速度 (tok/s), 同 ModelStats.gen_speed.
    pub gen_speed: f64,
    /// Σ输出 token / Σ生成耗时 (合并 rollup 统计时重算 gen_speed 用, 不下发给前端).
    #[serde(skip)]
    pub gen_output_tokens: u64,
    #[serde(skip)]
    pub gen_time_sum_ms: u64,
    /// 该供应商累计费用 (元), 见 `crate::pricing`.
    pub total_cost: f64,
    /// 该供应商今日窗口费用 (元, 东八区日界). 前端据 total_cost/today_cost 是否 >0 决定
    /// 是否展示该供应商费用卡片 (方案 A: 只显示有实际费用的供应商).
    pub today_cost: f64,
    /// 该供应商 KV 缓存命中节省的金额 (元). 仅计费供应商有意义; 免费/月套餐等
    /// 不按量计费模型在聚合层已被 `free_ids` 排除 (见 `compute_stats` 分组循环).
    /// 全局合计无意义 (会混入不计费供应商), 故前端仅按供应商各自展示.
    pub cache_saved: f64,
    /// 该供应商是否按量计费 (组内任一中转 ID 不在 free_ids 即视为计费). 仅计费供应商
    /// 在前端展示"已省"列; 免费/月套餐等不按量计费供应商 (如 opencode) 无费用基数,
    /// "已省"无实际意义, 前端以 "—" 占位而非误显示金额.
    pub billing: bool,
    /// 今日窗口 (东八区日界): 转发优化省下的输入 token 总数 (剥离推理链 + 历史裁剪 + 响应缓存命中).
    pub today_opt_saved_tokens: u64,
    /// 今日窗口优化省量明细: 仅剥离推理链省下的输入 token.
    pub today_strip_saved_tokens: u64,
    /// 今日窗口优化省量明细: 仅历史裁剪省下的输入 token.
    pub today_trim_saved_tokens: u64,
    /// 今日窗口优化省量明细: 仅响应缓存命中省下的 token.
    pub today_resp_cache_saved_tokens: u64,
}

/// 单个模型在时间桶内的 Token 趋势.
#[derive(Debug, Clone, Serialize)]
pub struct ModelTrend {
    pub date: String,
    pub ts: u64,
    pub provider: String,
    pub upstream_model: String,
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    pub requests: usize,
    pub errors: usize,
}

/// 日趋势 (通用: 按小时/天/月聚合均使用此结构).
#[derive(Debug, Clone, Serialize)]
pub struct DailyTrend {
    pub date: String,
    /// 该时间桶的起始时间戳(秒), 用于按真实时间排序(避免 MM/DD 字符串比较跨月错序).
    pub ts: u64,
    pub requests: usize,
    pub errors: usize,
    pub avg_latency: f64,
    /// 输入 token 总量 (按桶累加, 用于趋势图 Tokens 视角).
    pub total_prompt_tokens: u64,
    /// 输出 token 总量 (按桶累加, 用于趋势图 Tokens 视角).
    pub total_completion_tokens: u64,
    pub total_cache_hit_tokens: u64,
    pub total_cache_miss_tokens: u64,
    /// 该时间桶费用 (元).
    pub total_cost: f64,
}

/// 省量审计汇总: 潜在可省量的分布 (只读统计, **不代表已生效的优化**).
///
/// 目的是回答"还能从哪儿省 token": 每一项都对应一种尚未启用的改写策略,
/// 以及它在这批请求里能省下的量级. 全部为估算 (字符/4 ≈ token).
#[derive(Debug, Clone, Serialize, Default)]
pub struct AuditSummary {
    /// 带 tool_calls 的 assistant 消息里被豁免剥离的推理链 token.
    /// 当前 `strip_history_reasoning` 对这类消息不剥离, 而 agent 循环中它们占多数.
    pub exempt_reasoning_tokens: u64,
    /// 历史中字节级重复的大块内容 (tool 结果 / user 粘贴) 可省略的 token (保留首次).
    /// 属"信息无损但模型行为可能偏移", 启用前需评估.
    pub dup_block_tokens: u64,
    /// 上述重复块的个数.
    pub dup_block_count: u64,
    /// 上游 KV 缓存写入 token (写溢价). **仅 Anthropic 原生协议上报**;
    /// OpenAI 兼容网关用扁平的 hit/miss 口径, 此处恒为 0 —— 不要据此判断缓存是否工作.
    pub cache_creation_tokens: u64,
    /// 上游 KV 缓存读取 token (命中收益).
    pub cache_read_tokens: u64,
    /// 未命中缓存的输入 token (= 输入总量 − 命中量): 这部分按全价计费, 才是可优化空间.
    pub cache_miss_tokens: u64,
    /// 输入 token 总量 (各项占比的分母).
    pub input_tokens: u64,
    /// 已生效的省量 (剥离推理链 + 历史裁剪 + 响应缓存命中), 作为对照基准.
    pub applied_saved_tokens: u64,
    /// 已生效省量的来源明细 —— 便于确认「剥离推理链」开关是否真的在起作用.
    /// 剥离推理链中来自「带 tool_calls 的轮次」的部分 (即该开关的贡献).
    pub applied_strip_toolcall_tokens: u64,
    /// 已生效省量: 剥离推理链 (不含 tool_calls 轮次).
    pub applied_strip_tokens: u64,
    /// 已生效省量: 长会话历史裁剪.
    pub applied_trim_tokens: u64,
    /// 已生效省量: 本地响应缓存命中.
    pub applied_resp_cache_tokens: u64,
    /// 有多少个请求开启了「剥离 tool_calls 推理链」(用于提示开关是否已生效).
    pub strip_toolcall_requests: u64,
    /// 已生效省量折算的费用明细 (元), 与 total_opt_saved_fee 同口径.
    pub applied_strip_fee: f64,
    pub applied_strip_toolcall_fee: f64,
    pub applied_trim_fee: f64,
    pub applied_resp_cache_fee: f64,
    /// KV 缓存净省费用 (元): Σ(命中折扣 − 写入溢价), 即 total_cache_saved.
    pub cache_saved_fee: f64,
    /// 窗口内非缓存请求总数.
    pub requests: u64,
    /// 其中真正做过审计的请求数 (升级前的老日志与直通/回放路径不计).
    /// 远小于 `requests` 时, 各项潜在可省量会被低估, 需积累更多新请求再看.
    pub sampled_requests: u64,
    /// 窗口内 token 为**估算/未上报**的请求数 (上游没返回 usage).
    ///
    /// 数据可信度指标: 这些请求的输入/输出 token 是本地估的 (或记 0), 与实测值不可混看.
    /// 正常情况应接近 0; 持续偏高说明上游未按 OpenAI 口径返回 usage, 统计需打折看.
    pub estimated_requests: u64,
    /// 审计窗口的起止时间戳 (秒): 即实际纳入统计的最早/最晚日志时间.
    pub window_start: u64,
    pub window_end: u64,
    /// **已采样请求**的输入 token 合计.
    /// 潜在可省量只来自做过审计的请求, 占比必须以它为分母 ——
    /// 用窗口总输入会把占比按"采样比例"稀释掉 (实测用户 4999 条里仅少数有审计数据,
    /// 使重复块占比从真实的约 2% 被压成 0.0%).
    pub sampled_input_tokens: u64,
    /// 输出侧汇总 (全部来自既有日志字段, 无需新增请求路径埋点).
    pub output: OutputAudit,
}

/// 输出侧审计: 输出单价通常是输入的数倍, 这里看输出花在哪.
#[derive(Debug, Clone, Serialize, Default)]
pub struct OutputAudit {
    /// 输出 token 总量.
    pub output_tokens: u64,
    /// 有输出的请求数.
    pub requests: u64,
    /// 单请求输出超过 [`LONG_OUTPUT_TOKENS`] 的请求数.
    pub long_requests: u64,
    /// 上述长请求贡献的输出 token 数 (看输出是否高度集中在少数请求).
    pub long_tokens: u64,
    /// 单请求最大输出 token.
    pub max_tokens: u32,
    /// 死循环检测截断次数: 这部分输出是纯浪费 (模型重复直到被网关掐断).
    pub loop_truncated: u64,
}

/// 长输出判定阈值 (token). 约等于一次 8K 输出的上限.
pub const LONG_OUTPUT_TOKENS: u32 = 8192;

/// 使用统计聚合结果.
#[derive(Debug, Clone, Serialize)]
pub struct UsageStats {
    pub total_requests: usize,
    pub success_count: usize,
    pub error_count: usize,
    pub total_body_bytes: usize,
    pub avg_latency_ms: f64,
    pub total_prompt_tokens: u64,
    pub total_completion_tokens: u64,
    pub per_model: Vec<ModelStats>,
    pub per_provider: Vec<ProviderStats>,
    pub trends: Vec<DailyTrend>,
    pub model_trends: Vec<ModelTrend>,
    pub top_models: Vec<ModelStats>,
    /// 上游 KV Cache 命中/未命中 token 数 (DeepSeek 等).
    pub total_cache_hit_tokens: u64,
    pub total_cache_miss_tokens: u64,
    /// 命中率 = 命中 token / 总输入 token.
    pub cache_hit_rate: f64,
    /// 累计 KV 缓存**净**节省金额 (元): Σ(命中折扣 − 写入溢价, 见 `log_cache_saved`).
    /// 仅概览参考口径 (含不计费供应商, 语义有限); 供应商表「KV缓存省」列才是按供应商计费的权威口径.
    pub total_cache_saved: f64,
    /// 累计转发优化省下的输入 token 数: 剥离推理链 + 历史裁剪 + 响应缓存命中省下的 token (已持久化进日志, 跨重启累计).
    pub total_opt_saved_tokens: u64,
    /// 累计转发优化省下的费用 (元): 省下 token × 对应模型 input 单价. 仅计费供应商计入, 与 total_cost 同口径.
    pub total_opt_saved_fee: f64,
    /// 累计优化省量明细 (均来自日志, 跨重启累计): 仅剥离推理链省下的输入 token.
    pub total_strip_saved_tokens: u64,
    /// 累计优化省量明细: 仅历史裁剪省下的输入 token.
    pub total_trim_saved_tokens: u64,
    /// 累计优化省量明细: 仅响应缓存命中省下的 token.
    pub total_resp_cache_saved_tokens: u64,
    /// 本月窗口 (东八区月首0点起) 转发优化省下的输入 token 数 (剥离推理链 + 历史裁剪 + 响应缓存命中).
    pub month_opt_saved_tokens: u64,
    /// 本月窗口优化省量折算的费用 (元), 与 today_opt_saved_fee 同口径.
    pub month_opt_saved_fee: f64,
    /// 近 30 天每日优化省量序列 (tokens, 按本地日界聚合, 末位=今日), 用于头条卡片 sparkline.
    pub opt_saved_series: Vec<u64>,
    /// 累计费用 (元), 价格缺失的模型按 0 计.
    pub total_cost: f64,
    // ─── 今日窗口统计 (东八区日界, 不受日志 5000 条滚动窗口封顶影响) ───
    pub today_requests: usize,
    pub today_success: usize,
    pub today_errors: usize,
    pub today_avg_latency_ms: f64,
    pub today_total_prompt_tokens: u64,
    pub today_total_completion_tokens: u64,
    pub today_total_cache_hit_tokens: u64,
    pub today_total_cache_miss_tokens: u64,
    /// 今日窗口费用 (元, 东八区日界).
    pub today_total_cost: f64,
    /// 今日窗口: 转发优化省下的输入 token 数 (剥离推理链 + 历史裁剪 + 响应缓存命中). 向东八区日界对齐, 与今日 6 卡片同口径.
    pub today_opt_saved_tokens: u64,
    /// 今日窗口: 转发优化省下的费用 (元), 与 today_total_cost 同口径 (仅计费供应商计入).
    pub today_opt_saved_fee: f64,
    /// 今日窗口优化省量明细: 仅剥离推理链省下的输入 token.
    pub today_strip_saved_tokens: u64,
    /// 今日窗口优化省量明细: 仅历史裁剪省下的输入 token.
    pub today_trim_saved_tokens: u64,
    /// 今日窗口优化省量明细: 仅响应缓存命中省下的 token.
    pub today_resp_cache_saved_tokens: u64,
    /// 累计优化省量的最早记录日期 (MM/DD), 给"累计"标注时间起算点, 避免无意义地 forever 累计; 无优化记录时为空串.
    pub opt_saved_since: String,
    /// 省量审计汇总 (只读, 见 [`AuditSummary`]).
    pub audit: AuditSummary,
    /// 是否有任意模型配置了价格 (providers.json 的 model.price).
    /// 前端据此决定是否显示费用相关卡片/列, 避免无价格时显示 ¥0.00 误导.
    pub has_price_config: bool,
    /// 本轮 (进程启动以来) 累计统计 (原子计数, 不受日志滚动窗口封顶影响).
    pub session: SessionStats,
    /// 当前统计窗口内有效流式请求的加权生成速度 (tok/s).
    pub gen_speed: f64,
    /// 参与生成速度计算的请求数.
    pub gen_samples: usize,
    /// Σ输出 token / Σ生成耗时 (合并 rollup 统计时重算 gen_speed 用, 不下发给前端).
    #[serde(skip)]
    pub gen_output_tokens: u64,
    #[serde(skip)]
    pub gen_time_sum_ms: u64,
    /// 当前查询窗口的起始时间戳（秒）.
    pub window_start: u64,
    /// 当前查询窗口的结束时间戳（秒）.
    pub window_end: u64,
    /// 当前查询窗口长度（分钟）.
    pub window_minutes: f64,
    /// 当前统计窗口的可读来源说明.
    pub source_window: String,
}

fn stats_range_seconds(range: &str) -> Option<u64> {
    match range {
        "1d" => Some(86400),
        "7d" => Some(7 * 86400),
        "14d" => Some(14 * 86400),
        "29d" => Some(29 * 86400),
        "30d" => Some(30 * 86400),
        "365d" => Some(365 * 86400),
        _ => None,
    }
}

/// GET /admin/api/stats?granularity={hour|day|month}&range={1d|7d|14d|29d|window}
/// &provider=&model= — 返回同一查询窗口的使用统计.
pub async fn api_stats(
    State(state): State<super::proxy::AppState>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Json<UsageStats> {
    let granularity = params.get("granularity").map(|s| s.as_str()).unwrap_or("day");
    let range = params.get("range").map(|s| s.as_str()).unwrap_or("window");
    let provider_filter = params.get("provider").filter(|s| !s.trim().is_empty()).cloned();
    let model_filter = params.get("model").filter(|s| !s.trim().is_empty()).cloned();
    let now = now_ts();
    let range_start = stats_range_seconds(range).map(|seconds| now.saturating_sub(seconds));
    let range_end = now;
    let bucket_marker = bucket_start(now, granularity);
    // 缓存键必须包含查询范围、筛选条件和当前桶, 否则跨日/切换筛选会返回旧快照.
    let query_key = format!(
        "{}|range={}|provider={}|model={}|bucket={}",
        granularity,
        range,
        provider_filter.as_deref().unwrap_or(""),
        model_filter.as_deref().unwrap_or(""),
        bucket_marker,
    );
    let seq = state.log_buffer.seq();
    let prov_mtime = std::fs::metadata("providers.json")
        .and_then(|m| m.modified())
        .map(|t| t.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0))
        .unwrap_or(0);

    {
        let guard = state.stats_cache.lock().await;
        if let Some(cached) = guard.as_ref() {
            if cached.0 == seq && cached.1 == query_key && cached.2 == prov_mtime {
                return Json(cached.3.clone());
            }
        }
    }

    let all_logs = state.log_buffer.drain_all().await;
    let filtered_logs: Vec<RequestLog> = all_logs
        .into_iter()
        .filter(|log| range_start.map(|start| log.timestamp >= start && log.timestamp < range_end).unwrap_or(true))
        .filter(|log| provider_filter.as_deref().map(|provider| log.provider == provider).unwrap_or(true))
        .filter(|log| {
            model_filter.as_deref().map(|model| {
                let needle = model.to_lowercase();
                log.model.to_lowercase().contains(&needle)
                    || log.upstream_model.as_deref().unwrap_or("").to_lowercase().contains(&needle)
            }).unwrap_or(true)
        })
        .collect();

    let registry = state.registry.read().await;
    let mut has_price_config = false;
    let mut free_ids: std::collections::HashSet<String> = std::collections::HashSet::new();
    for provider in registry.providers() {
        for (model_id, mcfg) in provider.models {
            if mcfg.is_free(model_id.as_str()) {
                free_ids.insert(model_id.clone());
            }
            if mcfg.price.is_some() {
                has_price_config = true;
            }
        }
    }
    // registry.providers() 返回借用切片, 需在 drop 前构建价格表 (内部只读, 不持有借用).
    let price_table = PriceTable::from_registry(&registry.providers());
    drop(registry);

    // ─── 日级 rollup 合并: 日志滚动窗口之外、且今天之前的天从 rollup 读取,
    // 补齐月视图等长跨度统计 (5000 条封顶不再限制总请求/总费用/趋势).
    // 仅显式范围 (分析页 29d/365d 等) 合并; 'window' (仪表盘) 不合并 —
    // 否则 effective_start 会被历史数据拉远, 趋势桶数量爆炸冻结前端. ───
    let mut log_side = filtered_logs;
    let mut merged_days: Vec<crate::rollup::DailyRollup> = Vec::new();
    // 小时粒度只看近 24 小时 (总在日志窗口内), 且 rollup 无小时拆分, 不参与合并.
    if granularity != "hour" && range_start.is_some() {
        // rollup() 返回临时引用 &Arc; 必须 clone Arc 才能在 if let 块外安全使用.
        if let Some(book_arc) = state.log_buffer.rollup().cloned() {
            if let Some(cover_ts) = state.log_buffer.oldest_ts().await {
                if let Some((merge_end, rstart_day)) =
                    rollup_merge_bounds(now, range_start.unwrap_or(0), cover_ts)
                {
                    merged_days = book_arc.days_between(rstart_day, merge_end);
                    if !merged_days.is_empty() {
                        // 边界天内已在 rollup 的天从日志侧剔除, 防重复计数.
                        log_side.retain(|log| bucket_start(log.timestamp, "day") > merge_end);
                    }
                }
            }
        }
    }

    let mut stats = compute_stats(&log_side, &price_table, &free_ids);
    stats.has_price_config = has_price_config;
    let effective_start = range_start.unwrap_or_else(|| log_side.iter().map(|log| log.timestamp).min().unwrap_or(now));
    stats.trends = compute_trends_window(&log_side, granularity, &price_table, Some(effective_start), Some(range_end));
    stats.model_trends = compute_model_trends_window(&log_side, granularity, Some(effective_start), Some(range_end));
    merge_rollup_days(&mut stats, &merged_days, granularity, &price_table, &free_ids);
    stats.window_start = effective_start;
    stats.window_end = range_end;
    stats.window_minutes = ((range_end.saturating_sub(effective_start)) as f64 / 60.0).max(1.0);
    stats.source_window = match range {
        "1d" => "最近 1 天".to_string(),
        "7d" => "最近 7 天".to_string(),
        "14d" => "最近 14 天".to_string(),
        "29d" => "最近 29 天".to_string(),
        "30d" => "最近 30 天".to_string(),
        "365d" => "最近 12 个月".to_string(),
        _ => "当前运行日志窗口".to_string(),
    };
    stats.session = state.log_buffer.session_stats();

    *state.stats_cache.lock().await = Some((seq, query_key, prov_mtime, stats.clone()));
    Json(stats)
}


/// 本地时区偏移 (秒). 默认按东八区 (UTC+8) 切分日/月界, 使趋势符合用户日历.
/// 若需跟随系统时区可改为读取本地 UTC 偏移, 但固定东八区对国内用户更可预期.
const TZ_OFFSET_SECS: i64 = 8 * 3600;

/// 自 1970-01-01 起的累计天数 -> (年, 月, 日). 与 days_from_civil 互逆, 无 chrono 依赖.
fn days_to_ymd(mut d: i64) -> (i64, i64, i64) {
    if d < 0 {
        d = 0;
    }
    let mut y = 1970i64;
    loop {
        let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
        let dim = if leap { 366 } else { 365 };
        if d < dim {
            break;
        }
        d -= dim;
        y += 1;
    }
    let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
    let mdays: [i64; 12] = if leap {
        [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    } else {
        [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    };
    let mut m = 0usize;
    for (i, &md) in mdays.iter().enumerate() {
        if d < md {
            m = i + 1;
            break;
        }
        d -= md;
    }
    if m == 0 {
        m = 12;
    }
    (y, m as i64, d + 1)
}

/// (年, 月, 日) -> 自 1970-01-01 起的累计天数 (Howard Hinnant 算法, 避免 chrono 依赖).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = if m > 2 { m - 3 } else { m + 9 };
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// 将 ts 对齐到指定粒度的桶起始秒 (本地时区). hour=整点, day=本地0点, month=本地月首0点.
pub(crate) fn bucket_start(ts: u64, granularity: &str) -> u64 {
    let local = (ts as i64) + TZ_OFFSET_SECS;
    match granularity {
        "hour" => {
            let base = (local / 3600) * 3600;
            (base - TZ_OFFSET_SECS) as u64
        }
        "month" => {
            let days = local / 86400;
            let (y, m, _) = days_to_ymd(days);
            (days_from_civil(y, m, 1) * 86400 - TZ_OFFSET_SECS) as u64
        }
        _ => {
            let base = (local / 86400) * 86400;
            (base - TZ_OFFSET_SECS) as u64
        }
    }
}

/// 桶起始 ts 步进到下一个桶 (本地时区, 对齐粒度).
fn next_bucket(ts: u64, granularity: &str) -> u64 {
    match granularity {
        "hour" => ts.saturating_add(3600),
        "month" => {
            let local = (ts as i64) + TZ_OFFSET_SECS;
            let days = local / 86400;
            let (y, m, _) = days_to_ymd(days);
            let (ny, nm) = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
            (days_from_civil(ny, nm, 1) * 86400 - TZ_OFFSET_SECS) as u64
        }
        _ => ts.saturating_add(86400),
    }
}

/// 将 Unix 时间戳转成 MM/DD 日期字符串 (按东八区切日界, 避免 chrono 依赖).
fn ts_to_date(ts: u64) -> String {
    const SECS_PER_DAY: u64 = 86400;
    // 先按本地时区偏移归到本地当天 0 点, 再取天数.
    let local = (ts as i64) + TZ_OFFSET_SECS;
    let days = if local < 0 { 0 } else { local / SECS_PER_DAY as i64 };
    let (_y, m, d) = days_to_ymd(days);
    format!("{:02}/{:02}", m, d)
}

fn ts_to_month(ts: u64) -> String {
    let date = ts_to_date(ts);
    let parts: Vec<&str> = date.split('/').collect();
    if parts.len() == 2 {
        // 年份须按本地时区天数推算 (与 ts_to_date 的本地切日一致),
        // 否则跨年边界会出现 "2025/01" 实为 2026 年 1 月 的串年 bug.
        let local = (ts as i64) + TZ_OFFSET_SECS;
        let y_days = if local < 0 { 0 } else { local / 86400 };
        let mut y = 1970i64;
        let mut remaining = y_days as i64;
        loop {
            let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
            let dim = if leap { 366 } else { 365 };
            if remaining < dim { break; }
            remaining -= dim;
            y += 1;
        }
        let month_num = parts[0].parse::<u32>().unwrap_or(1);
        format!("{y}/{month_num:02}")
    } else {
        date
    }
}

fn ts_to_hour(ts: u64) -> String {
    let date = ts_to_date(ts);
    // 小时也按本地时区归位.
    let local = (ts as i64) + TZ_OFFSET_SECS;
    let hour = (((local as u64) % 86400) / 3600) as u32;
    format!("{date} {hour:02}:00")
}

/// 价格表: 以 **(供应商, 中转 model id)** 为键.
///
/// 为何必须带供应商: 中转供应商下同名 model_id 可能出现在多个供应商条目里
/// (如两家都配 `deepseek/v4-flash`) 且价格不同. 早期实现只以 model_id 为键,
/// 后者覆盖前者 —— 实测路由侧对重复 model_id 已会告警且只保留一条, 费用侧同样
/// 只应跟随生效的那条, 否则会出现"按 A 家价格给 B 家请求记账".
#[derive(Default)]
struct PriceTable {
    by_provider_model: HashMap<(String, String), ModelPrice>,
    /// 注册表中出现过的供应商名. lookup 回退前据此判定"供应商是否已知" ——
    /// 已知供应商但该模型未配价时必须记 0, 不得串用别家同名模型的价格.
    known_providers: std::collections::HashSet<String>,
    /// model_id → 价格; 仅当该 model_id 出现在多个供应商下且**价格全部相同**时写入.
    /// 用于日志里 provider 为空/已改名时的回退匹配 (保持历史统计可读).
    by_model_unique: HashMap<String, ModelPrice>,
}

impl PriceTable {
    fn from_registry(providers: &[crate::providers::ProviderConfig]) -> Self {
        let mut t = PriceTable::default();
        // model_id → 出现过的价格集合 (用于判定是否唯一价)
        let mut seen: HashMap<String, Vec<ModelPrice>> = HashMap::new();
        for p in providers {
            t.known_providers.insert(p.name.clone());
            for (model_id, mcfg) in &p.models {
                let Some(price) = mcfg.price else { continue };
                t.by_provider_model
                    .insert((p.name.clone(), model_id.clone()), price);
                seen.entry(model_id.clone()).or_default().push(price);
            }
        }
        for (model_id, prices) in seen {
            if let Some(first) = prices.first() {
                if prices.iter().all(|p| p == first) {
                    t.by_model_unique.insert(model_id, *first);
                }
            }
        }
        t
    }

    /// 查价: 先按 (供应商, model) 精确匹配; 仅当**供应商未知** (空 / `-` / 已从配置移除,
    /// 即历史日志场景) 时才回退到全局唯一价.
    ///
    /// 已知供应商但该模型未配价格 → 返回 `None` (费用记 0), 不得回退 ——
    /// 否则会把别家同名模型的价格记到本供应商请求上.
    fn lookup(&self, provider: &str, model: &str) -> Option<ModelPrice> {
        if let Some(p) = self.by_provider_model.get(&(provider.to_string(), model.to_string())) {
            return Some(*p);
        }
        let provider_known = !provider.is_empty()
            && provider != "-"
            && self.known_providers.contains(provider);
        if provider_known {
            return None;
        }
        self.by_model_unique.get(model).copied()
    }
}

/// 价格解析结果记忆化键: (供应商, 中转 model, endpoint).
/// 同一组合的解析结果稳定, 可安全复用 (详见 `PriceMemo`).
type PriceMemoKey = (String, String, String);

/// 请求级价格解析记忆化表: 避免 5000 条日志各自重复查面板配置的价格表.
/// 仅在单次 `compute_stats`/`compute_trends` 调用生命周期内有效, 不入全局.
struct PriceMemo<'a> {
    table: &'a PriceTable,
    cache: std::cell::RefCell<HashMap<PriceMemoKey, Option<ModelPrice>>>,
}

impl<'a> PriceMemo<'a> {
    fn new(table: &'a PriceTable) -> Self {
        Self {
            table,
            cache: std::cell::RefCell::new(HashMap::new()),
        }
    }

    /// 解析单条日志的价格 (记忆化): 命中则直接返回缓存的 `Option<ModelPrice>`.
    ///
    /// 优先用日志自带的**价格快照** (记录当时结算的历史账单); 缺失 (升级前的旧日志)
    /// 才回退当前配置 —— 这条回退只服务于旧记录, 新记录一律带快照, 因此改价/删模型
    /// 不会再改写已有历史.
    fn resolve(&self, log: &RequestLog) -> Option<ModelPrice> {
        if let Some(snap) = log.price {
            return Some(snap);
        }
        let key = (log.provider.clone(), log.model.clone(), log.endpoint.clone());
        if let Some(v) = self.cache.borrow().get(&key).copied() {
            return v;
        }
        let p = pricing::resolve_price(self.table.lookup(&log.provider, &log.model));
        self.cache.borrow_mut().insert(key, p);
        p
    }
}

/// 单条请求费用（元）. 价格缺失时按 0 计（不计入费用）.
fn log_cost(memo: &PriceMemo, log: &RequestLog) -> f64 {
    // 命中本地响应缓存的请求未真实消费上游 token, 不计费,
    // 否则会与原请求重复计费, 导致费用虚高（高缓存命中率场景偏差极大）.
    if log.cached {
        return 0.0;
    }
    let p = memo.resolve(log);
    pricing::compute_cost_with_creation(
        p,
        log.timestamp,
        log.prompt_tokens,
        log.completion_tokens,
        log.prompt_cache_hit_tokens,
        log.prompt_cache_creation_tokens,
    )
    .unwrap_or(0.0)
}

/// 单条请求因 KV 缓存**净**节省的金额（元）.
///
/// 净省 = 命中折扣 − 写入溢价:
///   - 命中折扣 = 命中 token × (input 价 − cache 读价)（读价缺失/无优惠则让差为 0）；
///   - 写入溢价 = 首次写入 token × (cache_creation 价 − input 价)（写入价通常高于 input 价,
///     如 DeepSeek 写入 1.25×）。该笔额外支出须从命中折扣扣除才是真实净省, 否则当 T2 历史
///     裁剪平移 user 前缀 / 系统提示词改动 / 切模型导致缓存反复重建时, 「已省」会被高估.
/// 输入价为 0 (未配置/月套餐) 或价格缺失 → 无计费基数, 净省记 0. 与 `log_cost` 复用同一价格
/// 解析与钳制口径 (读价/写入价缺失时回退 input 价, 对应项让差为 0), 防上游脏数据越界.
fn log_cache_saved(memo: &PriceMemo, log: &RequestLog) -> f64 {
    if log.cached {
        return 0.0;
    }
    let p = memo.resolve(log);
    let Some(p) = p else { return 0.0; };
    // 按请求时段选择生效价（高峰/空闲）; 与 compute_cost 同口径.
    let (input_price, _, cache_read) = pricing::effective(p, log.timestamp);
    // 输入价为 0 (价格未配置/不按量计费, 如月套餐) → 无计费基数, 净省记 0 (兜底防误计).
    if input_price == 0.0 {
        return 0.0;
    }
    let prompt = log.prompt_tokens as f64;
    // 与 compute_cost 一致拆分 hit / creation, 防上游脏数据导致负 fresh 或越界.
    let hit = (log.prompt_cache_hit_tokens as f64).min(prompt);
    let creation = (log.prompt_cache_creation_tokens as f64).min((prompt - hit).max(0.0));

    // 命中折扣: 命中 token 按 (input − 读价) 省; 读价缺失回退 input → 让差为 0.
    let read_saved = (input_price - cache_read).max(0.0);
    // 写入溢价: 首次写入按 cache_creation 价计 (通常高于 input); 缺失回退 input → 让差为 0.
    let creation_price = p.cache_creation_per_m.unwrap_or(input_price);
    let creation_penalty = (creation_price - input_price).max(0.0);

    // 净省不钳制到 0: 单条纯写入请求可能为负贡献, 聚合后自然抵消, 方能反映真实成本.
    hit / 1e6 * read_saved - creation / 1e6 * creation_penalty
}

/// 从日志列表计算聚合统计.
///
/// `price_overrides`: 中转 model id → 价格, 来自 providers.json 的 model.price（优先级最高）,
/// 缺失时回退内置默认价格表（见 `crate::pricing`）.
/// `free_ids`: 免费中转 ID 集合（来自注册表 is_free 判定）, 组内任一中转 ID 命中即标记免费.
fn compute_stats(
    logs: &[RequestLog],
    price_table: &PriceTable,
    free_ids: &std::collections::HashSet<String>,
) -> UsageStats {
    // 价格解析记忆化: 本次聚合生命周期内复用 resolve_price 结果 (避免 5000 条重复解析).
    let memo = PriceMemo::new(price_table);
    let total = logs.len();
    let error_count = logs.iter().filter(|l| is_log_error(l)).count();
    let success_count = total.saturating_sub(error_count);
    let total_body_bytes: usize = logs.iter().map(|l| l.body_len).sum();
    let avg_latency_ms = if total > 0 {
        logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / total as f64
    } else {
        0.0
    };
    let total_prompt_tokens: u64 = logs.iter().map(|l| l.prompt_tokens as u64).sum();
    let total_completion_tokens: u64 = logs.iter().map(|l| l.completion_tokens as u64).sum();
    let total_cache_hit_tokens: u64 = logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
    let total_cache_miss_tokens: u64 = logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
    let total_cost: f64 = logs.iter().map(|l| log_cost(&memo, l)).sum();
    let total_cache_saved: f64 = logs.iter().map(|l| log_cache_saved(&memo, l)).sum();
    let (gen_tokens, gen_millis, gen_samples) = logs.iter().filter_map(|l| {
        l.first_token_ms.filter(|ft| *ft < l.latency_ms && l.completion_tokens > 0)
            .map(|ft| (l.completion_tokens as u64, l.latency_ms.saturating_sub(ft), 1usize))
    }).fold((0u64, 0u64, 0usize), |(tokens, millis, samples), (t, m, s)| (tokens + t, millis + m, samples + s));
    let gen_speed = if gen_millis > 0 { gen_tokens as f64 / gen_millis as f64 * 1000.0 } else { 0.0 };
    let (stats_gen_output_tokens, stats_gen_time_sum_ms) = (gen_tokens, gen_millis);
    // 转发优化省量 (剥离推理链 + 历史裁剪 + 响应缓存命中): 累计 token 与折算费用 (按各模型 input 单价, 与 total_cost 同口径).
    let total_strip_saved_tokens: u64 = logs.iter().map(|l| l.strip_saved_tokens as u64).sum();
    let total_trim_saved_tokens: u64 = logs.iter().map(|l| l.trim_saved_tokens as u64).sum();
    let total_resp_cache_saved_tokens: u64 = logs.iter().map(|l| l.resp_cache_saved_tokens as u64).sum();
    // 累计优化省量 = 剥离推理链 + 历史裁剪 + 响应缓存命中 (三者均来自日志, 跨重启累计).
    let total_opt_saved_tokens: u64 = total_strip_saved_tokens + total_trim_saved_tokens + total_resp_cache_saved_tokens;
    // 省量审计: 只读汇总, 不改动任何转发内容.
    let audit = AuditSummary {
        exempt_reasoning_tokens: logs.iter().map(|l| l.audit_exempt_reasoning_tokens as u64).sum(),
        dup_block_tokens: logs.iter().map(|l| l.audit_dup_block_tokens as u64).sum(),
        dup_block_count: logs.iter().map(|l| l.audit_dup_block_count as u64).sum(),
        cache_creation_tokens: logs.iter().map(|l| l.prompt_cache_creation_tokens as u64).sum(),
        cache_read_tokens: total_cache_hit_tokens,
        cache_miss_tokens: total_prompt_tokens.saturating_sub(total_cache_hit_tokens),
        input_tokens: total_prompt_tokens,
        applied_saved_tokens: total_strip_saved_tokens
            + total_trim_saved_tokens
            + total_resp_cache_saved_tokens,
        applied_strip_toolcall_tokens: logs
            .iter()
            .map(|l| l.strip_toolcall_saved_tokens as u64)
            .sum(),
        applied_strip_tokens: total_strip_saved_tokens
            .saturating_sub(logs.iter().map(|l| l.strip_toolcall_saved_tokens as u64).sum()),
        applied_trim_tokens: total_trim_saved_tokens,
        applied_resp_cache_tokens: total_resp_cache_saved_tokens,
        strip_toolcall_requests: logs
            .iter()
            .filter(|l| l.strip_toolcall_saved_tokens > 0)
            .count() as u64,
        // 省量费用: 按各模型生效的 input 单价折算, 与 log_cost / total_opt_saved_fee 同口径.
        applied_strip_fee: sum_saved_fee(&memo, logs, |l| {
            (l.strip_saved_tokens as u64).saturating_sub(l.strip_toolcall_saved_tokens as u64)
        }),
        applied_strip_toolcall_fee: sum_saved_fee(&memo, logs, |l| {
            l.strip_toolcall_saved_tokens as u64
        }),
        applied_trim_fee: sum_saved_fee(&memo, logs, |l| l.trim_saved_tokens as u64),
        applied_resp_cache_fee: sum_saved_fee(&memo, logs, |l| l.resp_cache_saved_tokens as u64),
        cache_saved_fee: total_cache_saved,
        requests: logs.iter().filter(|l| !l.cached).count() as u64,
        sampled_requests: logs.iter().filter(|l| l.audit_observed).count() as u64,
        estimated_requests: logs.iter().filter(|l| l.usage_estimated).count() as u64,
        window_start: logs.iter().map(|l| l.timestamp).min().unwrap_or(0),
        window_end: logs.iter().map(|l| l.timestamp).max().unwrap_or(0),
        sampled_input_tokens: logs
            .iter()
            .filter(|l| l.audit_observed)
            .map(|l| l.prompt_tokens as u64)
            .sum(),
        output: {
            let non_empty = logs.iter().filter(|l| l.completion_tokens > 0);
            let long: Vec<&RequestLog> = logs
                .iter()
                .filter(|l| l.completion_tokens > LONG_OUTPUT_TOKENS)
                .collect();
            OutputAudit {
                output_tokens: logs.iter().map(|l| l.completion_tokens as u64).sum(),
                requests: non_empty.count() as u64,
                long_requests: long.len() as u64,
                long_tokens: long.iter().map(|l| l.completion_tokens as u64).sum(),
                max_tokens: logs.iter().map(|l| l.completion_tokens).max().unwrap_or(0),
                loop_truncated: logs
                    .iter()
                    .filter(|l| {
                        l.error
                            .as_deref()
                            .map(|e| e.contains("model loop detected"))
                            .unwrap_or(false)
                    })
                    .count() as u64,
            }
        },
    };
    let total_opt_saved_fee: f64 = logs
        .iter()
        .map(|l| {
            let opt = (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) as u64;
            if opt == 0 {
                return 0.0;
            }
            match memo.resolve(l) {
                // 按该请求的缓存命中比例加权折算, 见 saved_tokens_fee.
                Some(p) => saved_tokens_fee(
                    p,
                    l.timestamp,
                    opt,
                    l.prompt_tokens as u64,
                    l.prompt_cache_hit_tokens as u64,
                ),
                None => 0.0,
            }
        })
        .sum();
    // 命中率口径: 命中 / 总输入 token (与 opencode-visual-cache 一致: 缓存读 / prompt_tokens).
    // 分母用总输入而非 hit+miss: creation(首次写入) 已从 miss 拆出, hit+miss = prompt - creation 会偏小.
    let cache_hit_rate = if total_prompt_tokens > 0 {
        total_cache_hit_tokens as f64 / total_prompt_tokens as f64
    } else {
        0.0
    };

    // ─── 概览窗口聚合 (随 range 切换: today=当日 / hour=近24h / day=近30天 / month=近12月) ───
    // ─── 今日窗口聚合 (东八区 0 点起) ───
    // 日志文件有 5000 条滚动上限, "累计"口径会封顶失真; 今日口径不受影响, 用于概览小卡片.
    let now = now_ts() as i64;
    let today_start = ((now + TZ_OFFSET_SECS) / 86400 * 86400 - TZ_OFFSET_SECS) as u64;
    let today_logs: Vec<&RequestLog> = logs.iter().filter(|l| l.timestamp >= today_start).collect();
    let today_requests = today_logs.len();
    let today_errors = today_logs.iter().filter(|l| is_log_error(l)).count();
    let today_success = today_requests.saturating_sub(today_errors);
    let today_avg_latency_ms = if today_requests > 0 {
        today_logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / today_requests as f64
    } else {
        0.0
    };
    // 用 u64 累加: 单日 5000 条大上下文请求可轻易超过 u32 上限 (release 下静默回绕).
    let today_total_prompt_tokens: u64 = today_logs.iter().map(|l| l.prompt_tokens as u64).sum();
    let today_total_completion_tokens: u64 = today_logs.iter().map(|l| l.completion_tokens as u64).sum();
    let today_total_cache_hit_tokens: u64 = today_logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
    let today_total_cache_miss_tokens: u64 = today_logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
    let today_total_cost: f64 = today_logs.iter().map(|l| log_cost(&memo, l)).sum();
    // 今日窗口的转发优化省量 (剥离推理链 + 历史裁剪 + 响应缓存命中): token 与折算费用,
    // 与 today_total_cost 同口径 (东八区日界), 供概览头条卡片展示, 不再永远累计.
    let today_strip_saved_tokens: u64 = today_logs.iter().map(|l| l.strip_saved_tokens as u64).sum();
    let today_trim_saved_tokens: u64 = today_logs.iter().map(|l| l.trim_saved_tokens as u64).sum();
    let today_resp_cache_saved_tokens: u64 = today_logs.iter().map(|l| l.resp_cache_saved_tokens as u64).sum();
    // 今日优化省量 = 三者今日窗口之和 (与今日 6 卡片同口径, 东八区日界).
    let today_opt_saved_tokens: u64 = today_strip_saved_tokens + today_trim_saved_tokens + today_resp_cache_saved_tokens;
    let today_opt_saved_fee: f64 = today_logs
        .iter()
        .map(|l| {
            let opt = (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) as u64;
            if opt == 0 {
                return 0.0;
            }
            match memo.resolve(l) {
                // 与 total_opt_saved_fee / 请求详情同口径: 按命中比例加权, 见 saved_tokens_fee.
                Some(p) => saved_tokens_fee(
                    p,
                    l.timestamp,
                    opt,
                    l.prompt_tokens as u64,
                    l.prompt_cache_hit_tokens as u64,
                ),
                None => 0.0,
            }
        })
        .sum();
    // 累计优化省量的最早记录日期 (取有优化省量日志的最早时间戳), 用于给"累计"标注起算点.
    let opt_saved_since = logs
        .iter()
        .filter(|l| (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) > 0)
        .map(|l| l.timestamp)
        .min()
        .map(ts_to_date)
        .unwrap_or_default();

    // 本月窗口聚合 (东八区月首0点起): 月优化省量 = 三者本月之和.
    let month_start = bucket_start(now_ts(), "month");
    let month_logs: Vec<&RequestLog> = logs.iter().filter(|l| l.timestamp >= month_start).collect();
    let month_opt_saved_tokens: u64 = month_logs
        .iter()
        .map(|l| (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) as u64)
        .sum();
    // 月度省量折算费用: 与 today_opt_saved_fee / total_opt_saved_fee 同一折算方式.
    let month_opt_saved_fee: f64 = sum_saved_fee(&memo, &month_logs.iter().map(|l| (*l).clone()).collect::<Vec<_>>(), |l| {
        (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) as u64
    });

    // 近 30 天每日优化省量序列 (末位=今日), 用于头条卡片 sparkline.
    // 按本地日界分桶: 桶 key = 当日0点 ts; 遍历所有日志累加当日省量, 再按日界补齐最近30天空桶.
    let mut day_buckets: BTreeMap<u64, u64> = BTreeMap::new();
    for l in logs.iter() {
        let day_start = bucket_start(l.timestamp, "day");
        let saved = (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) as u64;
        *day_buckets.entry(day_start).or_insert(0) += saved;
    }
    let mut opt_saved_series: Vec<u64> = Vec::with_capacity(30);
    let mut ts = bucket_start(now_ts(), "day");
    for _ in 0..30 {
        opt_saved_series.push(*day_buckets.get(&ts).unwrap_or(&0));
        // 向前推一天 (day 粒度)
        ts = ts.saturating_sub(86400);
    }
    opt_saved_series.reverse(); // 末位=今日, 首位=30天前

    // 按"供应商/上游模型"组合分组: 上游模型缺失时回退到中转 model, 避免按中转 ID 统计造成的混乱.
    // 过滤 provider 为空或 "-" 的占位日志 (模型未找到/解析失败时的 404 占位), 避免模型明细出现 "-/xxx" 幽灵条目.
    let mut model_map: HashMap<String, Vec<&RequestLog>> = HashMap::new();
    for log in logs {
        if log.provider.is_empty() || log.provider == "-" {
            continue;
        }
        let effective = log
            .upstream_model
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| log.model.clone());
        let key = format!("{}/{}", log.provider, effective);
        model_map.entry(key).or_default().push(log);
    }
    let mut per_model: Vec<ModelStats> = model_map
        .into_iter()
        .map(|(_model, logs)| {
            let reqs = logs.len();
            let errs = logs.iter().filter(|l| is_log_error(l)).count();
            let bytes: usize = logs.iter().map(|l| l.body_len).sum();
            let avg = logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / reqs as f64;
            let pt: u64 = logs.iter().map(|l| l.prompt_tokens as u64).sum();
            let ct: u64 = logs.iter().map(|l| l.completion_tokens as u64).sum();
            let hit: u64 = logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
            let miss: u64 = logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
            let cost: f64 = logs.iter().map(|l| log_cost(&memo, l)).sum();
            // 纯生成吐字速度: 仅累计首 token 延迟已知的流式请求 (ft<总耗时 且 有输出 token),
            // gen_ms = 总耗时 - 首 token 延迟, 排除排队与 TTFT.
            let (gen_ct, gen_ms): (u64, u64) = logs
                .iter()
                .filter_map(|l| {
                    l.first_token_ms
                        .filter(|&ft| ft < l.latency_ms && l.completion_tokens > 0)
                        .map(|ft| (l.completion_tokens as u64, l.latency_ms - ft))
                })
                .fold((0u64, 0u64), |(ac, am), (c, m)| (ac + c, am + m));
            let gen_speed = if gen_ms > 0 {
                gen_ct as f64 / gen_ms as f64 * 1000.0
            } else {
                0.0
            };
            // 中转 ID 集合 (去重, 排序) — 便于对照"该上游模型由哪些中转 ID 映射而来".
            let mut aliases: Vec<String> = logs
                .iter()
                .map(|l| l.model.clone())
                .collect::<std::collections::HashSet<_>>()
                .into_iter()
                .collect();
            aliases.sort();

            // 单价取组内首条日志解析的结果（仅面板配置的价格）,
            // 供前端单价列展示.
            let price = pricing::resolve_price(price_table.lookup(&logs[0].provider, &logs[0].model));

            let provider_name = logs[0].provider.clone();
            let upstream_name = logs[0]
                .upstream_model
                .clone()
                .filter(|name| !name.is_empty())
                .unwrap_or_else(|| logs[0].model.clone());
            ModelStats {
                model: upstream_name.clone(),
                provider: provider_name,
                upstream_model: upstream_name,
                requests: reqs,
                errors: errs,
                avg_latency_ms: avg,
                total_body_bytes: bytes,
                total_prompt_tokens: pt,
                total_completion_tokens: ct,
                total_cache_hit_tokens: hit,
                total_cache_miss_tokens: miss,
                gen_speed,
                gen_output_tokens: gen_ct,
                gen_time_sum_ms: gen_ms,
                aliases,
                total_cost: cost,
                price,
                free: logs.iter().any(|l| free_ids.contains(&l.model)),
            }
        })
        .collect();
    per_model.sort_by(|a, b| b.requests.cmp(&a.requests));

    // 按供应商分组 (过滤空值和 "-" 占位符: 请求体解析失败/模型路由未找到时 provider 硬编码为 "-").
    let mut prov_map: HashMap<&str, Vec<&RequestLog>> = HashMap::new();
    for log in logs {
        let provider = if log.provider.is_empty() || log.provider == "-" {
            continue;
        } else {
            &log.provider
        };
        prov_map.entry(provider).or_default().push(log);
    }
    let mut per_provider: Vec<ProviderStats> = prov_map
        .into_iter()
        .map(|(provider, logs)| {
            let reqs = logs.len();
            let errs = logs.iter().filter(|l| is_log_error(l)).count();
            let bytes: usize = logs.iter().map(|l| l.body_len).sum();
            let avg = logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / reqs as f64;
            let pt: u64 = logs.iter().map(|l| l.prompt_tokens as u64).sum();
            let ct: u64 = logs.iter().map(|l| l.completion_tokens as u64).sum();
            let hit: u64 = logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
            let miss: u64 = logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
            let cost: f64 = logs.iter().map(|l| log_cost(&memo, l)).sum();
            // KV 缓存命中节省金额: 仅计费 (非免费/月套餐) 供应商计入, 排除不按量计费的模型.
            let cache_saved: f64 = logs
                .iter()
                .filter(|l| !free_ids.contains(&l.model))
                .map(|l| log_cache_saved(&memo, l))
                .sum();
            // 是否按量计费: 组内任一中转 ID 不在 free_ids (免费/月套餐) 即视为计费供应商.
            let billing = logs.iter().any(|l| !free_ids.contains(&l.model));
            let today_cost: f64 = logs
                .iter()
                .filter(|l| l.timestamp >= today_start)
                .map(|l| log_cost(&memo, l))
                .sum();
            // 今日窗口转发优化省量 (剥离推理链 + 历史裁剪 + 响应缓存命中), 与今日卡片同口径 (东八区日界).
            let today_strip_saved_tokens: u64 = logs
                .iter()
                .filter(|l| l.timestamp >= today_start)
                .map(|l| l.strip_saved_tokens as u64)
                .sum();
            let today_trim_saved_tokens: u64 = logs
                .iter()
                .filter(|l| l.timestamp >= today_start)
                .map(|l| l.trim_saved_tokens as u64)
                .sum();
            let today_resp_cache_saved_tokens: u64 = logs
                .iter()
                .filter(|l| l.timestamp >= today_start)
                .map(|l| l.resp_cache_saved_tokens as u64)
                .sum();
            let today_opt_saved_tokens =
                today_strip_saved_tokens + today_trim_saved_tokens + today_resp_cache_saved_tokens;
            // 纯生成吐字速度, 同 per_model 口径.
            let (gen_ct, gen_ms): (u64, u64) = logs
                .iter()
                .filter_map(|l| {
                    l.first_token_ms
                        .filter(|&ft| ft < l.latency_ms && l.completion_tokens > 0)
                        .map(|ft| (l.completion_tokens as u64, l.latency_ms - ft))
                })
                .fold((0u64, 0u64), |(ac, am), (c, m)| (ac + c, am + m));
            let gen_speed = if gen_ms > 0 {
                gen_ct as f64 / gen_ms as f64 * 1000.0
            } else {
                0.0
            };
            ProviderStats {
                provider: provider.to_string(),
                requests: reqs,
                errors: errs,
                avg_latency_ms: avg,
                total_body_bytes: bytes,
                total_prompt_tokens: pt,
                total_completion_tokens: ct,
                total_cache_hit_tokens: hit,
                total_cache_miss_tokens: miss,
                gen_speed,
                gen_output_tokens: gen_ct,
                gen_time_sum_ms: gen_ms,
                total_cost: cost,
                today_cost,
                cache_saved,
                billing,
                today_opt_saved_tokens,
                today_strip_saved_tokens,
                today_trim_saved_tokens,
                today_resp_cache_saved_tokens,
            }
        })
        .collect();
    per_provider.sort_by(|a, b| b.requests.cmp(&a.requests));

    // 按天分组趋势 (默认)
    let trends = compute_trends(logs, "day", price_table);
    let model_trends = compute_model_trends(logs, "day");

    // Top 5 模型
    let top_models: Vec<ModelStats> = per_model.iter().take(5).cloned().collect();

    UsageStats {
        total_requests: total,
        success_count,
        error_count,
        total_body_bytes,
        avg_latency_ms,
        total_prompt_tokens,
        total_completion_tokens,
        per_model,
        per_provider,
        trends,
        model_trends,
        top_models,
        total_cache_hit_tokens,
        total_cache_miss_tokens,
        cache_hit_rate,
        today_requests,
        today_success,
        today_errors,
        today_avg_latency_ms,
        today_total_prompt_tokens,
        today_total_completion_tokens,
        today_total_cache_hit_tokens,
        today_total_cache_miss_tokens,
        total_cost,
        total_cache_saved,
        total_opt_saved_tokens,
        total_opt_saved_fee,
        total_strip_saved_tokens,
        total_trim_saved_tokens,
        total_resp_cache_saved_tokens,
        audit,
        month_opt_saved_tokens,
        month_opt_saved_fee,
        opt_saved_series,
        today_total_cost,
        today_opt_saved_tokens,
        today_opt_saved_fee,
        today_strip_saved_tokens,
        today_trim_saved_tokens,
        today_resp_cache_saved_tokens,
        opt_saved_since,
        // has_price_config 在 api_stats 中按 price_overrides 是否非空注入.
        has_price_config: false,
        // 本轮 (进程级) 统计由 LogBuffer 原子计数提供, compute_stats 内无来源 → 默认空,
        // api_stats 返回前会覆盖为 log_buffer.session_stats().
        session: SessionStats::default(),
        gen_speed,
        gen_samples,
        gen_output_tokens: stats_gen_output_tokens,
        gen_time_sum_ms: stats_gen_time_sum_ms,
        window_start: logs.iter().map(|log| log.timestamp).min().unwrap_or_else(now_ts),
        window_end: now_ts(),
        window_minutes: 1.0,
        source_window: "当前运行日志窗口".to_string(),
    }
}

/// 解析 rollup 条目的生效价格: 中转 ID 覆盖优先 (组内任一别名命中即用), 否则按上游模型回退内置表.
/// 与日志口径 (memo.resolve 按 relay id 查面板配置的价格) 语义一致.
fn entry_price(
    price_table: &PriceTable,
    e: &crate::rollup::RollupEntry,
) -> Option<ModelPrice> {
    // 优先用条目自带的价格快照 —— 历史账单在记录时就已结算, 不随之后的改价/删模型变动.
    // (删除模型配置会让旧口径下的历史费用整片归零, 快照正是为此而存.)
    if let Some(snap) = e.price {
        return Some(snap);
    }
    // 以下为旧数据 (无快照) 的回退: 优先用 entry 自带的供应商做精确匹配 ——
    // 同名 model_id 在不同供应商下价格可能不同, 只取"第一个能匹配到的别名"会把整条按错价记账.
    if !e.provider.is_empty() && e.provider != "-" {
        for alias in &e.aliases {
            if let Some(p) = price_table
                .by_provider_model
                .get(&(e.provider.clone(), alias.clone()))
            {
                return Some(*p);
            }
        }
    }
    // 回退: 全局唯一价 (该 model_id 在所有供应商下同价, 或 provider 缺失的老数据).
    for alias in &e.aliases {
        if let Some(p) = price_table.by_model_unique.get(alias) {
            return Some(*p);
        }
    }
    None
}

/// rollup 条目费用 (元): 按高峰/空闲拆分的计费 token 以当前费率重算.
/// 与逐条 `log_cost` 求和完全等价 (费用对 token 线性, 分段求和可交换).
fn entry_cost(p: ModelPrice, e: &crate::rollup::RollupEntry) -> f64 {
    let (peak, offpeak) = pricing::effective_parts(p);
    let creation_price = p.cache_creation_per_m;
    // 与 log_cost 同口径: 输入拆三档 (命中 / 首次写入 / 其余), 写入用 cache_creation 价.
    let part = |(ip, op, cp): (f64, f64, f64), prompt: u64, completion: u64, hit: u64, creation: u64| -> f64 {
        let prompt = prompt as f64;
        let hit = (hit as f64).min(prompt);
        let creation = (creation as f64).min((prompt - hit).max(0.0));
        let miss = (prompt - hit - creation).max(0.0);
        let cprice = creation_price.unwrap_or(ip);
        hit / 1e6 * cp + creation / 1e6 * cprice + miss / 1e6 * ip + completion as f64 / 1e6 * op
    };
    part(peak, e.bill_prompt_peak, e.bill_completion_peak, e.bill_hit_peak, e.bill_creation_peak)
        + part(offpeak, e.bill_prompt_offpeak, e.bill_completion_offpeak, e.bill_hit_offpeak, e.bill_creation_offpeak)
}

/// rollup 条目 KV 缓存净省金额 (元), 口径与 `log_cache_saved` 一致
/// (净省 = 命中折扣 − 写入溢价; 输入价 0 → 无计费基数记 0; 不钳制到 0).
fn entry_cache_saved(p: ModelPrice, e: &crate::rollup::RollupEntry) -> f64 {
    let (peak, offpeak) = pricing::effective_parts(p);
    let part = |(ip, _op, cp): (f64, f64, f64), hit: u64, creation: u64| -> f64 {
        if ip == 0.0 {
            return 0.0;
        }
        let read_saved = (ip - cp).max(0.0);
        let creation_price = p.cache_creation_per_m.unwrap_or(ip);
        let penalty = (creation_price - ip).max(0.0);
        hit as f64 / 1e6 * read_saved - creation as f64 / 1e6 * penalty
    };
    part(peak, e.bill_hit_peak, e.bill_creation_peak)
        + part(offpeak, e.bill_hit_offpeak, e.bill_creation_offpeak)
}

/// 将日级 rollup 历史合并进统计结果 (月视图等长跨度查询).
///
/// `days` 与日志来源按天严格互斥 (仅含日志滚动窗口之外、且今天之前的天), 各项直接累加:
/// 总量/趋势取并集, 模型与供应商表按键合并后重算均值与吐字速度.
/// 费用/省量金额由拆分存储的计费 token 以当前费率重算, 改价可追溯历史 (与日志口径一致).
/// 趋势合并只累加到日志侧已补齐的窗口桶, 不创建新桶 (防止桶数量爆炸冻结前端).
fn merge_rollup_days(
    stats: &mut UsageStats,
    days: &[crate::rollup::DailyRollup],
    granularity: &str,
    price_table: &PriceTable,
    free_ids: &std::collections::HashSet<String>,
) {
    if days.is_empty() {
        return;
    }
    let now = now_ts() as i64;
    let today_start = ((now + TZ_OFFSET_SECS) / 86400 * 86400 - TZ_OFFSET_SECS) as u64;
    let month_start = bucket_start(now as u64, "month");

    // ── 总量累加 (含 "-" 占位条目, 与 compute_stats 总量口径一致) ──
    let mut total_requests = 0usize;
    let mut error_count = 0usize;
    let mut total_body_bytes = 0usize;
    let mut lat_sum_ms = 0u64;
    let mut total_prompt_tokens = 0u64;
    let mut total_completion_tokens = 0u64;
    let mut total_cache_hit_tokens = 0u64;
    let mut total_cache_miss_tokens = 0u64;
    let mut total_cost = 0f64;
    let mut total_cache_saved = 0f64;
    let mut total_opt_saved_tokens = 0u64;
    let mut total_opt_saved_fee = 0f64;
    let mut total_strip = 0u64;
    let mut total_trim = 0u64;
    let mut total_resp = 0u64;
    let mut gen_output = 0u64;
    let mut gen_ms = 0u64;
    let mut gen_samples = 0usize;
    let mut month_opt_saved_tokens = 0u64;
    let mut saved_since_day: Option<u64> = None;
    for day in days {
        for e in &day.entries {
            total_requests += e.requests as usize;
            error_count += e.errors as usize;
            total_body_bytes += e.body_bytes as usize;
            lat_sum_ms += e.latency_sum_ms;
            total_prompt_tokens += e.prompt_tokens;
            total_completion_tokens += e.completion_tokens;
            total_cache_hit_tokens += e.cache_hit_tokens;
            total_cache_miss_tokens += e.cache_miss_tokens;
            gen_output += e.gen_output_tokens;
            gen_ms += e.gen_time_sum_ms;
            gen_samples += e.gen_samples as usize;
            let saved = e.strip_saved_tokens + e.trim_saved_tokens + e.resp_cache_saved_tokens;
            total_strip += e.strip_saved_tokens;
            total_trim += e.trim_saved_tokens;
            total_resp += e.resp_cache_saved_tokens;
            total_opt_saved_tokens += saved;
            if day.day_start >= month_start {
                month_opt_saved_tokens += saved;
            }
            if saved > 0 && saved_since_day.map_or(true, |d| day.day_start < d) {
                saved_since_day = Some(day.day_start);
            }
            // 金额: 价格缺失按 0 计 (与 log_cost 同口径).
            if let Some(p) = entry_price(price_table, e) {
                total_cost += entry_cost(p, e);
                if !e.aliases.iter().any(|a| free_ids.contains(a)) {
                    total_cache_saved += entry_cache_saved(p, e);
                }
                // 优化省量折费: 与日志侧同口径 (见 saved_tokens_fee) —— 条目级用整条的
                // 缓存命中比例做加权, 高峰/空闲两段各按自己的费率折算.
                // input 价为 0 (免费/显式配 0) 时不折算, 与旧口径一致.
                if p.input_per_m > 0.0 {
                    let (peak_part, offpeak_part) = pricing::effective_parts(p);
                    let hit_ratio = cache_hit_ratio(e.prompt_tokens, e.cache_hit_tokens);
                    let peak_saved = e.saved_peak_tokens.min(saved);
                    let offpeak_saved = saved.saturating_sub(peak_saved);
                    total_opt_saved_fee += saved_fee_weighted(peak_part, peak_saved as f64, hit_ratio)
                        + saved_fee_weighted(offpeak_part, offpeak_saved as f64, hit_ratio);
                }
            }
        }
    }

    // 写回总量 (加权平均重算均值/吐字速度).
    let new_total = stats.total_requests + total_requests;
    stats.avg_latency_ms = (stats.avg_latency_ms * stats.total_requests as f64 + lat_sum_ms as f64)
        / new_total.max(1) as f64;
    stats.total_requests = new_total;
    stats.success_count += total_requests - error_count;
    stats.error_count += error_count;
    stats.total_body_bytes += total_body_bytes;
    stats.total_prompt_tokens += total_prompt_tokens;
    stats.total_completion_tokens += total_completion_tokens;
    stats.total_cache_hit_tokens += total_cache_hit_tokens;
    stats.total_cache_miss_tokens += total_cache_miss_tokens;
    stats.cache_hit_rate = if stats.total_prompt_tokens > 0 {
        stats.total_cache_hit_tokens as f64 / stats.total_prompt_tokens as f64
    } else {
        0.0
    };
    stats.total_cost += total_cost;
    stats.total_cache_saved += total_cache_saved;
    stats.total_opt_saved_tokens += total_opt_saved_tokens;
    stats.total_opt_saved_fee += total_opt_saved_fee;
    stats.total_strip_saved_tokens += total_strip;
    stats.total_trim_saved_tokens += total_trim;
    stats.total_resp_cache_saved_tokens += total_resp;
    stats.month_opt_saved_tokens += month_opt_saved_tokens;
    stats.gen_output_tokens += gen_output;
    stats.gen_time_sum_ms += gen_ms;
    stats.gen_samples += gen_samples;
    stats.gen_speed = if stats.gen_time_sum_ms > 0 {
        stats.gen_output_tokens as f64 / stats.gen_time_sum_ms as f64 * 1000.0
    } else {
        0.0
    };
    // 合并天严格早于所有日志天 → 省量起算点取合并侧 (若有).
    if let Some(d) = saved_since_day {
        stats.opt_saved_since = ts_to_date(d);
    }
    // 近 30 天省量序列: 合并天在窗口内的槽位直接覆写 (日志侧对这些天本无数据).
    for day in days {
        if day.day_start > today_start {
            continue;
        }
        let ago = (today_start - day.day_start) / 86400;
        if ago < 30 {
            let idx = 29 - ago as usize;
            let saved: u64 = day
                .entries
                .iter()
                .map(|e| e.strip_saved_tokens + e.trim_saved_tokens + e.resp_cache_saved_tokens)
                .sum();
            if idx < stats.opt_saved_series.len() {
                stats.opt_saved_series[idx] = saved;
            }
        }
    }

    // ── 模型表合并: 按 (供应商, 上游模型) 累加, 重算均值/吐字速度 ──
    for day in days {
        for e in &day.entries {
            if e.provider.is_empty() || e.provider == "-" {
                continue;
            }
            let entry_free = e.aliases.iter().any(|a| free_ids.contains(a));
            let cost = entry_price(price_table, e).map(|p| entry_cost(p, e)).unwrap_or(0.0);
            if let Some(m) = stats
                .per_model
                .iter_mut()
                .find(|m| m.provider == e.provider && m.upstream_model == e.upstream)
            {
                let new_reqs = m.requests + e.requests as usize;
                m.avg_latency_ms = (m.avg_latency_ms * m.requests as f64 + e.latency_sum_ms as f64)
                    / new_reqs.max(1) as f64;
                m.requests = new_reqs;
                m.errors += e.errors as usize;
                m.total_body_bytes += e.body_bytes as usize;
                m.total_prompt_tokens += e.prompt_tokens;
                m.total_completion_tokens += e.completion_tokens;
                m.total_cache_hit_tokens += e.cache_hit_tokens;
                m.total_cache_miss_tokens += e.cache_miss_tokens;
                m.gen_output_tokens += e.gen_output_tokens;
                m.gen_time_sum_ms += e.gen_time_sum_ms;
                m.gen_speed = if m.gen_time_sum_ms > 0 {
                    m.gen_output_tokens as f64 / m.gen_time_sum_ms as f64 * 1000.0
                } else {
                    0.0
                };
                for a in &e.aliases {
                    if !m.aliases.contains(a) {
                        m.aliases.push(a.clone());
                    }
                }
                m.aliases.sort();
                m.total_cost += cost;
                m.free = m.free || entry_free;
            } else {
                let price = entry_price(price_table, e);
                stats.per_model.push(ModelStats {
                    model: e.upstream.clone(),
                    provider: e.provider.clone(),
                    upstream_model: e.upstream.clone(),
                    requests: e.requests as usize,
                    errors: e.errors as usize,
                    avg_latency_ms: if e.requests > 0 {
                        e.latency_sum_ms as f64 / e.requests as f64
                    } else {
                        0.0
                    },
                    total_body_bytes: e.body_bytes as usize,
                    total_prompt_tokens: e.prompt_tokens,
                    total_completion_tokens: e.completion_tokens,
                    total_cache_hit_tokens: e.cache_hit_tokens,
                    total_cache_miss_tokens: e.cache_miss_tokens,
                    gen_speed: if e.gen_time_sum_ms > 0 {
                        e.gen_output_tokens as f64 / e.gen_time_sum_ms as f64 * 1000.0
                    } else {
                        0.0
                    },
                    gen_output_tokens: e.gen_output_tokens,
                    gen_time_sum_ms: e.gen_time_sum_ms,
                    aliases: {
                        let mut a = e.aliases.clone();
                        a.sort();
                        a
                    },
                    total_cost: cost,
                    price,
                    free: entry_free,
                });
            }
        }
    }
    stats.per_model.sort_by(|a, b| b.requests.cmp(&a.requests));
    stats.top_models = stats.per_model.iter().take(5).cloned().collect();

    // ── 供应商表合并 ──
    for day in days {
        for e in &day.entries {
            if e.provider.is_empty() || e.provider == "-" {
                continue;
            }
            let entry_free = e.aliases.iter().any(|a| free_ids.contains(a));
            let price = entry_price(price_table, e);
            let cost = price.map(|p| entry_cost(p, e)).unwrap_or(0.0);
            let cache_saved = price
                .filter(|_| !entry_free)
                .map(|p| entry_cache_saved(p, e))
                .unwrap_or(0.0);
            if let Some(pv) = stats.per_provider.iter_mut().find(|p| p.provider == e.provider) {
                let new_reqs = pv.requests + e.requests as usize;
                pv.avg_latency_ms = (pv.avg_latency_ms * pv.requests as f64 + e.latency_sum_ms as f64)
                    / new_reqs.max(1) as f64;
                pv.requests = new_reqs;
                pv.errors += e.errors as usize;
                pv.total_body_bytes += e.body_bytes as usize;
                pv.total_prompt_tokens += e.prompt_tokens;
                pv.total_completion_tokens += e.completion_tokens;
                pv.total_cache_hit_tokens += e.cache_hit_tokens;
                pv.total_cache_miss_tokens += e.cache_miss_tokens;
                pv.gen_output_tokens += e.gen_output_tokens;
                pv.gen_time_sum_ms += e.gen_time_sum_ms;
                pv.gen_speed = if pv.gen_time_sum_ms > 0 {
                    pv.gen_output_tokens as f64 / pv.gen_time_sum_ms as f64 * 1000.0
                } else {
                    0.0
                };
                pv.total_cost += cost;
                pv.cache_saved += cache_saved;
                pv.billing = pv.billing || !entry_free;
            } else {
                stats.per_provider.push(ProviderStats {
                    provider: e.provider.clone(),
                    requests: e.requests as usize,
                    errors: e.errors as usize,
                    avg_latency_ms: if e.requests > 0 {
                        e.latency_sum_ms as f64 / e.requests as f64
                    } else {
                        0.0
                    },
                    total_body_bytes: e.body_bytes as usize,
                    total_prompt_tokens: e.prompt_tokens,
                    total_completion_tokens: e.completion_tokens,
                    total_cache_hit_tokens: e.cache_hit_tokens,
                    total_cache_miss_tokens: e.cache_miss_tokens,
                    gen_speed: if e.gen_time_sum_ms > 0 {
                        e.gen_output_tokens as f64 / e.gen_time_sum_ms as f64 * 1000.0
                    } else {
                        0.0
                    },
                    gen_output_tokens: e.gen_output_tokens,
                    gen_time_sum_ms: e.gen_time_sum_ms,
                    total_cost: cost,
                    today_cost: 0.0,
                    cache_saved,
                    billing: !entry_free,
                    today_opt_saved_tokens: 0,
                    today_strip_saved_tokens: 0,
                    today_trim_saved_tokens: 0,
                    today_resp_cache_saved_tokens: 0,
                });
            }
        }
    }
    stats.per_provider.sort_by(|a, b| b.requests.cmp(&a.requests));

    // ── 趋势合并: 仅向日志侧已补齐的窗口桶累加, 不创建新桶 ──
    let mut buckets: BTreeMap<u64, DailyTrend> =
        std::mem::take(&mut stats.trends).into_iter().map(|t| (t.ts, t)).collect();
    for day in days {
        let ts = bucket_start(day.day_start, granularity);
        if let Some(b) = buckets.get_mut(&ts) {
            for e in &day.entries {
                let new_reqs = b.requests + e.requests as usize;
                b.avg_latency =
                    (b.avg_latency * b.requests as f64 + e.latency_sum_ms as f64) / new_reqs.max(1) as f64;
                b.requests = new_reqs;
                b.errors += e.errors as usize;
                b.total_prompt_tokens += e.prompt_tokens;
                b.total_completion_tokens += e.completion_tokens;
                b.total_cache_hit_tokens += e.cache_hit_tokens;
                b.total_cache_miss_tokens += e.cache_miss_tokens;
                if let Some(p) = entry_price(price_table, e) {
                    b.total_cost += entry_cost(p, e);
                }
            }
        }
    }
    stats.trends = buckets.into_values().collect();

    // ── 模型趋势合并 (键为 (桶, 供应商, 上游模型)) ──
    stats.model_trends = merge_rollup_model_trends(
        std::mem::take(&mut stats.model_trends),
        days,
        granularity,
    );
}

/// 把 rollup 天的模型维度数据并入模型趋势.
///
/// 历史天在日志侧已被剔除, 其模型桶不存在, 因此必须能「新建」桶;
/// 否则 rollup 的模型趋势会被静默丢弃 (长跨度图表缺历史曲线).
fn merge_rollup_model_trends(
    existing: Vec<ModelTrend>,
    days: &[crate::rollup::DailyRollup],
    granularity: &str,
) -> Vec<ModelTrend> {
    let mut mt: BTreeMap<(u64, String, String), ModelTrend> = existing
        .into_iter()
        .map(|t| ((t.ts, t.provider.clone(), t.upstream_model.clone()), t))
        .collect();
    let key_fn = trend_key_fn(granularity);
    for day in days {
        let ts = bucket_start(day.day_start, granularity);
        for e in &day.entries {
            if e.provider.is_empty() || e.provider == "-" {
                continue;
            }
            let t = mt
                .entry((ts, e.provider.clone(), e.upstream.clone()))
                .or_insert_with(|| ModelTrend {
                    date: key_fn(ts),
                    ts,
                    provider: e.provider.clone(),
                    upstream_model: e.upstream.clone(),
                    prompt_tokens: 0,
                    completion_tokens: 0,
                    requests: 0,
                    errors: 0,
                });
            t.prompt_tokens += e.prompt_tokens;
            t.completion_tokens += e.completion_tokens;
            t.requests += e.requests as usize;
            t.errors += e.errors as usize;
        }
    }
    mt.into_values().collect()
}

/// 按指定粒度聚合趋势数据.

///
/// 关键: 聚合后按粒度补齐**固定窗口**的空桶 (hour=24 整点 / day=最近30天 / month=最近12个月),
/// 使时间轴严格对齐且递增 —— 最新桶=当前粒度边界, 最旧桶=窗口起点 (含无请求空段),
/// 避免"只显示有数据的桶导致段数不定、时间轴不连续/不对应"的问题.
fn compute_trends(
    logs: &[RequestLog],
    granularity: &str,
    price_table: &PriceTable,
) -> Vec<DailyTrend> {
    compute_trends_window(logs, granularity, price_table, None, None)
}

fn compute_trends_window(
    logs: &[RequestLog],
    granularity: &str,
    price_table: &PriceTable,
    explicit_start: Option<u64>,
    explicit_end: Option<u64>,
) -> Vec<DailyTrend> {
    // 趋势桶内每条日志仍走 log_cost, 复用记忆化避免重复解析价格.
    let memo = PriceMemo::new(price_table);
    let key_fn: fn(u64) -> String = match granularity {
        "hour" => ts_to_hour,
        "month" => ts_to_month,
        _ => ts_to_date,
    };

    let mut map: BTreeMap<u64, Vec<&RequestLog>> = BTreeMap::new();
    for log in logs {
        let ts = bucket_start(log.timestamp, granularity);
        map.entry(ts).or_default().push(log);
    }
    // 直接按桶起始时间戳聚合，避免 MM/DD 或 HH:00 在跨年时碰撞.
    let mut buckets: BTreeMap<u64, DailyTrend> = BTreeMap::new();
    for (ts, logs) in map {
        let reqs = logs.len();
        let date = key_fn(ts);
        let errs = logs.iter().filter(|l| is_log_error(l)).count();
        let avg_lat = logs.iter().map(|l| l.latency_ms).sum::<u64>() as f64 / reqs as f64;
        let hit: u64 = logs.iter().map(|l| l.prompt_cache_hit_tokens as u64).sum();
        let miss: u64 = logs.iter().map(|l| l.prompt_cache_miss_tokens as u64).sum();
        let pt: u64 = logs.iter().map(|l| l.prompt_tokens as u64).sum();
        let ct: u64 = logs.iter().map(|l| l.completion_tokens as u64).sum();
        let cost: f64 = logs.iter().map(|l| log_cost(&memo, l)).sum();
        buckets.insert(
            ts,
            DailyTrend {
                date,
                ts,
                requests: reqs,
                errors: errs,
                avg_latency: avg_lat,
                total_prompt_tokens: pt,
                total_completion_tokens: ct,
                total_cache_hit_tokens: hit,
                total_cache_miss_tokens: miss,
                total_cost: cost,
            },
        );
    }

    // API 显式范围只在当前窗口内补齐；内部默认调用保持固定窗口兼容性.
    let (start, end) = match (explicit_start, explicit_end) {
        (Some(window_start), Some(window_end)) => (
            bucket_start(window_start, granularity),
            bucket_start(window_end.saturating_sub(1), granularity),
        ),
        _ => {
            let window: u64 = match granularity { "hour" => 24, "month" => 12, _ => 30 };
            let end = bucket_start(now_ts(), granularity);
            let mut start = end;
            for _ in 1..window { start = prev_bucket(start, granularity); }
            (start, end)
        }
    };
    let mut ts = start;
    loop {
        buckets.entry(ts).or_insert_with(|| DailyTrend {
            date: key_fn(ts),
            ts,
            requests: 0,
            errors: 0,
            avg_latency: 0.0,
            total_prompt_tokens: 0,
            total_completion_tokens: 0,
            total_cache_hit_tokens: 0,
            total_cache_miss_tokens: 0,
            total_cost: 0.0,
        });
        if ts == end {
            break;
        }
        ts = next_bucket(ts, granularity);
        if ts > end {
            // 安全护栏: 步进越过 end (理论上不会发生) 则停止.
            break;
        }
    }

    buckets.into_values().collect()
}

/// 按模型和时间桶聚合 Token 趋势，供分析页多系列图表使用。
fn compute_model_trends(logs: &[RequestLog], granularity: &str) -> Vec<ModelTrend> {
    compute_model_trends_window(logs, granularity, None, None)
}

/// 一组生效单价三元组 `(input, output, cache_read)` 下, `tokens` 个省量 token 的价值 (元).
///
/// 省量都是**输入侧**的历史内容 (推理链 / 历史轮次), 故只取 input 与 cache_read 两档,
/// 按 `hit_ratio` 加权 —— 见 [`saved_tokens_fee`] 对为何不能一律按 input 价的说明.
fn saved_fee_weighted(part: (f64, f64, f64), tokens: f64, hit_ratio: f64) -> f64 {
    let (input_price, _, cache_read_price) = part;
    tokens / 1e6 * (hit_ratio * cache_read_price + (1.0 - hit_ratio) * input_price)
}

/// 该请求的 KV 缓存命中比例 (0..1); prompt 为 0 时无从判断, 取 0 (按未命中价, 保守偏低).
fn cache_hit_ratio(prompt_tokens: u64, cache_hit_tokens: u64) -> f64 {
    if prompt_tokens == 0 {
        return 0.0;
    }
    (cache_hit_tokens.min(prompt_tokens) as f64 / prompt_tokens as f64).clamp(0.0, 1.0)
}

/// 省下 token 折算费用 (元): 按**该请求自身的 KV 缓存命中比例**, 把省量拆成
/// 「命中区」(cache_read 价) 与「未命中区」(input 价) 两部分加权.
///
/// 为什么不一律按 input 价折算: 实测 agent 工作流的缓存命中率极高 (本机实测
/// commandcodeAI 98.3% / ginka 93.3%), 而 cache_read 价与 input 价相差约 50x
/// (1.0 vs 0.02). 被省掉的推理链与历史轮次绝大多数本就落在缓存命中区, 一律按
/// 未命中价折算会把价值放大约 50 倍. 按请求自身命中结构加权, 得到的是"这些 token
/// 若未被省掉、按其所属请求的缓存结构计费"的期望值, 比单向取任一端都更接近真实节省.
fn saved_tokens_fee(
    p: ModelPrice,
    ts: u64,
    saved_tokens: u64,
    prompt_tokens: u64,
    cache_hit_tokens: u64,
) -> f64 {
    if saved_tokens == 0 {
        return 0.0;
    }
    saved_fee_weighted(
        pricing::effective(p, ts),
        saved_tokens as f64,
        cache_hit_ratio(prompt_tokens, cache_hit_tokens),
    )
}

/// 按各条日志的模型生效单价, 把 `pick` 选出的省量 token 折算为费用 (元).
///
/// 口径与 `compute_stats` 的 `total_opt_saved_fee` 一致 (同走 [`saved_tokens_fee`]);
/// 单价为 0 (未配置/免费) 时记 0. 用于把"已生效省量"拆成各项费用.
fn sum_saved_fee(
    memo: &PriceMemo,
    logs: &[RequestLog],
    pick: impl Fn(&RequestLog) -> u64,
) -> f64 {
    logs.iter()
        .map(|l| {
            let tokens = pick(l);
            if tokens == 0 {
                return 0.0;
            }
            match memo.resolve(l) {
                Some(p) => saved_tokens_fee(
                    p,
                    l.timestamp,
                    tokens,
                    l.prompt_tokens as u64,
                    l.prompt_cache_hit_tokens as u64,
                ),
                None => 0.0,
            }
        })
        .sum()
}

/// 计算 rollup 合并区间与日志侧保留边界.
///
/// 输入 `cover_ts` = 内存日志缓冲区最老一条的时间戳. 返回 `(merge_end, rstart_day)`:
/// 合并 rollup 的 `[rstart_day, merge_end]` 天, 且日志侧只保留 `day > merge_end` 的部分.
/// 返回 `None` 表示无需合并 (查询起点不早于可合并上界).
///
/// 上界取舍:
/// - `cover_day` 早于今天 → 该天 rollup 已完整, 采用它 (日志里只剩尾部), 上界取 `cover_day`;
/// - `cover_day` 就是今天 → 今天的 rollup 仍在累加 (不完整), 不能采用, 上界退到昨天,
///   今天的日志全部保留.
///
/// 旧实现仅在 `cover_day < today` 时才合并, 一旦单日请求数超过日志滚动窗口
/// (缓冲区只含今天) 就完全不合并 rollup, 使 29d/365d 长跨度查询退化为只有今天的数据.
fn rollup_merge_bounds(now: u64, range_start: u64, cover_ts: u64) -> Option<(u64, u64)> {
    let today_start = (((now as i64) + TZ_OFFSET_SECS) / 86400 * 86400 - TZ_OFFSET_SECS) as u64;
    let cover_day = bucket_start(cover_ts, "day");
    let merge_end = if cover_day < today_start {
        cover_day
    } else {
        prev_bucket(cover_day, "day")
    };
    // 下界对齐到查询起点所在天的 0 点, 与 compute_trends_window 首桶口径一致
    // (rollup 是日粒度, 无法只取半天; 不对齐会让范围首日整段丢失).
    let rstart_day = bucket_start(range_start, "day");
    if rstart_day <= merge_end {
        Some((merge_end, rstart_day))
    } else {
        None
    }
}

/// 趋势桶的日期键生成函数: 按粒度选 时/日/月 三种格式.
fn trend_key_fn(granularity: &str) -> fn(u64) -> String {
    match granularity {
        "hour" => ts_to_hour,
        "month" => ts_to_month,
        _ => ts_to_date,
    }
}

fn compute_model_trends_window(
    logs: &[RequestLog],
    granularity: &str,
    explicit_start: Option<u64>,
    explicit_end: Option<u64>,
) -> Vec<ModelTrend> {
    let key_fn = trend_key_fn(granularity);
    let mut map: BTreeMap<(u64, String, String), (u64, u64, usize, usize)> = BTreeMap::new();
    for log in logs {
        if log.provider.is_empty() || log.provider == "-" {
            continue;
        }
        let upstream = log
            .upstream_model
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| log.model.clone());
        let bucket = bucket_start(log.timestamp, granularity);
        let entry = map.entry((bucket, log.provider.clone(), upstream)).or_default();
        entry.0 += log.prompt_tokens as u64;
        entry.1 += log.completion_tokens as u64;
        entry.2 += 1;
        if is_log_error(log) {
            entry.3 += 1;
        }
    }
    let mut out: Vec<ModelTrend> = map
        .into_iter()
        .map(|((ts, provider, upstream_model), (prompt_tokens, completion_tokens, requests, errors))| ModelTrend {
            date: key_fn(ts), ts, provider, upstream_model, prompt_tokens, completion_tokens, requests, errors,
        })
        .collect();
    if let (Some(start), Some(end)) = (explicit_start, explicit_end) {
        let first = bucket_start(start, granularity);
        let last = bucket_start(end.saturating_sub(1), granularity);
        out.retain(|item| item.ts >= first && item.ts <= last);
    }
    out
}

/// 桶起始 ts 步进到上一个桶 (本地时区, 对齐粒度). 与 next_bucket 对称.
fn prev_bucket(ts: u64, granularity: &str) -> u64 {
    match granularity {
        "hour" => ts.saturating_sub(3600),
        "month" => {
            let local = (ts as i64) + TZ_OFFSET_SECS;
            let days = local / 86400;
            let (y, m, _) = days_to_ymd(days);
            let (py, pm) = if m == 1 { (y - 1, 12) } else { (y, m - 1) };
            (days_from_civil(py, pm, 1) * 86400 - TZ_OFFSET_SECS) as u64
        }
        _ => ts.saturating_sub(86400),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造一条带 KV Cache 统计的请求日志.
    fn mk(model: &str, provider: &str, hit: u32, miss: u32) -> RequestLog {
        RequestLog {
            timestamp: 0,
            model: model.to_string(),
            provider: provider.to_string(),
            endpoint: "/v1/chat/completions".to_string(),
            status: 200,
            latency_ms: 10,
            body_len: 100,
            error: None,
            prompt_tokens: 100,
            completion_tokens: 50,
            cached: false,
            prompt_cache_hit_tokens: hit,
            prompt_cache_miss_tokens: miss,
            prompt_cache_creation_tokens: 0,
            strip_saved_tokens: 0,
            trim_saved_tokens: 0,
            resp_cache_saved_tokens: 0,
            audit_exempt_reasoning_tokens: 0,
            audit_dup_block_tokens: 0,
            audit_dup_block_count: 0,
            strip_toolcall_saved_tokens: 0,
            audit_observed: false,
            price: None,
            usage_estimated: false,
            first_token_ms: None,
            upstream_model: None,
        }
    }

    /// BUG 回归: 已知供应商但该模型**未配价格**时, 不得回退拿到别家同名模型的价格
    /// (否则违反"未配置价格 = 费用 0", 会把 A 家价格记到 B 家请求上).
    #[test]
    fn lookup_does_not_fall_back_for_known_provider_without_price() {
        let priced = ModelPrice { input_per_m: 10.0, output_per_m: 20.0, ..Default::default() };
        let mut t = PriceTable::default();
        // p1 的 m 有价格; p2 也有 m 但未配价格
        t.by_provider_model.insert(("p1".into(), "m".into()), priced);
        t.known_providers.insert("p1".into());
        t.known_providers.insert("p2".into());
        // 模拟唯一价表里存在 m (p1 单次出现)
        t.by_model_unique.insert("m".into(), priced);

        // p2 已知但未配价 → 必须 None (记 0), 不能回退到 p1 的价格
        assert_eq!(t.lookup("p2", "m"), None, "已知供应商未配价应为 0, 不得串用别家价");
        // p1 精确命中
        assert_eq!(t.lookup("p1", "m"), Some(priced));
        // 完全未知的供应商 (老日志/已改名) → 允许回退
        assert_eq!(t.lookup("gone", "m"), Some(priced), "未知供应商可回退");
    }

    /// BUG 回归: cache_creation token 必须按 cache_creation_per_m 计价, 而非并入 input 价.
    /// (规范: Anthropic 写入缓存常为 input 的 1.25x; 原实现完全忽略该字段.)
    #[test]
    fn cost_charges_cache_creation_at_its_own_price() {
        let p = ModelPrice {
            input_per_m: 1.0, output_per_m: 1.0,
            cache_creation_per_m: Some(10.0),
            ..Default::default()
        };
        // 1M 输入全部是"首次写入缓存", 无命中
        let cost = pricing::compute_cost_with_creation(Some(p), 0, 1_000_000, 0, 0, 1_000_000).unwrap();
        assert!((cost - 10.0).abs() < 1e-9, "creation 应按 10 元/M, 实得 {cost}");

        // 未配 creation 价 → 回退 input 价 (保持既有行为)
        let p2 = ModelPrice { input_per_m: 2.0, output_per_m: 1.0, ..Default::default() };
        let c2 = pricing::compute_cost_with_creation(Some(p2), 0, 1_000_000, 0, 0, 1_000_000).unwrap();
        assert!((c2 - 2.0).abs() < 1e-9, "未配 creation 价应回退 input 价, 实得 {c2}");

        // 命中 + 写入 + 未缓存 三者混合
        let p3 = ModelPrice {
            input_per_m: 1.0, output_per_m: 0.0,
            cache_read_per_m: Some(0.1),
            cache_creation_per_m: Some(10.0),
            ..Default::default()
        };
        // prompt=1M: 命中 400k, 写入 100k, 其余 500k 按 input
        let c3 = pricing::compute_cost_with_creation(Some(p3), 0, 1_000_000, 0, 400_000, 100_000).unwrap();
        let expect = 0.4 * 0.1 + 0.1 * 10.0 + 0.5 * 1.0;
        assert!((c3 - expect).abs() < 1e-9, "混合计费应为 {expect}, 实得 {c3}");
    }

    /// 核心回归: 同一中转 ID 出现在两个供应商下且价格不同时, 必须各按各家价格计费,
    /// 不得互相覆盖 (早期实现只以 model_id 为键, 后者覆盖前者 → 按错价记账).
    #[test]
    fn same_model_id_different_provider_prices_are_not_conflated() {
        let cheap = ModelPrice { input_per_m: 1.0, output_per_m: 1.0, ..Default::default() };
        let pricey = ModelPrice { input_per_m: 50.0, output_per_m: 50.0, ..Default::default() };
        let mut t = PriceTable::default();
        // 两家都提供 "shared/model", 但价格差 50 倍
        t.by_provider_model.insert(("provA".into(), "shared/model".into()), cheap);
        t.by_provider_model.insert(("provB".into(), "shared/model".into()), pricey);
        // 同名不同价 → 不写入全局唯一价, 避免供应商缺失时误用
        assert!(t.by_model_unique.get("shared/model").is_none(), "同名不同价不得进入唯一回退表");
        let memo = PriceMemo::new(&t);

        let mut a = mk("shared/model", "provA", 0, 0);
        a.prompt_tokens = 1_000_000;
        a.completion_tokens = 0;
        let mut b = mk("shared/model", "provB", 0, 0);
        b.prompt_tokens = 1_000_000;
        b.completion_tokens = 0;

        assert!((log_cost(&memo, &a) - 1.0).abs() < 1e-9, "provA 应按 1 元/M");
        assert!((log_cost(&memo, &b) - 50.0).abs() < 1e-9, "provB 应按 50 元/M");
    }

    /// 同名同价时写入唯一回退表, 供 provider 缺失的历史日志使用.
    #[test]
    fn same_model_id_same_price_allows_fallback() {
        let p = ModelPrice { input_per_m: 3.0, output_per_m: 9.0, ..Default::default() };
        let mut t = PriceTable::default();
        t.by_provider_model.insert(("a".into(), "m".into()), p);
        t.by_provider_model.insert(("b".into(), "m".into()), p);
        // 同名同价 → 唯一回退可用
        let mut seen: HashMap<String, Vec<ModelPrice>> = HashMap::new();
        seen.entry("m".to_string()).or_default().push(p);
        seen.entry("m".to_string()).or_default().push(p);
        if let Some(first) = seen.get("m").and_then(|v| v.first()) {
            if seen["m"].iter().all(|x| x == first) {
                t.by_model_unique.insert("m".into(), *first);
            }
        }
        assert_eq!(t.lookup("unknown-provider", "m"), Some(p), "provider 未知时应回退唯一价");
        assert_eq!(t.lookup("a", "m"), Some(p), "已知 provider 精确匹配");
        assert_eq!(t.lookup("a", "nope"), None);
    }

    /// 不同模型按各自价格计费 (核心口径): 同窗口内两个价格差异巨大的模型,
    /// 费用必须是各自的 token × 各自单价, 不能串价.
    #[test]
    fn cost_is_per_model_not_shared() {
        // cheap: 输入 1 元/M; pricey: 输入 100 元/M (100 倍差异)
        let cheap = ModelPrice { input_per_m: 1.0, output_per_m: 2.0, ..Default::default() };
        let pricey = ModelPrice { input_per_m: 100.0, output_per_m: 200.0, ..Default::default() };
        let mut t = PriceTable::default();
        t.by_provider_model.insert(("p1".into(), "cheap".into()), cheap);
        t.by_provider_model.insert(("p2".into(), "pricey".into()), pricey);
        let memo = PriceMemo::new(&t);

        let mut a = mk("cheap", "p1", 0, 0);
        a.prompt_tokens = 1_000_000;
        a.completion_tokens = 0;
        let mut b = mk("pricey", "p2", 0, 0);
        b.prompt_tokens = 1_000_000;
        b.completion_tokens = 0;

        assert!((log_cost(&memo, &a) - 1.0).abs() < 1e-9, "cheap 应为 1 元");
        assert!((log_cost(&memo, &b) - 100.0).abs() < 1e-9, "pricey 应为 100 元");
        // 合计必须逐条相加, 不得任一价格覆盖另一价格
        let total = log_cost(&memo, &a) + log_cost(&memo, &b);
        assert!((total - 101.0).abs() < 1e-9, "合计 101 元, 实得 {total}");
    }

    /// 未配置价格的模型计费为 0, 且不污染同窗口其他模型的费用.
    #[test]
    fn unpriced_model_costs_zero_without_affecting_others() {
        let mut t = PriceTable::default();
        t.by_provider_model.insert(("p".into(), "priced".into()), ModelPrice {
            input_per_m: 10.0, output_per_m: 20.0, ..Default::default()
        });
        let memo = PriceMemo::new(&t);
        let mut priced = mk("priced", "p", 0, 0);
        priced.prompt_tokens = 1_000_000;
        priced.completion_tokens = 0;
        let mut unpriced = mk("nope", "p", 0, 0);
        unpriced.prompt_tokens = 1_000_000;
        unpriced.completion_tokens = 0;
        assert!((log_cost(&memo, &priced) - 10.0).abs() < 1e-9);
        assert_eq!(log_cost(&memo, &unpriced), 0.0, "未配置价格应为 0");
        assert!((log_cost(&memo, &priced) - 10.0).abs() < 1e-9, "不受未定价模型影响");
    }

    /// 聚合: 全局缓存命中/未命中 token 与命中率正确; 模型与供应商分组正确累加.
    #[test]
    fn test_cache_hit_rate_aggregation() {
        let logs = vec![
            mk("deepseek", "ds", 80, 20),
            mk("deepseek", "ds", 60, 40),
            mk("gpt", "oa", 0, 100),
        ];
        let s = compute_stats(&logs, &PriceTable::default(), &std::collections::HashSet::new());
        // 全局: 命中 80+60+0=140, 未命中 20+40+100=160
        assert_eq!(s.total_cache_hit_tokens, 140);
        assert_eq!(s.total_cache_miss_tokens, 160);
        assert!((s.cache_hit_rate - 140.0 / 300.0).abs() < 1e-9);
        // 模型分组: 上游模型与供应商字段分开返回 (upstream 缺失回退 model).
        let ds = s.per_model.iter().find(|m| m.model == "deepseek").unwrap();
        assert_eq!(ds.provider, "ds");
        assert_eq!(ds.upstream_model, "deepseek");
        assert_eq!(ds.total_cache_hit_tokens, 140);
        assert_eq!(ds.total_cache_miss_tokens, 60);
        // 供应商分组: ds 命中 140, 未命中 60
        let p = s.per_provider.iter().find(|p| p.provider == "ds").unwrap();
        assert_eq!(p.total_cache_hit_tokens, 140);
        assert_eq!(p.total_cache_miss_tokens, 60);
    }

    /// 聚合: 无 cache 数据时命中率为 0 (不除零).
    #[test]
    fn test_cache_hit_rate_zero() {
        let logs = vec![mk("m", "p", 0, 0)];
        let s = compute_stats(&logs, &PriceTable::default(), &std::collections::HashSet::new());
        assert_eq!(s.total_cache_hit_tokens, 0);
        assert_eq!(s.total_cache_miss_tokens, 0);
        assert_eq!(s.cache_hit_rate, 0.0);
    }

    /// 趋势: 日界按东八区切分 (UTC 23:30 属「当天」, UTC 01:00 属「次日」).
    #[test]
    fn test_trend_local_timezone_day_boundary() {
        // 2026-08-01T23:30:00Z → 东八区 08-02 07:30, 属 08/02
        let t_same_day = 1_785_627_000;
        // 2026-08-02T01:00:00Z → 东八区 08-02 09:00, 仍属 08/02
        let t_next = 1_785_632_400;
        let logs = vec![mk_ts("m", "p", t_same_day), mk_ts("m", "p", t_next)];
        let trends = compute_trends(&logs, "day", &PriceTable::default());
        // 补齐窗口后至少 30 个日桶 (含空桶); 两条日志都应归入东八区 08/02 同一桶.
        assert!(trends.len() >= 30, "日粒度应补齐最近 30 天窗口");
        let bucket = trends.iter().find(|d| d.date == "08/02").expect("应有 08/02 桶");
        assert_eq!(bucket.requests, 2);
        // 时间轴严格递增 (按 ts).
        let mut prev = 0u64;
        for d in &trends {
            assert!(d.ts >= prev, "日桶必须按时间递增");
            prev = d.ts;
        }
    }

    /// 模型趋势: 同一日期按模型拆分, 并按东八区日界正确归桶.
    #[test]
    fn test_model_trends_by_day_and_model() {
        let mut a = mk_ts("alias-a", "provider-a", 1_785_627_000);
        a.upstream_model = Some("model-a".to_string());
        a.prompt_tokens = 100;
        a.completion_tokens = 20;
        let mut b = mk_ts("alias-b", "provider-b", 1_785_632_400);
        b.upstream_model = Some("model-b".to_string());
        b.prompt_tokens = 300;
        b.completion_tokens = 40;
        let mut c = mk_ts("alias-c", "provider-a", 1_785_713_400);
        c.upstream_model = Some("model-a".to_string());
        c.prompt_tokens = 50;
        c.completion_tokens = 5;
        let trends = compute_model_trends_window(
            &[a, b, c],
            "day",
            Some(1_785_600_000),
            Some(1_785_800_000),
        );
        let a_day = trends.iter().find(|x| x.provider == "provider-a" && x.upstream_model == "model-a" && x.date == "08/02").unwrap();
        assert_eq!(a_day.prompt_tokens, 100);
        assert_eq!(a_day.completion_tokens, 20);
        assert_eq!(a_day.requests, 1);
        let b_day = trends.iter().find(|x| x.provider == "provider-b" && x.upstream_model == "model-b" && x.date == "08/02").unwrap();
        assert_eq!(b_day.prompt_tokens, 300);
        let a_later = trends.iter().find(|x| x.provider == "provider-a" && x.upstream_model == "model-a" && x.date != "08/02").unwrap();
        assert_eq!(a_later.prompt_tokens, 50);
    }

    /// 趋势: 跨多日/多月日志能正确聚合成多个桶 (验证统计基于全量而非内存截断).
    #[test]
    fn test_trend_full_data_multi_bucket() {
        // 2026-07-15T00:00:00Z 起, 间隔 5 天, 全部落在 07 月.
        let base = 1_784_073_600;
        let logs: Vec<RequestLog> = (0..3)
            .map(|i| mk_ts("m", "p", base + i * 5 * 86400))
            .collect();
        let trends = compute_trends(&logs, "day", &PriceTable::default());
        assert!(trends.len() >= 30, "日粒度至少补齐 30 天窗口");
        // 非空桶应有 3 个 (跨 07-15 / 07-20 / 07-25), 其余为补齐的空桶.
        let non_empty = trends.iter().filter(|d| d.requests > 0).count();
        assert_eq!(non_empty, 3, "跨多日应生成 3 个非空趋势桶");
        // 按月聚合: 全部落在同一个月 (07 月), 仅 1 个非空桶; 窗口补齐 12 个月.
        let month = compute_trends(&logs, "month", &PriceTable::default());
        assert_eq!(month.len(), 12, "月粒度补齐 12 个月窗口");
        let m_non_empty = month.iter().filter(|d| d.requests > 0).count();
        assert_eq!(m_non_empty, 1);
        assert!(month.iter().any(|d| d.date == "2026/07" && d.requests == 3));
    }

    /// 趋势: 小时粒度补齐最近 24 个整点桶 (最新=当前小时, 最旧=当前小时-23h),
    /// 且时间轴严格递增 (回应用户反馈: 小时应显示 24 段、时间对应).
    #[test]
    fn test_trend_hour_window_24_segments() {
        let now = now_ts();
        // 往当前小时内塞一条日志, 验证它归入「当前小时」桶.
        let logs = vec![mk_ts("m", "p", now)];
        let trends = compute_trends(&logs, "hour", &PriceTable::default());
        assert_eq!(trends.len(), 24, "小时粒度应补齐最近 24 个整点");
        // 时间轴严格递增.
        let mut prev = 0u64;
        for d in &trends {
            assert!(d.ts >= prev, "小时桶必须按时间递增");
            prev = d.ts;
        }
        // 最新桶 = 当前小时整点, 且其内有 1 条请求.
        let last = trends.last().expect("应有最新桶");
        assert_eq!(last.requests, 1, "当前小时桶应包含刚插入的日志");
        // 最新桶时间 = 当前时间对齐到整点 (<= now).
        assert!(last.ts <= now);
        assert_eq!(last.ts % 3600, 0, "桶起始须为整点");
        // 最旧桶 = 最新桶 - 23 小时.
        let first = trends.first().expect("应有最旧桶");
        assert_eq!(last.ts - first.ts, 23 * 3600);
    }

    /// 构造带指定时间戳的日志 (复用 mk 的其余默认值).
    fn mk_ts(model: &str, provider: &str, ts: u64) -> RequestLog {
        let mut log = mk(model, provider, 0, 0);
        log.timestamp = ts;
        log
    }

    /// 回归: 跨年边界月桶年份必须正确 (ts_to_month 曾用 UTC 天数推年,
    /// 导致本地 2026-01-01 被错标为 "2025/01"). 同时校验月桶序列严格按时间递增.
    #[test]
    fn test_trend_month_year_boundary() {
        // 2026-01-01T00:30:00Z → 东八区 2026-01-01 08:30, 属 2026/01 (非 2025/01).
        let ts = 1_768_185_000;
        let logs = vec![mk_ts("m", "p", ts)];
        let month = compute_trends(&logs, "month", &PriceTable::default());
        assert_eq!(month.len(), 12, "月粒度补齐 12 个月窗口");
        // 必须存在 "2026/01" 桶 (而非 "2025/01"), 且含 1 条请求.
        let jan = month
            .iter()
            .find(|d| d.date == "2026/01")
            .expect("跨年边界应正确标记为 2026/01");
        assert_eq!(jan.requests, 1);
        assert!(!month.iter().any(|d| d.date == "2025/01"), "不得出现串年的 2025/01");
        // 月桶序列按时间严格递增 (date 字符串不再因串年错序).
        let mut prev = String::new();
        for d in &month {
            assert!(d.date >= prev, "月桶必须按时间递增: {} < {}", d.date, prev);
            prev = d.date.clone();
        }
    }

    /// rollup 合并边界: 单日流量撑满日志窗口 (缓冲区最老日志就在今天) 时,
    /// 仍须合并今天之前的 rollup 天, 且今天的日志不得被剔除.
    #[test]
    fn rollup_merge_bounds_covers_history_when_buffer_holds_only_today() {
        // 取一个北京时区白天时刻作为 now, 便于构造跨天样本.
        let now = 1_700_000_000u64;
        let today = bucket_start(now, "day");
        let range_start = now - 29 * 86400;

        // 情形 A: 缓冲区最老日志就在今天 (单日 >5000 条被滚动覆盖)
        let (merge_end, rstart) = rollup_merge_bounds(now, range_start, now - 60).unwrap();
        assert_eq!(merge_end, today - 86400, "上界应退到昨天, 今天交给日志侧");
        assert_eq!(rstart, bucket_start(range_start, "day"), "下界对齐到范围首日");
        assert!(rstart < merge_end, "历史区间必须被合并, 不能退化成只有今天");

        // 情形 B: 缓冲区最老日志在更早的天 → 该天 rollup 已完整, 上界取该天
        let older = today - 5 * 86400;
        let (merge_end_b, _) = rollup_merge_bounds(now, range_start, older).unwrap();
        assert_eq!(merge_end_b, older);

        // 情形 C: 查询起点不早于上界 → 无需合并
        assert!(rollup_merge_bounds(now, now - 60, now - 60).is_none());
    }

    /// rollup 的模型趋势必须能为历史天新建桶 (否则长跨度图表缺历史曲线).
    #[test]
    fn merge_rollup_days_creates_model_trend_buckets() {
        use crate::rollup::{DailyRollup, RollupEntry};
        let day_start = bucket_start(1_700_000_000, "day") - 86400;
        let mut entry = RollupEntry::default();
        entry.provider = "ds".to_string();
        entry.upstream = "deepseek-v4-pro".to_string();
        entry.requests = 7;
        entry.prompt_tokens = 700;
        entry.completion_tokens = 70;
        let day = DailyRollup { day_start, entries: vec![entry] };

        let trends = merge_rollup_model_trends(Vec::new(), &[day], "day");
        assert_eq!(trends.len(), 1, "历史天的模型桶应被新建");
        let t = &trends[0];
        assert_eq!(t.ts, day_start);
        assert_eq!(t.provider, "ds");
        assert_eq!(t.upstream_model, "deepseek-v4-pro");
        assert_eq!(t.prompt_tokens, 700);
        assert_eq!(t.requests, 7);
        assert!(!t.date.is_empty(), "日期键不能为空");
    }

    /// 命中本地响应缓存的日志不得贡献上游 token: 不计入总量/命中率分母, 只保留省量.
    #[test]
    fn cached_replay_contributes_no_upstream_tokens() {
        let buf = LogBuffer::new();
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            let mut log = mk("m", "p", 0, 0);
            log.prompt_tokens = 6000;
            log.completion_tokens = 50;
            log.cached = true;
            log.resp_cache_saved_tokens = 6050;
            buf.push(log).await;
        });
        let s = buf.session_stats();
        assert_eq!(s.requests, 1, "命中回放仍是一次请求");
        assert_eq!(s.prompt_tokens, 0, "命中回放无上游输入消耗");
        assert_eq!(s.completion_tokens, 0, "命中回放无上游输出消耗");
        // 省量必须保留, 否则面板"优化省量"会丢数据
        let logs = rt.block_on(buf.drain_all());
        assert_eq!(logs[0].resp_cache_saved_tokens, 6050);
        assert_eq!(logs[0].prompt_tokens, 0);
    }

    /// 本轮 (进程级) 累计: push 时原子累加, 请求/成功/输入/输出/KV 命中均正确.
    #[test]
    fn session_stats_accumulates_on_push() {
        let buf = LogBuffer::new();
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            let mut log = mk("m", "p", 80, 20);
            buf.push(log.clone()).await; // 成功, hit=80 miss=20
            log.prompt_cache_hit_tokens = 60;
            log.prompt_cache_miss_tokens = 40;
            buf.push(log.clone()).await; // 成功
            log.status = 500;
            log.prompt_cache_hit_tokens = 0;
            log.prompt_cache_miss_tokens = 0;
            buf.push(log).await; // 失败
        });
        let s = buf.session_stats();
        assert_eq!(s.requests, 3);
        assert_eq!(s.success, 2, "500 不算成功");
        assert_eq!(s.prompt_tokens, 300, "3 条 × 100");
        assert_eq!(s.completion_tokens, 150, "3 条 × 50");
        assert_eq!(s.cache_hit_tokens, 140);
        assert_eq!(s.cache_miss_tokens, 60);
    }

    /// 「已省」改为净省口径: 命中折扣 − 写入溢价, 与 `compute_cost` 输入拆分一致.
    /// 锁定缓存被反复重建 (T2 裁剪平移前缀 / 改系统提示词 / 切模型) 时不再高估省钱.
    #[test]
    fn test_cache_saved_net_of_creation_premium() {
        // DeepSeek 类价格 (元/百万 token): 输入 2, 读 0.2, 写入 2.5.
        let price = ModelPrice {
            input_per_m: 2.0,
            output_per_m: 8.0,
            cache_read_per_m: Some(0.2),
            cache_creation_per_m: Some(2.5),
            input_per_m_offpeak: 0.0,
            output_per_m_offpeak: 0.0,
            cache_read_per_m_offpeak: 0.0,
        };
        let mut t = PriceTable::default();
        t.by_provider_model.insert(("ds".into(), "ds".into()), price);
        let memo = PriceMemo::new(&t);

        // 1) 纯命中 1M token, 无写入 → 净省 = 1 × (2 − 0.2) = 1.8.
        let mut log = mk("ds", "ds", 1_000_000, 0);
        log.prompt_tokens = 1_000_000;
        assert!((log_cache_saved(&memo, &log) - 1.8).abs() < 1e-9);

        // 2) 纯写入 1M token (无命中) → 净省 = −1 × (2.5 − 2) = −0.5 (写入溢价抵消, 不钳到 0).
        let mut log2 = mk("ds", "ds", 0, 0);
        log2.prompt_tokens = 1_000_000;
        log2.prompt_cache_creation_tokens = 1_000_000;
        assert!((log_cache_saved(&memo, &log2) - (-0.5)).abs() < 1e-9);

        // 3) 命中 1M + 写入 1M → 净省 = 1.8 − 0.5 = 1.3.
        let mut log3 = mk("ds", "ds", 1_000_000, 0);
        log3.prompt_tokens = 2_000_000;
        log3.prompt_cache_creation_tokens = 1_000_000;
        assert!((log_cache_saved(&memo, &log3) - 1.3).abs() < 1e-9);

        // 4) 命中本地响应缓存 (log.cached) → 未真实消费上游, 省额记 0.
        let mut log4 = mk("ds", "ds", 1_000_000, 0);
        log4.cached = true;
        assert_eq!(log_cache_saved(&memo, &log4), 0.0);

        // 5) 输入价为 0 (月套餐/未配置) → 无计费基数, 净省记 0.
        let free = ModelPrice { input_per_m: 0.0, output_per_m: 0.0, cache_read_per_m: None, cache_creation_per_m: None, input_per_m_offpeak: 0.0, output_per_m_offpeak: 0.0, cache_read_per_m_offpeak: 0.0 };
        let mut t2 = PriceTable::default();
        t2.by_provider_model.insert(("oc".into(), "opencode".into()), free);
        let memo2 = PriceMemo::new(&t2);
        let mut log5 = mk("opencode", "oc", 1_000_000, 0);
        log5.prompt_tokens = 1_000_000;
        assert_eq!(log_cache_saved(&memo2, &log5), 0.0);
    }

    /// 省量折算必须按「该请求自身的缓存命中比例」加权, 而不是一律按未命中输入价.
    ///
    /// 回归背景: 原口径把省下的 token 全按 input 价折算, 而实测 agent 工作流缓存命中率
    /// 可达 98% —— 被省掉的推理链本会落在缓存命中区 (input 与 cache_read 价差约 50x),
    /// 于是面板把省量价值放大约 50 倍 (实测 tool_calls 项 ¥63.75 实为 ¥2.38).
    #[test]
    fn saved_fee_is_weighted_by_cache_hit_ratio() {
        // input 1.0 / output 2.0 / cache_read 0.01 (价差 100x, 便于断言两个极端)
        let p = ModelPrice {
            input_per_m: 1.0,
            output_per_m: 2.0,
            cache_read_per_m: Some(0.01),
            ..Default::default()
        };
        // 非高峰: 用 offpeak=0 回退高峰价, 保证时段不影响断言.
        let ts = 0;

        // 1) 命中率 99%: 10000 tok 省量 → 0.01 * (0.99*0.01 + 0.01*1.0) = 0.000199
        let fee = saved_tokens_fee(p, ts, 10_000, 100_000, 99_000);
        assert!((fee - 0.000199).abs() < 1e-12, "命中率加权结果实得 {fee}");

        // 2) 必须显著低于「全按 input 价」的错误口径 (0.01 元)
        let naive = 10_000f64 / 1e6 * 1.0;
        assert!(fee < naive / 40.0, "加权后应远低于按 input 价的 {naive}, 实得 {fee}");

        // 3) 命中率 0% → 退化为 input 价 (未命中区)
        let nohit = saved_tokens_fee(p, ts, 10_000, 100_000, 0);
        assert!((nohit - naive).abs() < 1e-12, "零命中应等于 input 价, 实得 {nohit}");

        // 4) 全部命中 → 趋近 cache_read 价 (0.01/M → 10000 tok = 0.0001)
        let allhit = saved_tokens_fee(p, ts, 10_000, 100_000, 100_000);
        assert!((allhit - 0.0001).abs() < 1e-12, "全命中应等于 cache_read 价, 实得 {allhit}");

        // 5) prompt 为 0 (无从判断命中结构) → 按未命中价, 保守偏低而不是放大
        assert!((saved_tokens_fee(p, ts, 10_000, 0, 0) - naive).abs() < 1e-12);

        // 6) 省量为 0 → 恒 0 (不得因单价非零而凭空计费)
        assert_eq!(saved_tokens_fee(p, ts, 0, 100_000, 99_000), 0.0);

        // 7) 命中数超过 prompt (脏数据) 时钳制在 1.0, 不得把比例算成 >1
        assert!((saved_tokens_fee(p, ts, 10_000, 1_000, 999_999) - 0.0001).abs() < 1e-12);
    }

    /// 今日 / 月度 / 累计 / 审计明细四处省量费用必须同口径 (同走 saved_tokens_fee).
    ///
    /// 回归背景: 折算口径散落在四处内联实现里, 改一处漏三处会让面板各卡片互相打架
    /// (实测曾出现审计明细 ¥3.28 而累计仍显示 ¥82.00).
    #[test]
    fn saved_fee_sites_agree_on_same_logs() {
        let price = ModelPrice {
            input_per_m: 1.0,
            output_per_m: 2.0,
            cache_read_per_m: Some(0.01),
            ..Default::default()
        };
        let mut t = PriceTable::default();
        t.by_provider_model.insert(("prov".into(), "m".into()), price);
        let memo = PriceMemo::new(&t);

        // 命中率 99%, 省量 10000
        let mut log = mk("m", "prov", 99_000, 1_000);
        log.prompt_tokens = 100_000;
        log.strip_saved_tokens = 10_000;
        let logs = vec![log];

        let a = sum_saved_fee(&memo, &logs, |l| {
            (l.strip_saved_tokens + l.trim_saved_tokens + l.resp_cache_saved_tokens) as u64
        });
        let b = saved_tokens_fee(price, 0, 10_000, 100_000, 99_000);
        assert!((a - b).abs() < 1e-12, "sum_saved_fee 与 saved_tokens_fee 应一致: {a} vs {b}");
        assert!(a > 0.0, "该场景省量费用应为正, 实得 {a}");
    }

    /// 请求记录是**历史账单**: 费用按记录当时的价格结算, 之后改价/删模型都不得改写它.
    ///
    /// 回归背景: 原先 `RequestLog` 只存 token, `cost` 在查询期用当前价格表现算 ——
    /// 实测同一批 1451 条历史日志, 改价后总费用被改写, 删掉模型配置后更是直接归零.
    #[test]
    fn log_cost_uses_frozen_price_snapshot() {
        let frozen = ModelPrice { input_per_m: 1.0, output_per_m: 2.0, ..Default::default() };
        let current = ModelPrice { input_per_m: 100.0, output_per_m: 200.0, ..Default::default() };
        let mut t = PriceTable::default();
        t.by_provider_model.insert(("p".into(), "m".into()), current);
        let memo = PriceMemo::new(&t);

        let mut snap = mk("m", "p", 0, 0);
        snap.prompt_tokens = 1_000_000;
        snap.completion_tokens = 0;
        snap.price = Some(frozen);

        // 1) 带快照: 按记录当时的价格结算 (1.0/M → 1.0 元), 不受当前配置 (100x) 影响.
        assert!((log_cost(&memo, &snap) - 1.0).abs() < 1e-9, "应按快照价结算, 实得 {}", log_cost(&memo, &snap));

        // 2) 旧日志 (无快照): 回退当前配置, 与升级前行为一致.
        let mut legacy = mk("m", "p", 0, 0);
        legacy.prompt_tokens = 1_000_000;
        legacy.completion_tokens = 0;
        assert!((log_cost(&memo, &legacy) - 100.0).abs() < 1e-9, "旧日志应回退当前配置");

        // 3) 当前配置被清空 (等价于删除模型): 带快照的历史仍必须可结算 —— 这是本修复的核心.
        let empty_table = PriceTable::default();
        let memo_empty = PriceMemo::new(&empty_table);
        assert!(
            (log_cost(&memo_empty, &snap) - 1.0).abs() < 1e-9,
            "删除模型后历史费用不得归零, 实得 {}",
            log_cost(&memo_empty, &snap)
        );

        // 4) 省量与缓存净省的折算同样走快照, 保持全口径一致.
        let mut saved = mk("m", "p", 0, 0);
        saved.prompt_tokens = 1_000_000;
        saved.completion_tokens = 0;
        saved.strip_saved_tokens = 100_000;
        saved.price = Some(frozen);
        let fee = sum_saved_fee(&memo_empty, &[saved], |l| l.strip_saved_tokens as u64);
        assert!(fee > 0.0, "删除模型后省量折算也不得归零, 实得 {fee}");
    }

    /// rollup 条目自带价格快照: 条目级费用同样不受后续改价/删模型影响.
    #[test]
    fn rollup_entry_price_is_frozen() {
        let frozen = ModelPrice { input_per_m: 1.0, output_per_m: 2.0, ..Default::default() };
        let mut e = crate::rollup::RollupEntry::default();
        e.provider = "p".into();
        e.upstream = "m".into();
        e.aliases = vec!["m".into()];
        e.price = Some(frozen);
        e.bill_prompt_offpeak = 1_000_000;

        // 当前配置里价格已改成 100x, 表里甚至可能已无此模型 → 条目仍按快照结算.
        let mut t = PriceTable::default();
        t.known_providers.insert("p".into());
        t.by_provider_model
            .insert(("p".into(), "m".into()), ModelPrice { input_per_m: 100.0, output_per_m: 200.0, ..Default::default() });
        let p = entry_price(&t, &e).expect("应能取到价格");
        assert_eq!(p.input_per_m, 1.0, "必须用条目快照价而非当前配置价");
        assert!((entry_cost(p, &e) - 1.0).abs() < 1e-9);

        // 表为空 (模型被删除) 时同样可用.
        assert!(entry_price(&PriceTable::default(), &e).is_some(), "删模型后条目仍应可结算");
    }
}
