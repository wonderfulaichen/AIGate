//! 模型元信息 — 从 models.dev 公开库拉取上下文/输出限制与模态能力,
//! 供管理面板悬停展示 (上下文大小 / 最大输出 / 视觉标签等).
//!
//! 数据源: `https://models.dev/api.json` (免费无 key, 供应商→模型两级结构).
//! 网关别名 (如 `kimi-k3-free`) 通过归一化匹配: 剥噪声后缀 → 前缀族模糊匹配.
//! 拉取失败静默降级为空表 (负缓存 10 分钟防抖), 不影响任何转发链路.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::Serialize;
use tokio::sync::RwLock;
use tracing::{info, warn};

const MODELS_DEV_URL: &str = "https://models.dev/api.json";
/// 成功数据缓存时长.
const CACHE_TTL: Duration = Duration::from_secs(24 * 3600);
/// 拉取失败后的负缓存时长 (期间直接返回空表, 不再打外网).
const NEGATIVE_TTL: Duration = Duration::from_secs(10 * 60);

/// 单个模型的元信息 (序列化给面板前端).
#[derive(Debug, Clone, Serialize)]
pub struct ModelMeta {
    /// 上下文窗口 (token 上限).
    pub context: Option<u64>,
    /// 最大输出 token.
    pub output: Option<u64>,
    /// 支持图像输入 (视觉): modalities.input 含 image/video 或 attachment=true.
    pub vision: bool,
    /// 支持推理 (reasoning 模式).
    pub reasoning: bool,
    /// 支持工具调用.
    pub tool_call: bool,
}

/// models.dev 收录的单价 (美元 / 百万 tokens). 缺失字段为 None.
///
/// 仅作**面板参考**, 不写入 `price`: models.dev 是单一价、USD 计价, 而 AIGate 内部按
/// CNY 计价且部分官方供应商是峰谷双价 (DeepSeek 空闲价 = 高峰价一半, 另有独立缓存命中价).
#[derive(Debug, Clone, Serialize)]
pub struct MetaCost {
    pub input: Option<f64>,
    pub output: Option<f64>,
    /// 缓存读取价 (`cache_read`).
    pub cache_read: Option<f64>,
    /// 缓存写入价 (`cache_write`), 部分供应商才有.
    pub cache_write: Option<f64>,
    /// 该价来自哪个 models.dev provider —— 面板据此展示来源, 让用户判断是否与自己的供应商相符.
    pub provider: String,
}

/// models.dev 全量索引: 归一化 id → 元信息.
///
/// 说明: 上下文/输出/视觉/推理/工具等字段在同名模型间基本一致, 扁平覆盖无实质影响.
/// **价格不能这样处理** —— 见 `costs` 字段的注释.
struct Index {
    map: HashMap<String, ModelMeta>,
    /// 官方参考价: `(models.dev provider id, 归一化模型名)` → 单价 (USD/1M).
    ///
    /// ⚠️ 价格必须按 provider 分别索引: models.dev 收录 200+ provider, 同一模型被几十家
    /// 重复收录且报价差异极大. 实测 `deepseek-v4-flash` 有 **34 个 provider** 贡献同一
    /// 归一化键 —— 官方 deepseek 报 0.15/0.6, azure 报 0.19/0.51, 而部分订阅制 provider
    /// 直接报 **0/0**. 若沿用扁平 map (后者覆盖前者), 参考价会取到别家甚至 0 ——
    /// 这正是本项目反复强调要避免的「看着精确的假数」.
    costs: HashMap<(String, String), MetaCost>,
}

impl Index {
    fn empty() -> Self {
        Self { map: HashMap::new(), costs: HashMap::new() }
    }
}

/// 元信息缓存: AppState 持 Arc 共享, 内部 RwLock 保护 (跨 await, 用 tokio 锁).
pub struct MetaCache {
    inner: RwLock<CacheState>,
}

struct CacheState {
    index: Option<Arc<Index>>,
    fetched_at: Option<Instant>,
    last_failure_at: Option<Instant>,
}

impl MetaCache {
    pub fn new() -> Self {
        Self {
            inner: RwLock::new(CacheState {
                index: None,
                fetched_at: None,
                last_failure_at: None,
            }),
        }
    }

    /// 解析一批「(供应商, 模型名)」 → 该供应商在 models.dev 的**参考价**.
    ///
    /// **严格按供应商归属匹配, 匹配不到就不返回** —— 不做"随便找一家同名模型"的回退.
    /// 理由: 同名模型在不同 provider 下报价差异极大 (实测 `deepseek-v4-flash` 有 34 家收录:
    /// 官方 0.15/0.6, azure 0.19/0.51, 部分订阅制 provider 报 0/0). 拿别家的价当"参考"
    /// 只会误导. 返回 `provider(原样) -> {模型名(原样) -> MetaCost}`.
    pub async fn resolve_costs(
        &self,
        client: &reqwest::Client,
        queries: &[(String, String)],
    ) -> HashMap<String, HashMap<String, MetaCost>> {
        let index = self.get_index(client).await;
        let mut out: HashMap<String, HashMap<String, MetaCost>> = HashMap::new();
        for (prov, name) in queries {
            let p = prov.trim();
            let n = name.trim();
            if p.is_empty() || n.is_empty() {
                continue;
            }
            let Some(ix) = index.as_ref() else { continue };
            let key = (p.to_lowercase(), normalize(n));
            if let Some(c) = ix.costs.get(&key) {
                out.entry(p.to_string()).or_default().insert(n.to_string(), c.clone());
            }
        }
        out
    }

    /// 解析一批模型名 → 名称到元信息的映射 (未收录的键值为 None, 前端据此隐藏).
    pub async fn resolve_many(
        &self,
        client: &reqwest::Client,
        names: &[String],
    ) -> HashMap<String, Option<ModelMeta>> {
        let index = self.get_index(client).await;
        let mut out = HashMap::with_capacity(names.len());
        for n in names {
            let key = n.trim().to_string();
            if key.is_empty() {
                continue;
            }
            // 已提交过同名请求时复用结果 (names 可能含重复别名).
            if out.contains_key(&key) {
                continue;
            }
            let meta = index
                .as_ref()
                .and_then(|ix| resolve_one(&ix.map, &key));
            out.insert(key, meta);
        }
        out
    }

    /// 取索引: 缓存新鲜直接用; 过期/缺失则拉取重建; 近期失败过且未过负缓存期返回空.
    async fn get_index(&self, client: &reqwest::Client) -> Option<Arc<Index>> {
        {
            let st = self.inner.read().await;
            if let (Some(ix), Some(t)) = (&st.index, st.fetched_at) {
                if t.elapsed() < CACHE_TTL {
                    return Some(Arc::clone(ix));
                }
            }
            if let Some(f) = st.last_failure_at {
                if f.elapsed() < NEGATIVE_TTL {
                    return None;
                }
            }
        }
        // 放读锁后拉取 (避免长临界区阻塞并发请求); 并发重复拉取无害, 后写覆盖.
        match fetch_index(client).await {
            Ok(ix) => {
                let arc = Arc::new(ix);
                let mut st = self.inner.write().await;
                st.index = Some(Arc::clone(&arc));
                st.fetched_at = Some(Instant::now());
                st.last_failure_at = None;
                info!("model_meta: loaded {} entries from models.dev", st.index.as_ref().map(|i| i.map.len()).unwrap_or(0));
                Some(arc)
            }
            Err(e) => {
                warn!("model_meta: fetch models.dev failed: {e} (degraded to no-meta for {NEGATIVE_TTL:?})");
                let mut st = self.inner.write().await;
                st.last_failure_at = Some(Instant::now());
                None
            }
        }
    }
}

/// 拉取并解析 models.dev/api.json → 归一化索引.
async fn fetch_index(client: &reqwest::Client) -> Result<Index, String> {
    let resp = client
        .get(MODELS_DEV_URL)
        .timeout(Duration::from_secs(30))
        .send()
        .await
        .map_err(|e| format!("request: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status()));
    }
    let body = resp.text().await.map_err(|e| format!("read body: {e}"))?;
    let v: serde_json::Value =
        serde_json::from_str(&body).map_err(|e| format!("parse json: {e}"))?;
    Ok(parse_root(&v))
}

/// 解析顶层结构 `{provider: {models: {id: {...}}}}`, 防御式读取字段.
fn parse_root(root: &serde_json::Value) -> Index {
    let mut ix = Index::empty();
    let Some(providers) = root.as_object() else {
        return ix;
    };
    for (prov_id, pv) in providers {
        let Some(models) = pv.get("models").and_then(|m| m.as_object()) else {
            continue;
        };
        let prov_key = prov_id.trim().to_lowercase();
        for (id, mv) in models {
            let context = mv.pointer("/limit/context").and_then(fnum);
            let output = mv.pointer("/limit/output").and_then(fnum);
            let input_mods: Vec<String> = mv
                .pointer("/modalities/input")
                .and_then(|x| x.as_array())
                .map(|a| a.iter().filter_map(|s| s.as_str()).map(String::from).collect())
                .unwrap_or_default();
            let vision = input_mods.iter().any(|s| s == "image" || s == "video")
                || mv.get("attachment").and_then(|b| b.as_bool()).unwrap_or(false);
            let reasoning = mv.get("reasoning").and_then(|b| b.as_bool()).unwrap_or(false);
            let tool_call = mv.get("tool_call").and_then(|b| b.as_bool()).unwrap_or(false);
            let norm = normalize(id);
            ix.map.insert(
                norm.clone(),
                ModelMeta { context, output, vision, reasoning, tool_call },
            );
            // 参考价按 (provider, 归一化模型名) 单独存放 —— 同名模型各家中转价差异极大,
            // 不能扁平覆盖 (详见 Index::costs 注释).
            if let Some(mc) = mv.get("cost").and_then(|c| parse_cost(c, &prov_key)) {
                ix.costs.insert((prov_key.clone(), norm), mc);
            }
        }
    }
    ix
}

/// 解析单个 provider 的 `cost` 对象 → MetaCost. 全字段缺失/非对象时返回 None.
fn parse_cost(c: &serde_json::Value, provider: &str) -> Option<MetaCost> {
    if !c.is_object() {
        return None;
    }
    let mc = MetaCost {
        input: c.get("input").and_then(|v| v.as_f64()),
        output: c.get("output").and_then(|v| v.as_f64()),
        cache_read: c.get("cache_read").and_then(|v| v.as_f64()),
        cache_write: c.get("cache_write").and_then(|v| v.as_f64()),
        provider: provider.to_string(),
    };
    if mc.input.is_none() && mc.output.is_none()
        && mc.cache_read.is_none() && mc.cache_write.is_none()
    {
        None
    } else {
        Some(mc)
    }
}

/// 数字字段容错读取 (models.dev 个别字段可能为浮点/字符串).
fn fnum(v: &serde_json::Value) -> Option<u64> {
    v.as_u64().or_else(|| v.as_f64().map(|f| f as u64)).or_else(|| v.as_str()?.trim().parse().ok())
}

/// 归一化模型名: 小写 + 剥网关别名噪声后缀 (-free/-contributor 等) 与日期尾段.
///
/// 例: `Kimi-K3-Free` → `kimi-k3`; `gpt-4o-2024-11-20` → `gpt-4o`;
/// `claude-sonnet-4-5-20250929` → `claude-sonnet-4-5`. 索引与查询两侧一致归一化,
/// 纯数字版本号误剥无碍 (同键命中); 仅短纯数字段 (如 `claude-sonnet-4-5` 的 `5`) 保留.
pub(crate) fn normalize(name: &str) -> String {
    let mut s = name.trim().to_lowercase();
    const NOISE: &[&str] = &[
        "-free", "-contributor", "-custom", "-preview", "-thinking",
        "-chat", "-latest", "-exp", "-experimental", "-online", "-api",
    ];
    loop {
        let before = s.clone();
        for n in NOISE {
            if s.ends_with(n) {
                s.truncate(s.len() - n.len());
            }
        }
        // 日期样式数字尾段: 长度 2/4 (月/年) 或 ≥6 且以 20 开头 (YYYYMMDD).
        if let Some(pos) = s.rfind('-') {
            let seg = &s[pos + 1..];
            let dateish = (seg.len() == 2 || seg.len() == 4
                || (seg.len() >= 6 && seg.starts_with("20")))
                && seg.bytes().all(|b| b.is_ascii_digit());
            if dateish {
                s.truncate(pos);
            }
        }
        if s == before {
            break;
        }
    }
    s
}

/// 单名解析: 精确 → 别名剥离已在 normalize 中完成 → 前缀族唯一最佳匹配.
fn resolve_one(map: &HashMap<String, ModelMeta>, name: &str) -> Option<ModelMeta> {
    let q = normalize(name);
    if q.is_empty() {
        return None;
    }
    if let Some(m) = map.get(&q) {
        return Some(m.clone());
    }
    // 前缀族匹配: 库名 id 以查询名为前缀 (如 claude-sonnet-4-20250929 ← claude-sonnet-4),
    // 或查询名以前缀族开头但更长 (如 gpt-4o-2024-11-20 ← gpt-4o). 取长度差最小的候选.
    let mut best: Option<(usize, &ModelMeta)> = None;
    for (id, m) in map {
        let diff = if id.starts_with(&q) {
            id.len() - q.len()
        } else if q.starts_with(id.as_str()) {
            q.len() - id.len()
        } else {
            continue;
        };
        best = match best {
            Some((bd, _)) if diff >= bd => best,
            _ => Some((diff, m)),
        };
    }
    // 歧义检测: 重扫一遍确认最小距离唯一对应一个候选, 否则放弃 (避免张冠李戴).
    if let Some((bd, bm)) = best {
        let mut ties = 0usize;
        for id in map.keys() {
            let diff = if id.starts_with(&q) {
                id.len() - q.len()
            } else if q.starts_with(id.as_str()) {
                q.len() - id.len()
            } else {
                continue;
            };
            if diff == bd {
                ties += 1;
            }
        }
        if ties == 1 {
            return Some(bm.clone());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_strips_noise_and_dates() {
        assert_eq!(normalize("Kimi-K3-Free"), "kimi-k3");
        assert_eq!(normalize("muse-spark-1.2-contributor"), "muse-spark-1.2");
        assert_eq!(normalize("gpt-4o-2024-11-20"), "gpt-4o");
        assert_eq!(normalize("claude-sonnet-4-5-20250929"), "claude-sonnet-4-5");
        assert_eq!(normalize("DeepSeek-V4-CUSTOM"), "deepseek-v4");
        assert_eq!(normalize("glm-5"), "glm-5"); // 无噪声不动
        assert_eq!(normalize("claude-sonnet-4-5"), "claude-sonnet-4-5"); // 短数字段保留
    }

    #[test]
    fn parse_root_extracts_fields() {
        let root: serde_json::Value = serde_json::json!({
            "anthropic": { "models": {
                "claude-sonnet-4-5": {
                    "name": "Claude Sonnet 4.5",
                    "attachment": true, "reasoning": true, "tool_call": true,
                    "modalities": {"input": ["text","image"], "output": ["text"]},
                    "limit": {"context": 200000, "output": 64000},
                    "cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}
                }
            }},
            "openai": { "models": {
                "gpt-5.2": {
                    "reasoning": true, "tool_call": true,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 400000, "output": 128000}
                }
            }}
        });
        let ix = parse_root(&root);
        assert_eq!(ix.map.len(), 2);
        let m = ix.map.get("claude-sonnet-4-5").unwrap();
        assert_eq!(m.context, Some(200000));
        assert_eq!(m.output, Some(64000));
        assert!(m.vision);
        assert!(m.reasoning && m.tool_call);
        // 参考价 (USD/1M) 存在 (provider, 模型) 维度, 并带上来源 provider
        let c = ix.costs.get(&("anthropic".to_string(), "claude-sonnet-4-5".to_string()))
            .expect("cost 应按 (provider, model) 被解析");
        assert_eq!(c.input, Some(3.0));
        assert_eq!(c.output, Some(15.0));
        assert_eq!(c.cache_read, Some(0.3));
        assert_eq!(c.cache_write, Some(3.75));
        assert_eq!(c.provider, "anthropic");
        let g = ix.map.get("gpt-5.2").unwrap();
        assert!(!g.vision);
        assert_eq!(g.context, Some(400000));
        // 无 cost 字段 -> 无条目 (面板据此不显示参考价, 不得造出 0 价)
        assert!(!ix.costs.contains_key(&("openai".to_string(), "gpt-5.2".to_string())));
    }

    /// **核心保证**: 同名模型被多个 provider 收录时, 参考价按 provider 分别保存,
    /// 不得互相覆盖 (实测 deepseek-v4-flash 有 34 家收录, 报价从 0 到 0.435 不等).
    #[test]
    fn costs_are_indexed_per_provider_not_flattened() {
        let root: serde_json::Value = serde_json::json!({
            "deepseek": { "models": {
                "deepseek-v4-flash": {"cost": {"input": 0.15, "output": 0.6, "cache_read": 0.003}}
            }},
            "azure": { "models": {
                "deepseek-v4-flash": {"cost": {"input": 0.19, "output": 0.51}}
            }},
            "alibaba-token-plan": { "models": {
                "deepseek-v4-flash": {"cost": {"input": 0, "output": 0}}
            }}
        });
        let ix = parse_root(&root);
        let k = |p: &str| (p.to_string(), "deepseek-v4-flash".to_string());
        assert_eq!(ix.costs.get(&k("deepseek")).unwrap().input, Some(0.15));
        assert_eq!(ix.costs.get(&k("azure")).unwrap().input, Some(0.19));
        // 0 价是真实存在的订阅制档位, 必须原样保留 (不能被别的 provider 顶掉)
        let free = ix.costs.get(&k("alibaba-token-plan")).unwrap();
        assert_eq!(free.input, Some(0.0));
        assert_eq!(free.provider, "alibaba-token-plan");
        // 三家各自独立存在
        assert_eq!(ix.costs.len(), 3);
    }

    /// cost 解析的边界: 空对象 / 全空字段 -> None; 部分字段 -> 保留已知项.
    #[test]
    fn parse_root_handles_partial_cost() {
        let root: serde_json::Value = serde_json::json!({
            "p": { "models": {
                "a": {"cost": {}},
                "b": {"cost": {"input": null, "output": null}},
                "c": {"cost": {"input": 1.5}},
                "d": {"cost": "not-an-object"},
                "e": {"cost": {"output": 2.5, "cache_read": 0.1}}
            }}
        });
        let ix = parse_root(&root);
        let k = |n: &str| ("p".to_string(), n.to_string());
        assert!(!ix.costs.contains_key(&k("a")), "空 cost 对象不应产生条目");
        assert!(!ix.costs.contains_key(&k("b")), "全空字段不应产生条目");
        assert!(!ix.costs.contains_key(&k("d")), "非对象不应产生条目");
        let c = ix.costs.get(&k("c")).expect("部分字段应产生条目");
        assert_eq!(c.input, Some(1.5));
        assert_eq!(c.output, None);
        let e = ix.costs.get(&k("e")).expect("部分字段应产生条目");
        assert_eq!(e.output, Some(2.5));
        assert_eq!(e.cache_read, Some(0.1));
        assert_eq!(e.input, None);
    }

    #[test]
    fn resolve_exact_alias_and_unmatched() {
        let root: serde_json::Value = serde_json::json!({
            "p": { "models": {
                "kimi-k3": {"limit": {"context": 256000, "output": 32000},
                             "modalities": {"input":["text"],"output":["text"]}},
                "glm-5": {"limit": {"context": 128000, "output": 96000},
                           "modalities": {"input":["text"],"output":["text"]}}
            }}
        });
        let ix = parse_root(&root);
        // 精确命中
        assert!(resolve_one(&ix.map, "glm-5").is_some());
        // 别名剥噪命中
        let m = resolve_one(&ix.map, "kimi-k3-free").unwrap();
        assert_eq!(m.context, Some(256000));
        // 未收录
        assert!(resolve_one(&ix.map, "muse-spark-x").is_none());
        assert!(resolve_one(&ix.map, "").is_none());
    }

    #[test]
    fn resolve_prefix_family_prefers_closest() {
        let root: serde_json::Value = serde_json::json!({
            "p": { "models": {
                "gpt-4o": {"limit": {"context": 128000}},
                "gpt-4o-mini": {"limit": {"context": 128000}}
            }}
        });
        let ix = parse_root(&root);
        // 查询带日期尾巴 → 回落唯一前缀族 gpt-4o (gpt-4o-mini 也以 gpt-4o 开头?
        // "gpt-4o-mini" starts_with "gpt-4o-" ≠ "gpt-4o" 前缀判断用完整串:
        // "gpt-4o-2024..." starts_with "gpt-4o" ✓ 且 starts_with "gpt-4o-mini" ✗ → 唯一).
        let q = format!("{}-2024-11-20", "gpt-4o");
        let m = resolve_one(&ix.map, &q).unwrap();
        assert_eq!(m.context, Some(128000));
    }
}
