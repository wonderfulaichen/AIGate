//! 供应商配置 — 从 providers.json 加载, 构建 model→provider 路由表.
//!
//! 配置文件格式见 providers.json 中的中文说明.

use std::collections::HashMap;
use std::time::Duration;

use reqwest::Client;
use serde::{Deserialize, Serialize};
use tracing::{info, warn};

use crate::pricing::ModelPrice;

/// 单个模型的配置.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ModelConfig {
    /// 上游真实模型名 — 转发时替换 body 中的 model 字段.
    ///
    /// 用于模型名映射: 客户端用自定义 id (如 deepseek-v4-flash-custom),
    /// 但上游 API 要求真实模型名 (如 deepseek-v4-flash).
    /// 不设则原样转发.
    pub upstream_model: Option<String>,
    /// 思考强度: low / medium / high / max (仅对支持推理的模型生效).
    pub reasoning_effort: Option<String>,
    /// 是否为免费模型 (显式标记, 可选).
    ///
    /// 未显式标记时, [`is_free`](Self::is_free) 会自动回退识别
    /// `upstream_model` 字符串是否包含 `free` 或 `免费` (小写不敏感).
    #[serde(default)]
    pub free: Option<bool>,
    /// 额外注入到请求 body 的字段 (任意 JSON 键值对).
    #[serde(default)]
    pub extra_body: Option<serde_json::Value>,
    /// 模型级 API 格式覆盖: 可选 "openai" / "anthropic" / "responses".
    ///
    /// 不设置时回落到供应商级 [`ProviderConfig::api_format`].
    /// 用途: 同供应商(如 opencode go / zen 网关)下, 不同模型可能走不同端点
    /// (glm/kimi → /chat/completions, minimax / qwen3-plus·max → /messages,
    ///  grok / gpt-5.6-luna / muse-spark → /responses),
    /// 仅靠供应商级 `api_format` 无法区分, 需在此逐个标注.
    /// 点「获取」拉取上游模型时, 见 [`default_api_format`] 自动打标.
    #[serde(default)]
    pub api_format: Option<String>,
    /// **多协议候选集** (可选): 该上游模型同时支持哪些协议, 如 `["responses", "openai"]`.
    ///
    /// 与 [`Self::api_format`] 的关系: `api_format` 是**单一主协议**(向后兼容, 仍优先);
    /// 本字段声明"还能用哪些". 入口按**客户端来的协议优先匹配**候选集
    /// (见 [`ModelConfig::pick_api_format`]): 客户端打 `/v1/responses` 而候选集含
    /// `responses` 时就走 responses 原生直通, 避免跨协议往返转换;
    /// 客户端协议不在候选集时才回落到主协议 + 转换。
    ///
    /// 动机: 官方 API 是「同一个 model 名, 多个端点都能用」(DeepSeek 官方即如此),
    /// 而单值 `api_format` 做不到 —— 同一模型想同时用 chat 与 responses 只能起两个中转 ID。
    /// 声明候选集后, 一个 ID 即可覆盖多协议, 由请求决定实际走哪条 (更接近官方体验)。
    ///
    /// 空 / 省略 = 单协议 (行为与旧版完全一致)。
    #[serde(default)]
    pub api_formats: Option<Vec<String>>,
    /// 模型价格（元 / 百万 tokens）— 费用统计用.
    ///
    /// 优先级高于内置默认价格表（见 `crate::pricing`）. 缺省时回退内置表;
    /// 内置表也未覆盖的模型, 费用记为 0（"未配置价格"）.
    ///
    /// 字段: `{ "input_per_m": 1.0, "output_per_m": 2.0, "cache_read_per_m": 0.02 }`,
    /// `cache_read_per_m` 可选（KV Cache 命中价, 缺失则按输入价计）.
    #[serde(default)]
    pub price: Option<ModelPrice>,
    /// 是否剥离「带 tool_calls 的 assistant 消息」里的推理链 (默认 false = 保留).
    ///
    /// 必须**按模型**而非按供应商设置: 中转供应商 (zen / go / commandcodeAI 等) 一个条目下
    /// 混着多种协议 —— glm/kimi/deepseek 走 OpenAI, minimax/qwen3-plus 走 Anthropic,
    /// grok/gpt-5.6-luna 走 Responses. 供应商级开关会把 Anthropic 链路的模型一起剥掉.
    ///
    /// 约束因上游而异:
    /// - Anthropic: extended thinking + tool use 时 thinking 块须连签名原样回传, 剥掉会 400;
    /// - DeepSeek reasoner 系: 多轮工具调用要求 reasoning_content 与 tool_calls 并存;
    /// - 多数 OpenAI 兼容网关: 忽略该字段, 剥掉纯赚输入 token.
    #[serde(default)]
    pub strip_toolcall_reasoning: Option<bool>,
    /// 模型来源：manual（手写）或 fetched（拉取），用于 UI 区分（可选，缺省视为 fetched 兼容旧配置）。
    #[serde(default)]
    pub origin: Option<String>,
    /// 模型级循环守卫开关：Some(false) 关闭该模型的死循环检测，None 回落全局配置。
    #[serde(default)]
    pub loop_guard: Option<bool>,
}

impl ModelConfig {
    /// 判定模型是否免费.
    ///
    /// 规则: 显式 `free: true` 优先; 否则回退自动识别
    /// `upstream_model` (或 model id) 是否包含 `free` / `免费` (小写不敏感).
    pub fn is_free(&self, model_id: &str) -> bool {
        if self.free == Some(true) {
            return true;
        }
        if self.free == Some(false) {
            return false;
        }
        let s = self
            .upstream_model
            .as_deref()
            .unwrap_or(model_id)
            .to_lowercase();
        s.contains("free") || s.contains("免费")
    }

    /// 判定该模型是否走 Anthropic /messages 协议.
    ///
    /// 优先级: 模型级 `api_format` 覆盖 > 供应商级 `api_format` (回落).
    /// 例: go 供应商整体 openai, 但某 minimax 模型标 `api_format: "anthropic"`,
    ///     则该模型走 /messages, 其余模型仍走 /chat/completions.
    pub fn is_anthropic(&self, provider: &ProviderConfig) -> bool {
        match &self.api_format {
            Some(f) => f == "anthropic",
            None => provider.is_anthropic(),
        }
    }

    /// 判定该模型是否走 OpenAI Responses API (/v1/responses) 协议.
    ///
    /// 优先级同 `is_anthropic`: 模型级 `api_format` 覆盖 > 供应商级.
    pub fn is_responses(&self, provider: &ProviderConfig) -> bool {
        match &self.api_format {
            Some(f) => f == "responses",
            None => provider.is_responses(),
        }
    }

    /// 三层回落解析模型的实际 API 格式:
    ///   1) 模型级 `api_format` 显式配置
    ///   2) 供应商级 `api_format` 显式配置
    ///   3) [`default_api_format`] 按模型名前缀自动推断
    ///
    /// 返回值: `"anthropic"` / `"responses"` / `"openai"` (或 `None` 等同 openai).
    pub fn resolve_api_format(&self, provider: &ProviderConfig, model_id: &str) -> Option<String> {
        // 1) 模型级显式配置
        if let Some(ref f) = self.api_format {
            return Some(f.clone());
        }
        // 2) 供应商级显式配置
        if let Some(ref f) = provider.api_format {
            return Some(f.clone());
        }
        // 3) 按模型名自动推断 (grok-*→responses, claude-*→anthropic, 等)
        // 兼容中转别名 (如 go-muse) 与上游真名 (muse-spark-1.2-contributor) 双口径
        let upstream_id = self.upstream_model.as_deref().unwrap_or(model_id);
        default_api_format(&provider.name, model_id)
            .or_else(|| default_api_format(&provider.name, upstream_id))
            .map(|s| s.to_string())
    }

    /// 按**客户端入口协议**在候选集里挑实际使用的上游协议 (多协议模型专用).
    ///
    /// `client_format`: 客户端打进来的入口协议 —— `"anthropic"` (/v1/messages) /
    /// `"responses"` (/v1/responses) / `"openai"` (/v1/chat/completions)。
    ///
    /// 规则 (按优先级):
    ///   1. `api_formats` 为空 → 返回 `None`, 调用方走既有的 [`Self::resolve_api_format`]
    ///      (**单协议语义完全不变**, 向后兼容)。
    ///   2. 候选集含客户端入口协议 → 用它 (可走原生直通, 无跨协议转换损耗)。
    ///   3. 否则用候选集第一项 (声明顺序即优先级, 由用户/推断决定谁是主协议)。
    ///
    /// 注意: 这里不看 `api_format` —— 声明了候选集即表示"以候选集为准";
    /// 未声明候选集时 `api_format` 仍走原路径。两者互斥, 避免"候选集与主协议谁优先"的歧义。
    pub fn pick_api_format(&self, client_format: &str) -> Option<&str> {
        let cands = self.api_formats.as_ref()?;
        let list: Vec<&str> = cands
            .iter()
            .map(|s| s.trim())
            .filter(|s| matches!(*s, "openai" | "anthropic" | "responses"))
            .collect();
        if list.is_empty() {
            return None;
        }
        // 客户端入口协议优先: 命中即可直通, 不必跨协议往返 (这才是"由请求决定协议")
        let client = normalize_format(client_format);
        if let Some(hit) = list.iter().find(|f| **f == client.as_str()) {
            return Some(hit);
        }
        // 回落: 候选集首项 (声明顺序即优先级)
        list.first().copied()
    }
}

/// 单个供应商的配置.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ProviderConfig {
    /// 供应商名称 (仅用于日志显示).
    pub name: String,
    /// OpenAI 兼容的 chat/completions 端点 URL.
    pub endpoint: String,
    /// 从哪个环境变量读取 API key.
    pub api_key_env: String,
    /// 未配置环境变量时的默认值 (如 Zen 的 "public").
    pub api_key_default: Option<String>,
    /// **实测支持哪些协议** (协议探测的结果, 由「拉取模型」时自动写入)。
    ///
    /// 这是**服务方级**信息 —— 探测探的是端点路径 (`/chat/completions` 等), 故同一供应商下
    /// 所有模型共享同一份结果; 这正是界面上展示"该上游支持哪些协议"的数据来源。
    ///
    /// 为空 = 未探测过, 或探测未能判定 (WAF 拦截 / 该站对所有路径状态一致)。
    /// 与模型级 `api_formats` 的区别: 后者是**该模型实际使用**的协议候选 (可手改),
    /// 本字段只是"实测能力"的记录, 供展示与用户决策, 不直接参与转发。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub probed_protocols: Option<Vec<String>>,
    /// 余额查询 API 端点 (可选).
    #[serde(default)]
    pub balance_endpoint: Option<String>,
    /// 额外注入的 HTTP 请求头.
    #[serde(default)]
    pub headers: Option<HashMap<String, String>>,
    /// 上游 API 格式: 缺省 "openai" (chat/completions);
    /// 模型级 API 格式覆盖: `"openai"` (默认/可省略), `"anthropic"`, 或 `"responses"`.
    /// - `"openai"`: 走 `/v1/chat/completions` (默认)
    /// - `"anthropic"`: 走 `/v1/messages` (网关自动做 OpenAI↔Anthropic 双向转换)
    /// - `"responses"`: 走 `/v1/responses` (OpenAI Responses API, 网关自动转换为 chat/completions 格式)
    /// 覆盖 `default_api_format` 的自动推断, 仅对本供应商生效.
    #[serde(default)]
    pub api_format: Option<String>,
    /// Anthropic /messages 协议的独立端点 URL (可选).
    ///
    /// 当模型走 Anthropic 协议 (`api_format: "anthropic"` 或自动推断为 anthropic) 时,
    /// 若本字段有值则使用此端点, 否则回落把供应商 `endpoint` 中的 `/chat/completions`
    /// 改写为 `/messages` (OpenCode 同域名不同路径场景, 如 DeepSeek / api.deepseek.com).
    /// 这样同一供应商可同时暴露 OpenAI 与 Anthropic 两种协议端点 (参考 DeepSeek 官方设计).
    #[serde(default)]
    pub endpoint_anthropic: Option<String>,
    /// Responses /v1/responses 协议的独立端点 URL (可选).
    ///
    /// 当模型走 Responses 协议 (`api_format: "responses"` 或自动推断为 responses) 时,
    /// 若本字段有值则使用此端点, 否则回落把供应商 `endpoint` 中的 `/chat/completions`
    /// 改写为 `/responses` (如 opencode go 网关).
    #[serde(default)]
    pub endpoint_responses: Option<String>,
    /// 供应商级 prompt caching 开关 (仅 Anthropic /messages 协议生效).
    /// 默认 true: 在 system 末块 + 最后一条 user 消息注入 cache_control, 使上游第二轮起
    /// 命中 prompt cache (input 按 0.1x 计 + 一次性写入费). 个别网关不支持/会改写 client
    /// cache_control 时报错, 可设 false 关闭. 仅对走 /messages 的模型生效.
    #[serde(default)]
    pub prompt_cache: Option<bool>,
    /// OpenAI 兼容协议 (含 DeepSeek) 的显式前缀缓存打标开关.
    ///
    /// 开启后, 非流式请求的 messages 会在 system 与最后一条 user 消息上注入
    /// `cache_control: {type: ephemeral}`, 显式标记前缀缓存断点, 让 OpenAI/DeepSeek 等
    /// 支持该字段的上游按此前缀缓存 KV (命中后 input 大幅降费). 与 Anthropic 路径的
    /// cache_control 注入对称. 默认关闭 (依赖各上游自动前缀缓存, 不主动注入,
    /// 避免不被支持的网关因未知字段报错); 显式开启即表示上游支持该字段. 仅对走 OpenAI 协议的模型生效.
    #[serde(default)]
    pub openai_cache_control: Option<bool>,
    /// 该供应商支持的模型, key 是 model id.
    pub models: HashMap<String, ModelConfig>,
    /// 上游请求体大小上限 (字节, 可选). 超过则网关在转发前直接返回 413 中文提示,
    /// 避免盲目打到上游被裸拒 (如 opencode.ai/zen/go 限制约 1MB). 默认不限制.
    #[serde(default)]
    pub max_request_body_bytes: Option<usize>,
}

impl ProviderConfig {
    /// 是否使用 Anthropic /messages 协议.
    pub fn is_anthropic(&self) -> bool {
        self.api_format.as_deref() == Some("anthropic")
    }
    /// 是否使用 OpenAI Responses API (/v1/responses) 协议.
    pub fn is_responses(&self) -> bool {
        self.api_format.as_deref() == Some("responses")
    }
}

/// providers.json 的顶层结构.
#[derive(Debug, Deserialize)]
struct ProvidersFile {
    /// 供应商列表 (其他以 _ 开头的字段会被忽略).
    providers: Vec<ProviderConfig>,
}

/// 运行时路由条目 — 模型中转ID → (供应商配置, 模型配置).
///
/// 注意: 客户端请求里的 `model` 字段是**模型中转ID**(对外暴露的别名, 可任取符合用途的名称),
/// 由 providers.json 的键定义; `upstream_model` 才是上游真实模型 ID.
#[derive(Debug, Clone)]
pub struct RouteEntry {
    pub provider: ProviderConfig,
    pub model: ModelConfig,
}

/// 全局路由表.
#[derive(Debug, Clone)]
pub struct ProviderRegistry {
    /// model id → 路由条目.
    routes: HashMap<String, RouteEntry>,
    /// 供应商配置列表 (用于健康检查和面板展示).
    providers: Vec<ProviderConfig>,
}

impl ProviderRegistry {
    /// 从 providers.json 文件加载.
    ///
    /// 从 providers.json 文件加载.
    pub fn load(path: &str) -> Result<Self, String> {
        let content = std::fs::read_to_string(path)
            .map_err(|e| format!("无法读取 {path}: {e}"))?;
        Self::from_str(&content)
    }

    /// 从 JSON 字符串加载.
    pub fn from_str(json: &str) -> Result<Self, String> {
        let file: ProvidersFile = serde_json::from_str(json)
            .map_err(|e| format!("解析 providers.json 失败: {e}"))?;

        let mut routes = HashMap::new();
        for provider in &file.providers {
            info!(
                "providers: loaded '{}' -> {} ({} models, key_env={})",
                provider.name,
                provider.endpoint,
                provider.models.len(),
                provider.api_key_env
            );
            for (model_id, model_cfg) in &provider.models {
                if routes.contains_key(model_id) {
                    warn!("providers: duplicate model '{model_id}', later entry overwrites");
                }
                routes.insert(model_id.clone(), RouteEntry {
                    provider: provider.clone(),
                    model: model_cfg.clone(),
                });
            }
        }
        info!("providers: {} models registered", routes.len());

        Ok(Self {
            routes,
            providers: file.providers,
        })
    }

    /// 按模型中转ID查找路由条目 (返回克隆, 支持 RwLock 场景).
    pub fn lookup(&self, model_id: &str) -> Option<RouteEntry> {
        self.routes.get(model_id).cloned()
    }

    /// 获取所有已注册的模型中转ID (用于 /v1/models).
    pub fn model_ids(&self) -> Vec<&str> {
        self.routes.keys().map(|s| s.as_str()).collect()
    }

    /// 获取供应商的 API key (Key 是 provider 的子资源, 按 provider.name 索引).
    ///
    /// 优先级: 面板设置值 (keys.json[provider.name]) → 环境变量 (api_key_env)
    /// → providers.json 内置默认值 (api_key_default).
    pub async fn api_key(&self, provider: &ProviderConfig, key_store: &crate::keys::KeyStore) -> Result<String, String> {
        if let Some(k) = key_store.get_for_provider(&provider.name).await {
            return Ok(k);
        }
        if let Ok(v) = std::env::var(&provider.api_key_env) {
            if !v.is_empty() {
                return Ok(v);
            }
        }
        if let Some(default) = &provider.api_key_default {
            Ok(default.clone())
        } else {
            Err(format!(
                "API key not set: provider '{}' has no panel key, env {} is empty, and no default",
                provider.name, provider.api_key_env
            ))
        }
    }

    /// 获取所有供应商配置 (克隆, 支持 RwLock 场景).
    pub fn providers(&self) -> Vec<ProviderConfig> {
        self.providers.clone()
    }

    /// 该中转 ID 此刻生效的配置价 (元/百万 token), 未配置返回 `None`.
    ///
    /// 取的是路由条目自带的 model 配置 —— 即**本次请求实际使用的**那一份价格,
    /// 用于写日志时结算历史账单快照 (见 `admin::LogBuffer::push`).
    pub fn price_of(&self, model_id: &str) -> Option<crate::pricing::ModelPrice> {
        self.routes.get(model_id).and_then(|r| r.model.price)
    }

    /// 从文件热重载配置.
    pub fn reload(&mut self, path: &str) -> Result<(), String> {
        let content = std::fs::read_to_string(path).map_err(|e| format!("读取 {path} 失败: {e}"))?;
        let new = Self::from_str(&content)?;
        info!("providers: {} models reloaded", new.routes.len());
        self.routes = new.routes;
        self.providers = new.providers;
        Ok(())
    }

    /// 序列化为格式化 JSON (不含 _ 开头的注释字段).
    pub fn to_json(&self) -> Result<String, String> {
        let obj = serde_json::json!({ "providers": self.providers });
        serde_json::to_string_pretty(&obj).map_err(|e| e.to_string())
    }

    /// 将上游拉取到的模型 ID 合并进指定供应商的 `models` 表.
    ///
    /// - 新增未存在的模型 ID, 中转 ID 生成为 **`供应商名/上游模型ID`**, `upstream_model`
    ///   存原始上游 ID (原样转发), `reasoning_effort` 留空 (由客户端按模型自行调节思考档位),
    ///   `extra_body` 留空.
    /// - **为什么上游 ID 自带斜杠时也加前缀**: OpenRouter / commandcodeAI 等上游用
    ///   「作者/模型」命名 (如 `deepseek/deepseek-v4-flash`). 若原样用作中转 ID, 同一 ID
    ///   会在多家供应商间撞车, 而路由表是 `HashMap`(一个 ID 一条路由), 后加载者静默覆盖
    ///   前者 —— 被覆盖的条目在面板上可见可改却永不生效, 且无任何提示. 加前缀后每个
    ///   供应商的条目都拥有独立中转 ID (形如 `commandcodeAI/deepseek/deepseek-v4-flash`).
    /// - **去重的唯一标准 = 上游模型 ID**: 只要本地已有某个条目 (任意中转别名) 的 `upstream_model`
    ///   等于待拉取的 ID, 就跳过. **不对比中转别名(key)、也不对比思考档位(reasoning_effort)**.
    ///   例: 文件里 `go-flash -> deepseek-v4-flash`, 上游返回的 `deepseek-v4-flash` 不再重复加入.
    ///
    /// 返回 `(新增数量, 跳过数量)`. 仅修改内存态, 调用方需自行 `to_json()` 写回文件并 `reload()`.
    pub fn add_models(&mut self, name: &str, ids: &[String]) -> (usize, usize) {
        let mut added = 0usize;
        let mut skipped = 0usize;
        if let Some(provider) = self.providers.iter_mut().find(|p| p.name == name) {
            let prov_name = provider.name.clone();
            for id in ids {
                // 去重: 本地是否已有条目的 upstream_model == 该上游 ID (不对比别名 / 思考档位)
                let already_exists = provider
                    .models
                    .values()
                    .any(|m| m.upstream_model.as_deref() == Some(id.as_str()));
                if already_exists {
                    skipped += 1;
                    continue;
                }
                // 中转 ID: 恒加「供应商/」前缀 (幂等: 已带本前缀则不重复加).
                let transit_id = transit_model_id(&prov_name, id);
                provider.models.insert(
                    transit_id,
                    ModelConfig {
                        upstream_model: Some(id.clone()),
                        reasoning_effort: None,
                        free: None,
                        extra_body: None,
                        api_format: default_api_format(&provider.name, id).map(|s| s.to_string()),
                        api_formats: None,
                        price: None,
                        strip_toolcall_reasoning: None,
                        origin: Some("fetched".to_string()),
                        loop_guard: None,
                    },
                );
                added += 1;
            }
        }
        (added, skipped)
    }
}

/// 由「供应商名 + 上游模型 ID」构造模型中转 ID: `供应商/上游模型ID`.
///
/// **上游 ID 自带斜杠时同样加前缀**: OpenRouter / commandcodeAI 等上游以「作者/模型」
/// 命名 (如 `deepseek/deepseek-v4-flash`). 若原样用作中转 ID, 同一 ID 会挂在多家供应商下,
/// 而路由表是 `HashMap`(一个 ID 只能有一条路由), 后加载者静默覆盖前者 —— 被覆盖的条目
/// 在面板上可见可改却永不生效, 且没有任何提示. 加前缀后各家条目拥有独立中转 ID.
///
/// 幂等: 上游 ID 已等于供应商名或已带 `供应商/` 前缀时, 原样返回 (重复拉取/导入不叠加前缀).
pub fn transit_model_id(provider_name: &str, upstream_id: &str) -> String {
    let p = provider_name.trim();
    let u = upstream_id.trim();
    if p.is_empty() {
        return u.to_string();
    }
    if u == p || u.starts_with(&format!("{p}/")) {
        return u.to_string();
    }
    format!("{p}/{u}")
}

/// 按官方网关清单, 为「供应商 + 模型 ID」推断默认 API 格式 (OpenAI / Anthropic / Responses).
///
/// 数据来源: opencode 官方文档 `go.mdx` / `zen.mdx` (已对照源码核实):
///
/// - **go 网关** (`/zen/go/v1`):
///   - `glm` / `kimi` / `deepseek` / `mimo` → `/chat/completions` (OpenAI)
///   - `minimax-*`、`qwen3.*-plus/max` (含 m2.5) → `/messages` (Anthropic)
///   - `grok-*` / `gpt-5.6-luna` / `muse-spark-*` → `/responses` (OpenAI Responses API)
/// - **zen 网关** (`/zen/v1`):
///   - `minimax-m2.5/m2.7`、`deepseek`、`glm`、`kimi` → `/chat/completions` (OpenAI)
///   - `claude-*`、`qwen3.5/3.6/3.7-plus/max` → `/messages` (Anthropic)
///   - `gpt-*` / `grok-*` → `/responses` (OpenAI Responses API)
///
/// 返回 `Some("anthropic")` / `Some("responses")` / `None` (回落 OpenAI, 即不写 `api_format`).
pub fn default_api_format(provider_name: &str, model_id: &str) -> Option<&'static str> {
    let id = model_id.to_lowercase();
    let provider = provider_name.to_lowercase();

    // 通用规则: 任意供应商下, 模型名含 "claude" 一律走 Anthropic /messages.
    if id.contains("claude") {
        return Some("anthropic");
    }
    // 通用规则: 任意供应商下, 模型名含 "grok" 一律走 Responses API.
    if id.contains("grok") {
        return Some("responses");
    }

    match provider.as_str() {
        "go" => {
            // minimax-* 与 qwen3.*-plus/max 走 /messages
            if id.starts_with("minimax")
                || (id.starts_with("qwen3") && (id.contains("-plus") || id.contains("-max")))
            {
                Some("anthropic")
            }
            // gpt-5.6-luna / muse-spark-* 走 /responses
            else if id.starts_with("gpt-5") || id.starts_with("muse-spark") {
                Some("responses")
            } else {
                None
            }
        }
        "zen" => {
            // qwen3*-plus/max 走 /messages; claude-* 由上方通用规则处理;
            if id.starts_with("qwen3") && (id.contains("-plus") || id.contains("-max")) {
                Some("anthropic")
            }
            // gpt-* 走 /responses (gpt-* 由上方 grok 通用规则不覆盖, 此处兜底)
            else if id.starts_with("gpt-") {
                Some("responses")
            } else {
                None
            }
        }
        _ => None,
    }
}

/// 入口协议名归一: 把客户端入口的称呼统一成配置里用的三个值.
///
/// 入口与配置的用词本就不完全一致 (`/v1/chat/completions` 习惯叫 "chat",
/// 而配置里写 "openai"), 集中在此转换, 避免各处手写映射表而漂移.
/// 未知值原样返回 (不猜), 由调用方的候选集匹配决定是否命中.
pub fn normalize_format(name: &str) -> String {
    match name.trim().to_ascii_lowercase().as_str() {
        "openai" | "openai_chat" | "chat" | "chat_completions" => "openai".to_string(),
        "anthropic" | "messages" | "claude" => "anthropic".to_string(),
        "responses" | "openai_responses" | "response" => "responses".to_string(),
        other => other.to_string(),
    }
}

/// **零成本协议推测** (探测前的前两层): 端点 URL 路径 → 内置 host 目录.
///
/// 命中即返回**候选协议集**, `None` 表示两层都拿不到 (调用方走探测兜底)。
///
/// 层次与依据:
/// 1. **URL 路径**: 不少供应商把协议写在路径里 —— 参考项目 cc-switch 的预置条目正是如此
///    (`https://api.minimaxi.io/anthropic/v1`、`https://api.tbox.cn/api/llm/v1/chat/completions`)。
///    实测覆盖率不高 (models.dev 197 个端点里仅 5.6% 带信号, 多数只是裸 `/v1`), 但零成本且准确。
/// 2. **内置 host 目录** ([`crate::provider_catalog`]): 按**服务方 host** 查表, 实测覆盖 92%。
///    必须是按服务方而非模型 —— 同名模型跨服务方协议会不同 (见 [`probe_protocols`] 文档)。
///
/// 注意: 这里返回的是"**已知支持**"的协议集, 不像探测能发现"还支持哪些"。
/// 例如 deepseek 在目录里记为 openai, 但它实际也支持 responses —— 这类多协议信息
/// 只有探测能拿到。故调用方应把本函数结果当作**快速路径**, 需要"完整清单"时仍用探测。
pub fn free_protocol_guess(provider: &ProviderConfig) -> Option<Vec<String>> {
    let ep = provider.endpoint.trim_end_matches('/');
    let lower = ep.to_ascii_lowercase();

    // ① URL 路径信号 (顺序即优先级: 具体段优先)
    //
    // ⚠️ 只认**有区分度**的路径段: `anthropic` / `responses` 段意味着服务方明确以该协议对外。
    // **不能把 `chat` / `completions` 当信号** —— `/v1/chat/completions` 是所有 OpenAI 兼容
    // 端点的默认路径, 几乎每个网关都这么写 (含各类中转), 认它会让第一层"总是命中",
    // 目录与探测永远走不到 (此前的实现即踩了这个坑, 由单测 `free_guess_returns_none_when_unknown` 抓出)。
    // 反过来: **裸 `/v1` 不带任何协议段时, 恰恰说明"没拿到信号", 应交给目录/探测判断**。
    for seg in lower.split('/') {
        match seg {
            "anthropic" | "messages" => return Some(vec!["anthropic".to_string()]),
            "responses" => return Some(vec!["responses".to_string()]),
            _ => {}
        }
    }

    // ② 内置 host 目录 (按服务方)
    //    用 reqwest 的 Url 解析而非字符串切分: 免去 userinfo/端口/查询串的手工处理。
    let host = reqwest::Url::parse(ep)
        .ok()
        .and_then(|u| u.host_str().map(|h| h.to_string()))
        .unwrap_or_default();
    if let Some(proto) = crate::provider_catalog::protocol_for_host(&host) {
        return Some(vec![proto.to_string()]);
    }
    None
}

/// 探测某上游**实际支持哪些协议** —— 用 HEAD 请求判定端点路径是否存在.
///
/// **零 token 成本**: 早期实现用 `POST + max_tokens=1` 探测, 虽小但仍会生成 token;
/// 对贵模型或按 output 计费的推理模型并不划算。改用 **HEAD**(无请求体 → 不触发生成),
/// 判据同样成立 —— 实测 DeepSeek: `/chat/completions` 与 `/responses` 返 **405**
/// (Method Not Allowed, 说明路径存在) 而 `/messages` 返 **404** (不存在)。
///
/// 判据:
/// - **404 / 501 → 不存在**
/// - **405 / 400 / 401 / 403 / 415 / 422 / 429 / 2xx → 存在**(只是不接受 HEAD 或参数不合)
/// - **对照探针**: 若一个必定不存在的路径也返"存在类"状态 (部分网关对所有路径都返
///   401/403), 则该站不可判别 → 返回空 (不猜), 由用户手配。
///
/// 为什么需要探测: 协议**不在模型元信息里** (models.dev 模型级字段只有 context/cost/
/// modalities/reasoning; 且实测同名模型跨服务方协议会不同 —— claude-opus-4-8 在 anthropic
/// 官方走 anthropic 协议、在 302ai 等转售商走 openai 协议, 即协议取决于**谁在提供服务**)。
/// 故只能按服务方判断: 先查内置目录 (见 [`crate::provider_catalog`]), 查不到才探测。
///
/// 部分网关的 WAF 会拦可疑请求 —— 带上真实 key 与 provider.headers 可显著提高成功率。
pub async fn probe_protocols(
    client: &Client,
    provider: &ProviderConfig,
    key: &str,
    _model_hint: Option<&str>,
) -> Vec<String> {
    let ep = provider.endpoint.trim_end_matches('/');
    let base = {
        let mut b = ep.to_string();
        for suf in ["/chat/completions", "/responses", "/messages"] {
            if let Some(stripped) = b.strip_suffix(suf) {
                b = stripped.to_string();
                break;
            }
        }
        b
    };

    // 单次 HEAD (个别网关不支持 HEAD → 回退 GET, 同样无请求体、不生成 token)
    async fn hit(
        client: &Client,
        provider: &ProviderConfig,
        key: &str,
        url: &str,
    ) -> Option<u16> {
        for method in [reqwest::Method::HEAD, reqwest::Method::GET] {
            let mut req = client.request(method.clone(), url);
            if !key.is_empty() {
                req = req.header("Authorization", format!("Bearer {key}"));
            }
            // Anthropic 端点要求版本头
            req = req.header("anthropic-version", "2023-06-01");
            if let Some(headers) = &provider.headers {
                for (k, v) in headers {
                    req = req.header(k, v);
                }
            }
            if let Ok(resp) = req.timeout(Duration::from_secs(20)).send().await {
                let code = resp.status().as_u16();
                // HEAD 被 405 拒绝时用 GET 复验一次 (405 本身已说明路径存在, 但 GET 能拿到更准的状态)
                if method == reqwest::Method::HEAD && code == 405 {
                    continue;
                }
                return Some(code);
            }
            // 网络错误: 换方法再试一次
        }
        None
    }

    let exists = |code: u16| -> bool {
        // 404/501 = 路径不存在; 其余 (含 405 方法不允许) 都说明路由已匹配到该路径
        !matches!(code, 404 | 501)
    };

    // ── 对照探针: 探一个必定不存在的路径 ──
    // 部分网关对所有路径都返 401/403 (统一鉴权/反爬), 此时逐路径探测毫无判别力。
    // 先探这个"哨兵": 若连它都算"存在", 说明该站不可判 → 直接返回空, 不给假结果。
    let sentinel = hit(
        client,
        provider,
        key,
        &format!("{base}/aigate-probe-nonexistent-9f3c"),
    )
    .await;
    if let Some(c) = sentinel {
        if exists(c) {
            return Vec::new(); // 不可判别: 该站对所有路径状态一致
        }
    }

    let mut found: Vec<String> = Vec::new();
    for (proto, path) in [
        ("openai", "/chat/completions"),
        ("anthropic", "/messages"),
        ("responses", "/responses"),
    ] {
        if let Some(code) = hit(client, provider, key, &format!("{base}{path}")).await {
            if exists(code) {
                found.push(proto.to_string());
            }
        }
    }
    found
}

/// 从上游 `/v1/models` 拉取模型 ID 列表.
///
/// - 端点推导: 把 provider.endpoint 中的 `/chat/completions` 替换为 `/models`
///   (兼容 DeepSeek / opencode(zen/go) 等 OpenAI 兼容网关).
/// - 鉴权: 使用 provider 真实 key (`Bearer`) + provider.headers (如有).
/// - 解析兼容 OpenAI 标准 `{object:"list", data:[{id}]}`、部分网关 `{models:[...]}`
///   及裸数组 `[...]`, 见 [`extract_model_ids`].
pub async fn fetch_models_from_upstream(
    client: &Client,
    provider: &ProviderConfig,
    key: &str,
) -> Result<Vec<String>, String> {
    // /models 端点推导: 兼容 OpenAI (/chat/completions) 与 Anthropic (/messages) 两类端点.
    // 若 endpoint 是裸 base URL (如 https://api.xxx.com/v1), 替换无命中时自动追加 /models.
    let ep = provider.endpoint.trim_end_matches('/');
    let models_url = {
        let after = ep
            .replace("/chat/completions", "/models")
            .replace("/messages", "/models");
        if after != ep {
            after // 已从已知后缀转换
        } else {
            format!("{ep}/models") // 裸 base URL → 追加 /models
        }
    };
    let mut req = client.get(&models_url);
    if !key.is_empty() {
        req = req.header("Authorization", format!("Bearer {key}"));
    }
    if let Some(headers) = &provider.headers {
        for (k, v) in headers {
            req = req.header(k, v);
        }
    }
    let resp = req
        .timeout(Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| format!("拉取模型请求失败: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("上游返回 HTTP {} 拉取模型失败", resp.status().as_u16()));
    }
    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("解析上游响应 JSON 失败: {e}"))?;
    extract_model_ids(&body)
}

/// 从上游 `/v1/models` 的 JSON 响应中提取模型 ID 列表.
///
/// 兼容结构:
/// - OpenAI 标准: `{ "object": "list", "data": [ { "id": "..." } ] }`
/// - 部分网关: `{ "models": [ { "id": "..." } ] }`
/// - 裸数组: `[ "model-a", "model-b" ]` 或 `[ { "id": "..." } ]`
fn extract_model_ids(body: &serde_json::Value) -> Result<Vec<String>, String> {
    let arr = body
        .get("data")
        .and_then(|v| v.as_array())
        .or_else(|| body.get("models").and_then(|v| v.as_array()))
        .or_else(|| body.as_array());
    let arr = match arr {
        Some(a) => a,
        None => {
            return Err("响应中未找到模型列表 (期望 data / models 数组或顶层数组)".to_string())
        }
    };
    let mut ids = Vec::new();
    for item in arr {
        if let Some(id) = item.get("id").and_then(|v| v.as_str()) {
            ids.push(id.to_string());
        } else if let Some(id) = item.as_str() {
            ids.push(id.to_string());
        }
    }
    if ids.is_empty() {
        return Err("上游返回的模型列表为空".to_string());
    }
    Ok(ids)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE_JSON: &str = r#"
    {
      "providers": [
        {
          "name": "test-zen",
          "endpoint": "https://example.com/v1/chat/completions",
          "api_key_env": "TEST_ZEN_KEY",
          "api_key_default": "public",
          "models": {
            "free-model": {}
          }
        },
        {
          "name": "test-go",
          "endpoint": "https://example.com/go/v1/chat/completions",
          "api_key_env": "TEST_GO_KEY",
          "models": {
            "pro-model": { "reasoning_effort": "high" }
          }
        }
      ]
    }
    "#;

    #[test]
    fn is_free_detects_flag_and_name() {
        let base = |upstream: Option<&str>, free: Option<bool>| ModelConfig {
            upstream_model: upstream.map(|s| s.to_string()),
            reasoning_effort: None,
            free,
            extra_body: None,
            api_format: None,
            api_formats: None,
            price: None,
            strip_toolcall_reasoning: None,
            origin: None,
            loop_guard: None,
        };
        // 显式 free: true 优先
        assert!(base(Some("gpt-4o"), Some(true)).is_free("gpt-4o"));
        // 显式 free: false 覆盖命名回退
        assert!(!base(Some("deepseek-v4-flash-free"), Some(false)).is_free("x"));
        // 未标记时回退 upstream_model 含 free (大小写不敏感)
        assert!(base(Some("DeepSeek-V4-Flash-FREE"), None).is_free("x"));
        // 未标记时回退含中文"免费"
        assert!(base(Some("qwen-免费"), None).is_free("x"));
        // 未标记且不含关键词 → 非免费
        assert!(!base(Some("gpt-4o"), None).is_free("gpt-4o"));
        // upstream_model 为空时回退 model_id
        assert!(base(None, None).is_free("kimi-free"));
        assert!(!base(None, None).is_free("kimi-pro"));
    }

    #[test]
    fn loads_providers_and_models() {
        let reg = ProviderRegistry::from_str(SAMPLE_JSON).unwrap();
        assert_eq!(reg.model_ids().len(), 2);
        assert!(reg.lookup("free-model").is_some());
        assert!(reg.lookup("pro-model").is_some());
        assert!(reg.lookup("unknown").is_none());
    }

    #[test]
    fn routes_to_correct_endpoint() {
        let reg = ProviderRegistry::from_str(SAMPLE_JSON).unwrap();
        let entry = reg.lookup("pro-model").unwrap();
        assert_eq!(entry.provider.endpoint, "https://example.com/go/v1/chat/completions");
        assert_eq!(entry.model.reasoning_effort.as_deref(), Some("high"));
    }

    #[test]
    fn api_key_falls_back_to_default() {
        std::env::remove_var("TEST_ZEN_KEY");
        let reg = ProviderRegistry::from_str(SAMPLE_JSON).unwrap();
        let entry = reg.lookup("free-model").unwrap();
        assert_eq!(entry.provider.api_key_default.as_deref(), Some("public"));
    }

    #[test]
    fn add_models_adds_new_skips_existing() {
        let mut reg = ProviderRegistry::from_str(SAMPLE_JSON).unwrap();
        // 既有条目 go-flash -> deepseek-v4-flash (上游模型 ID 已被覆盖)
        {
            let p = reg
                .providers
                .iter_mut()
                .find(|p| p.name == "test-zen")
                .unwrap();
            p.models.insert(
                "go-flash".to_string(),
                ModelConfig {
                    upstream_model: Some("deepseek-v4-flash".to_string()),
                    reasoning_effort: None,
                    free: None,
                    extra_body: None,
                    api_format: None,
                    api_formats: None,
                    price: None,
                    strip_toolcall_reasoning: None,
                    origin: None,
                    loop_guard: None,
                },
            );
        }
        let ids = vec![
            "new-model-a".to_string(),
            "new-model-b".to_string(),
            "deepseek-v4-flash".to_string(), // 已有 upstream_model 覆盖, 应跳过
        ];
        let (added, skipped) = reg.add_models("test-zen", &ids);
        assert_eq!(added, 2);
        assert_eq!(skipped, 1);
        // 新增的模型: 中转 ID = 供应商/上游ID, upstream_model 存原始上游 ID, reasoning_effort 留空
        let provider = reg.providers.iter().find(|p| p.name == "test-zen").unwrap();
        let m = provider.models.get("test-zen/new-model-a").unwrap();
        assert_eq!(m.upstream_model.as_deref(), Some("new-model-a"));
        assert!(m.reasoning_effort.is_none());
        // 上游已覆盖的 ID 未作为新 key 被加入
        assert!(!provider.models.contains_key("test-zen/deepseek-v4-flash"));
    }

    /// 中转 ID 恒加「供应商/」前缀; 上游 ID 自带斜杠 (OpenRouter 风格) 也一样加,
    /// 否则同名 ID 会在多家供应商间撞车, 被路由表静默覆盖.
    #[test]
    fn transit_id_always_prefixes_provider() {
        // 裸 ID
        assert_eq!(transit_model_id("go", "deepseek-v4-flash"), "go/deepseek-v4-flash");
        // 上游自带斜杠: 仍加前缀, 得到两段斜杠 (供应商/作者/模型)
        assert_eq!(
            transit_model_id("commandcodeAI", "deepseek/deepseek-v4-flash"),
            "commandcodeAI/deepseek/deepseek-v4-flash"
        );
        // 同一上游 ID 在不同供应商下产生不同中转 ID -> 不再冲突
        let a = transit_model_id("Openrouter", "deepseek/deepseek-v4-flash");
        let b = transit_model_id("commandcodeAI", "deepseek/deepseek-v4-flash");
        assert_ne!(a, b);
    }

    /// 前缀必须幂等: 已带本供应商前缀时不重复叠加 (重复拉取/导入场景).
    #[test]
    fn transit_id_is_idempotent() {
        assert_eq!(transit_model_id("go", "go/deepseek-flash"), "go/deepseek-flash");
        assert_eq!(
            transit_model_id("commandcodeAI", "commandcodeAI/deepseek/deepseek-v4-flash"),
            "commandcodeAI/deepseek/deepseek-v4-flash"
        );
        // 只认「自己的」前缀: 别家前缀不算已加
        assert_eq!(
            transit_model_id("go", "zen/deepseek-flash"),
            "go/zen/deepseek-flash"
        );
        // 供应商名为空 -> 原样返回, 不造出 "/xxx" 这种畸形 ID
        assert_eq!(transit_model_id("", "m"), "m");
    }

    /// 拉取后同名上游 ID 挂到两家供应商, 注册表里两条路由都能查到 (改前缀的核心目的).
    #[test]
    fn same_upstream_id_across_providers_no_longer_collides() {
        let json = r#"
        {
          "providers": [
            { "name": "Openrouter", "endpoint": "https://a/v1/chat/completions",
              "api_key_env": "K1", "models": {} },
            { "name": "commandcodeAI", "endpoint": "https://b/v1/chat/completions",
              "api_key_env": "K2", "models": {} }
          ]
        }
        "#;
        let mut reg = ProviderRegistry::from_str(json).unwrap();
        let up = vec!["deepseek/deepseek-v4-flash".to_string()];
        reg.add_models("Openrouter", &up);
        reg.add_models("commandcodeAI", &up);

        // add_models 只改 providers 列表, 路由表由 from_str 重建 —— 走真实保存/重载路径.
        let reloaded = ProviderRegistry::from_str(&reg.to_json().unwrap()).unwrap();

        // 两条独立路由, 各自指向自己的供应商
        let r1 = reloaded
            .lookup("Openrouter/deepseek/deepseek-v4-flash")
            .expect("Openrouter 路由存在");
        assert_eq!(r1.provider.name, "Openrouter");
        assert_eq!(r1.model.upstream_model.as_deref(), Some("deepseek/deepseek-v4-flash"));
        let r2 = reloaded
            .lookup("commandcodeAI/deepseek/deepseek-v4-flash")
            .expect("commandcodeAI 路由存在");
        assert_eq!(r2.provider.name, "commandcodeAI");
        assert_eq!(r2.model.upstream_model.as_deref(), Some("deepseek/deepseek-v4-flash"));

        // 未加前缀的裸 ID 不再是可路由的中转 ID
        assert!(reloaded.lookup("deepseek/deepseek-v4-flash").is_none());
    }

    /// 反例对照: 若不加前缀 (旧行为), 两家供应商的同名 ID 会塌成一条路由,
    /// 后者覆盖前者 —— 这正是本次要修的问题.
    #[test]
    fn unprefixed_ids_would_collide() {
        let json = r#"
        {
          "providers": [
            { "name": "Openrouter", "endpoint": "https://a/v1/chat/completions",
              "api_key_env": "K1",
              "models": { "deepseek/deepseek-v4-flash": { "upstream_model": "deepseek/deepseek-v4-flash" } } },
            { "name": "commandcodeAI", "endpoint": "https://b/v1/chat/completions",
              "api_key_env": "K2",
              "models": { "deepseek/deepseek-v4-flash": { "upstream_model": "deepseek/deepseek-v4-flash" } } }
          ]
        }
        "#;
        let reg = ProviderRegistry::from_str(json).unwrap();
        // 同一个 ID 只剩一条路由, 且被后加载的 commandcodeAI 占据 (Openrouter 那条永不生效)
        let r = reg.lookup("deepseek/deepseek-v4-flash").expect("存在一条路由");
        assert_eq!(r.provider.name, "commandcodeAI");
    }

    #[test]
    fn add_models_unknown_provider_is_noop() {
        let mut reg = ProviderRegistry::from_str(SAMPLE_JSON).unwrap();
        let (added, skipped) = reg.add_models("no-such-provider", &["x".to_string()]);
        assert_eq!(added, 0);
        assert_eq!(skipped, 0);
    }

    #[test]
    fn add_models_skips_existing_upstream_model() {
        // 构建含 upstream_model 覆盖的注册表: go-flash -> deepseek-v4-flash
        let json = r#"
        {
          "providers": [
            {
              "name": "test-zen",
              "endpoint": "https://example.com/v1/chat/completions",
              "api_key_env": "TEST_ZEN_KEY",
              "models": {
                "go-flash": { "upstream_model": "deepseek-v4-flash" }
              }
            }
          ]
        }
        "#;
        let mut reg = ProviderRegistry::from_str(json).unwrap();
        // 上游返回的 deepseek-v4-flash 已被 go-flash 的 upstream_model 覆盖, 应跳过 (不新增 key)
        let (added, skipped) = reg.add_models("test-zen", &["deepseek-v4-flash".to_string()]);
        assert_eq!(added, 0);
        assert_eq!(skipped, 1);
        let provider = reg.providers.iter().find(|p| p.name == "test-zen").unwrap();
        assert!(!provider.models.contains_key("deepseek-v4-flash"));
    }

    #[test]
    fn extract_openai_standard() {
        let body: serde_json::Value =
            serde_json::json!({ "object": "list", "data": [{ "id": "a" }, { "id": "b" }] });
        assert_eq!(
            extract_model_ids(&body).unwrap(),
            vec!["a".to_string(), "b".to_string()]
        );
    }

    #[test]
    fn extract_models_array() {
        let body: serde_json::Value = serde_json::json!({ "models": [{ "id": "x" }] });
        assert_eq!(extract_model_ids(&body).unwrap(), vec!["x".to_string()]);
    }

    #[test]
    fn extract_bare_string_array() {
        let body: serde_json::Value = serde_json::json!(["m1", "m2"]);
        assert_eq!(
            extract_model_ids(&body).unwrap(),
            vec!["m1".to_string(), "m2".to_string()]
        );
    }

    #[test]
    fn extract_empty_is_error() {
        let body: serde_json::Value = serde_json::json!({ "data": [] });
        assert!(extract_model_ids(&body).is_err());
    }

    #[test]
    fn default_api_format_tags_gateway_specific_models() {
        // go 网关: minimax / qwen3*-plus·max → anthropic; glm/kimi/deepseek → None(openai)
        assert_eq!(default_api_format("go", "minimax-m2.5"), Some("anthropic"));
        assert_eq!(default_api_format("go", "qwen3.7-max"), Some("anthropic"));
        assert_eq!(default_api_format("go", "qwen3.5-plus"), Some("anthropic"));
        assert_eq!(default_api_format("go", "glm-5.2"), None);
        assert_eq!(default_api_format("go", "kimi-k2.7-code"), None);
        assert_eq!(default_api_format("go", "deepseek-v4-pro"), None);
        // zen 网关: claude / qwen3*-plus·max → anthropic; minimax-m2.x / deepseek / glm → None
        assert_eq!(default_api_format("zen", "claude-sonnet-4-6"), Some("anthropic"));
        assert_eq!(default_api_format("zen", "qwen3.6-max"), Some("anthropic"));
        assert_eq!(default_api_format("zen", "minimax-m2.5"), None);
        assert_eq!(default_api_format("zen", "deepseek-v4-flash"), None);
        // 未知供应商一律 None
        assert_eq!(default_api_format("deepseek", "deepseek-chat"), None);
        // 大小写不敏感
        assert_eq!(default_api_format("GO", "MiniMax-M2.5"), Some("anthropic"));
    }

    #[test]
    fn model_is_anthropic_prefers_model_over_provider() {
        let openai_provider = ProviderConfig {
            name: "go".into(),
            endpoint: "https://x/v1/chat/completions".into(),
            api_key_env: "K".into(),
            api_key_default: None,
            probed_protocols: None,
            balance_endpoint: None,
            headers: None,
            api_format: Some("openai".into()),
            prompt_cache: None,
            endpoint_anthropic: None,
            endpoint_responses: None,
            openai_cache_control: None,
            max_request_body_bytes: None,
            models: HashMap::new(),
        };
        // 供应商 openai + 模型未标注 → openai
        let m_default = ModelConfig {
            upstream_model: Some("glm-5.2".into()),
            reasoning_effort: None,
            free: None,
            extra_body: None,
            api_format: None,
            api_formats: None,
            price: None,
            strip_toolcall_reasoning: None,
            origin: None,
            loop_guard: None,
        };
        assert!(!m_default.is_anthropic(&openai_provider));
        // 供应商 openai + 模型标注 anthropic → anthropic
        let m_override = ModelConfig {
            upstream_model: Some("minimax-m2.5".into()),
            reasoning_effort: None,
            free: None,
            extra_body: None,
            api_format: Some("anthropic".into()),
            api_formats: None,
            price: None,
            strip_toolcall_reasoning: None,
            origin: None,
            loop_guard: None,
        };
        assert!(m_override.is_anthropic(&openai_provider));
        // 供应商 anthropic + 模型回落 → anthropic
        let anthropic_provider = ProviderConfig {
            api_format: Some("anthropic".into()),
            ..openai_provider.clone()
        };
        assert!(m_default.is_anthropic(&anthropic_provider));
    }

    fn mc(api_format: Option<&str>, api_formats: Option<Vec<&str>>) -> ModelConfig {
        ModelConfig {
            upstream_model: None,
            reasoning_effort: None,
            free: None,
            extra_body: None,
            api_format: api_format.map(|s| s.to_string()),
            api_formats: api_formats.map(|v| v.into_iter().map(|s| s.to_string()).collect()),
            price: None,
            strip_toolcall_reasoning: None,
            origin: None,
            loop_guard: None,
        }
    }

    /// 未声明候选集 → pick 返回 None, 调用方走既有 resolve (单协议语义完全不变).
    #[test]
    fn pick_api_format_none_when_no_candidates() {
        assert_eq!(mc(None, None).pick_api_format("responses"), None);
        // 只有单值 api_format 时也不接管 —— 向后兼容的关键断言
        assert_eq!(mc(Some("responses"), None).pick_api_format("openai"), None);
        // 空候选集 = 未声明
        assert_eq!(mc(None, Some(vec![])).pick_api_format("openai"), None);
        // 候选集全是非法值 = 未声明 (不猜)
        assert_eq!(mc(None, Some(vec!["weird", ""])).pick_api_format("openai"), None);
    }

    /// 客户端入口协议命中候选集 → 用客户端那个 (可走原生直通, 避免跨协议转换).
    #[test]
    fn pick_api_format_prefers_client_entry_protocol() {
        let m = mc(None, Some(vec!["openai", "responses"]));
        // 候选集顺序是 [openai, responses]: 但客户端打 responses 时应挑 responses,
        // 而不是"第一项"——这正是"由请求决定协议"
        assert_eq!(m.pick_api_format("responses"), Some("responses"));
        assert_eq!(m.pick_api_format("openai"), Some("openai"));
        // 反向声明的候选集也一样
        let m2 = mc(None, Some(vec!["responses", "openai"]));
        assert_eq!(m2.pick_api_format("openai"), Some("openai"));
        assert_eq!(m2.pick_api_format("responses"), Some("responses"));
        // 三个协议共存
        let m3 = mc(None, Some(vec!["openai", "anthropic", "responses"]));
        assert_eq!(m3.pick_api_format("anthropic"), Some("anthropic"));
        assert_eq!(m3.pick_api_format("responses"), Some("responses"));
        assert_eq!(m3.pick_api_format("openai"), Some("openai"));
    }

    /// 客户端协议不在候选集 → 回落候选集首项 (声明顺序即优先级).
    #[test]
    fn pick_api_format_falls_back_to_first_candidate() {
        let m = mc(None, Some(vec!["responses", "openai"]));
        // 客户端打 anthropic, 候选集没有 → 用第一项 responses (由本管线转换)
        assert_eq!(m.pick_api_format("anthropic"), Some("responses"));
        let m2 = mc(None, Some(vec!["openai"]));
        assert_eq!(m2.pick_api_format("responses"), Some("openai"));
    }

    /// 入口协议名归一: `/v1/chat/completions` 习惯叫法要能匹配配置里的 "openai".
    #[test]
    fn pick_api_format_normalizes_entry_names() {
        let m = mc(None, Some(vec!["openai", "anthropic", "responses"]));
        assert_eq!(m.pick_api_format("chat"), Some("openai"));
        assert_eq!(m.pick_api_format("chat_completions"), Some("openai"));
        assert_eq!(m.pick_api_format("openai_chat"), Some("openai"));
        assert_eq!(m.pick_api_format("messages"), Some("anthropic"));
        assert_eq!(m.pick_api_format("claude"), Some("anthropic"));
        assert_eq!(m.pick_api_format("openai_responses"), Some("responses"));
        // 大小写/空白容错
        assert_eq!(m.pick_api_format("  RESPONSES  "), Some("responses"));
    }

    /// normalize_format 本身的映射表.
    #[test]
    fn normalize_format_maps_entry_names() {
        assert_eq!(normalize_format("chat"), "openai");
        assert_eq!(normalize_format("messages"), "anthropic");
        assert_eq!(normalize_format("response"), "responses");
        assert_eq!(normalize_format("openai"), "openai");
        // 未知值原样返回 (不猜)
        assert_eq!(normalize_format("gemini"), "gemini");
    }

    fn prov_ep(name: &str, ep: &str) -> ProviderConfig {
        ProviderConfig {
            name: name.into(),
            endpoint: ep.into(),
            api_key_env: "K".into(),
            api_key_default: None,
            probed_protocols: None,
            balance_endpoint: None,
            headers: None,
            api_format: None,
            prompt_cache: None,
            endpoint_anthropic: None,
            endpoint_responses: None,
            openai_cache_control: None,
            max_request_body_bytes: None,
            models: HashMap::new(),
        }
    }

    /// 第一层: URL 路径含**有区分度**的协议段 → 零成本直接读出
    /// (对应参考项目 preset 里 `https://api.minimaxi.io/anthropic/v1` 这类写法)。
    ///
    /// 关键: `/v1/chat/completions` 是所有 OpenAI 兼容端点的默认路径, **不是协议信号**
    /// —— 若当信号会"总是命中", 目录与探测永远走不到。此断言固化这一点。
    #[test]
    fn free_guess_from_url_path() {
        assert_eq!(
            free_protocol_guess(&prov_ep("x", "https://api.minimax.io/anthropic/v1/chat/completions")),
            Some(vec!["anthropic".to_string()]),
            "/anthropic/ 段应优先"
        );
        assert_eq!(
            free_protocol_guess(&prov_ep("x", "https://gw.example.com/v1/responses")),
            Some(vec!["responses".to_string()])
        );
        assert_eq!(
            free_protocol_guess(&prov_ep("x", "https://gw.example.com/v1/messages")),
            Some(vec!["anthropic".to_string()])
        );
        // 裸 chat/completions **不是**信号 → 落到后续层 (此处 host 未知故为 None)
        assert_eq!(
            free_protocol_guess(&prov_ep("x", "https://gw.example.com/v1/chat/completions")),
            None,
            "/v1/chat/completions 是通用路径, 不得当协议信号"
        );
        // 不应因路径里出现子串而误判 (按整段匹配)
        assert_eq!(
            free_protocol_guess(&prov_ep("x", "https://gw.example.com/not-anthropic-x/v1")),
            None,
            "'not-anthropic-x' 不是 anthropic 段"
        );
    }

    /// 第二层: 裸 /v1 端点走内置 host 目录 (覆盖多数常见服务方, 零请求)。
    #[test]
    fn free_guess_from_builtin_catalog() {
        // 目录命中: 裸 /v1 无路径信号, 靠 host 查表
        assert_eq!(
            free_protocol_guess(&prov_ep("deepseek", "https://api.deepseek.com/v1/chat/completions")),
            Some(vec!["openai".to_string()])
        );
        // 子域剥离: 目录里是 openrouter.ai, 用户填 www.openrouter.ai 也应命中
        assert_eq!(
            free_protocol_guess(&prov_ep("or", "https://www.openrouter.ai/api/v1")),
            Some(vec!["openai".to_string()]),
            "应逐级剥子域命中"
        );
    }

    /// 第三层: 两层都拿不到 → 返回 None, 调用方走探测 (HEAD, 零 token)。
    /// 这正是未收录的小众中转的情形 (实测用户的 ginka / commandcodeAI)。
    #[test]
    fn free_guess_returns_none_when_unknown() {
        assert_eq!(
            free_protocol_guess(&prov_ep("x", "https://api.ginka.cloud/v1/chat/completions")),
            None
        );
        assert_eq!(
            free_protocol_guess(&prov_ep("x", "https://cpa.arromega.org/v1/chat/completions")),
            None
        );
        // 自建内网网关同样落到探测
        assert_eq!(
            free_protocol_guess(&prov_ep("x", "http://192.168.1.10:8000/v1/chat/completions")),
            None
        );
    }

    /// 目录查询本身: 后缀匹配 + 逐级剥子域。
    #[test]
    fn catalog_lookup_by_host_suffix() {
        use crate::provider_catalog::protocol_for_host;
        assert_eq!(protocol_for_host("api.deepseek.com"), Some("openai"));
        assert_eq!(protocol_for_host("deepseek.com"), Some("openai"));
        assert_eq!(protocol_for_host("API.DeepSeek.com"), Some("openai"), "大小写不敏感");
        assert_eq!(protocol_for_host("api.deepseek.com."), Some("openai"), "容忍尾点");
        assert_eq!(protocol_for_host("unknown-vendor.example"), None);
        assert_eq!(protocol_for_host(""), None);
        // 不应把无关域名的后缀当命中: 目录里若有 "com" 之外的通用段会误判, 这里确认无此问题
        assert_eq!(protocol_for_host("com"), None);
    }
}
