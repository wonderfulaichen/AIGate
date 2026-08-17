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
    /// 模型级 API 格式覆盖: 可选 "openai" / "anthropic".
    ///
    /// 不设置时回落到供应商级 [`ProviderConfig::api_format`].
    /// 用途: 同供应商(如 opencode go / zen 网关)下, 不同模型可能走不同端点
    /// (glm/kimi → /chat/completions, minimax / qwen3-plus·max → /messages),
    /// 仅靠供应商级 `api_format` 无法区分, 需在此逐个标注.
    /// 点「获取」拉取上游模型时, 见 [`default_api_format`] 自动打标.
    #[serde(default)]
    pub api_format: Option<String>,
    /// 模型价格（元 / 百万 tokens）— 费用统计用.
    ///
    /// 优先级高于内置默认价格表（见 `crate::pricing`）. 缺省时回退内置表;
    /// 内置表也未覆盖的模型, 费用记为 0（"未配置价格"）.
    ///
    /// 字段: `{ "input_per_m": 1.0, "output_per_m": 2.0, "cache_read_per_m": 0.02 }`,
    /// `cache_read_per_m` 可选（KV Cache 命中价, 缺失则按输入价计）.
    #[serde(default)]
    pub price: Option<ModelPrice>,
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
    /// 余额查询 API 端点 (可选).
    #[serde(default)]
    pub balance_endpoint: Option<String>,
    /// 额外注入的 HTTP 请求头.
    #[serde(default)]
    pub headers: Option<HashMap<String, String>>,
    /// 上游 API 格式: 缺省 "openai" (chat/completions);
    /// 设为 "anthropic" 时, 代理把 OpenAI 请求体转换为 Anthropic /messages 格式,
    /// 并把 Anthropic 流式/非流式响应逆转换为 OpenAI 格式返回客户端.
    /// (OpenCode Go 网关的 MiniMax/Qwen 系列走 /messages, 见官方 go.mdx.)
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
}

impl ProviderConfig {
    /// 是否使用 Anthropic /messages 协议.
    pub fn is_anthropic(&self) -> bool {
        self.api_format.as_deref() == Some("anthropic")
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
    /// - 新增未存在的模型 ID, `upstream_model` 设为同名 (原样转发), `reasoning_effort` 留空
    ///   (由客户端按模型自行调节思考档位), `extra_body` 留空.
    /// - **去重的唯一标准 = 上游模型 ID**: 只要本地已有某个条目 (任意中转别名) 的 `upstream_model`
    ///   等于待拉取的 ID, 就跳过. **不对比中转别名(key)、也不对比思考档位(reasoning_effort)**.
    ///   例: 文件里 `go-flash -> deepseek-v4-flash`, 上游返回的 `deepseek-v4-flash` 不再重复加入.
    ///
    /// 返回 `(新增数量, 跳过数量)`. 仅修改内存态, 调用方需自行 `to_json()` 写回文件并 `reload()`.
    pub fn add_models(&mut self, name: &str, ids: &[String]) -> (usize, usize) {
        let mut added = 0usize;
        let mut skipped = 0usize;
        if let Some(provider) = self.providers.iter_mut().find(|p| p.name == name) {
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
                provider.models.insert(
                    id.clone(),
                    ModelConfig {
                        upstream_model: Some(id.clone()),
                        reasoning_effort: None,
                        free: None,
                        extra_body: None,
                        api_format: default_api_format(&provider.name, id).map(|s| s.to_string()),
                        price: None,
                    },
                );
                added += 1;
            }
        }
        (added, skipped)
    }
}

/// 按官方网关清单, 为「供应商 + 模型 ID」推断默认 API 格式 (OpenAI / Anthropic).
///
/// 数据来源: opencode 官方文档 `go.mdx` / `zen.mdx` (已对照源码核实):
///
/// - **go 网关** (`/zen/go/v1`):
///   - `glm` / `kimi` / `deepseek` / `mimo` → `/chat/completions` (OpenAI)
///   - `minimax-*`、`qwen3.*-plus/max` (含 m2.5) → `/messages` (Anthropic)
/// - **zen 网关** (`/zen/v1`):
///   - `minimax-m2.5/m2.7`、`deepseek`、`glm`、`kimi` → `/chat/completions` (OpenAI)
///   - `claude-*`、`qwen3.5/3.6/3.7-plus/max` → `/messages` (Anthropic)
///   - `gpt-*` → `/responses` (AIGate 暂不支持, 不标注, 留待部署方手动处理)
///
/// 返回 `Some("anthropic")` 表示需走 Anthropic /messages; 返回 `None` 表示
/// 回落 OpenAI (即不写 `api_format` 字段). 仅匹配已知前缀, 未知供应商一律 `None`.
pub fn default_api_format(provider_name: &str, model_id: &str) -> Option<&'static str> {
    let id = model_id.to_lowercase();
    let provider = provider_name.to_lowercase();

    // 通用规则: 任意供应商下, 模型名含 "claude" 一律走 Anthropic /messages.
    // (用户诉求: claude 模型自动切到适配它的协议, 不依赖供应商手动标注.)
    if id.contains("claude") {
        return Some("anthropic");
    }

    match provider.as_str() {
        "go" => {
            // minimax-* 与 qwen3.*-plus/max 走 /messages
            if id.starts_with("minimax")
                || (id.starts_with("qwen3") && (id.contains("-plus") || id.contains("-max")))
            {
                Some("anthropic")
            } else {
                None
            }
        }
        "zen" => {
            // claude-* 与 qwen3*-plus/max 走 /messages; minimax-m2.x / deepseek / glm / kimi 回落 openai
            if id.starts_with("minimax")
                || (id.starts_with("qwen3") && (id.contains("-plus") || id.contains("-max")))
            {
                Some("anthropic")
            } else {
                None
            }
        }
        _ => None,
    }
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
    let models_url = provider
        .endpoint
        .replace("/chat/completions", "/models")
        .replace("/messages", "/models");
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
            price: None,
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
                    price: None,
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
        // 新增的模型: upstream_model 同名, reasoning_effort 留空
        let provider = reg.providers.iter().find(|p| p.name == "test-zen").unwrap();
        let m = provider.models.get("new-model-a").unwrap();
        assert_eq!(m.upstream_model.as_deref(), Some("new-model-a"));
        assert!(m.reasoning_effort.is_none());
        // 上游已覆盖的 ID 未作为新 key 被加入
        assert!(!provider.models.contains_key("deepseek-v4-flash"));
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
            balance_endpoint: None,
            headers: None,
            api_format: Some("openai".into()),
            prompt_cache: None,
            models: HashMap::new(),
        };
        // 供应商 openai + 模型未标注 → openai
        let m_default = ModelConfig {
            upstream_model: Some("glm-5.2".into()),
            reasoning_effort: None,
            free: None,
            extra_body: None,
            api_format: None,
            price: None,
        };
        assert!(!m_default.is_anthropic(&openai_provider));
        // 供应商 openai + 模型标注 anthropic → anthropic
        let m_override = ModelConfig {
            upstream_model: Some("minimax-m2.5".into()),
            reasoning_effort: None,
            free: None,
            extra_body: None,
            api_format: Some("anthropic".into()),
            price: None,
        };
        assert!(m_override.is_anthropic(&openai_provider));
        // 供应商 anthropic + 模型回落 → anthropic
        let anthropic_provider = ProviderConfig {
            api_format: Some("anthropic".into()),
            ..openai_provider.clone()
        };
        assert!(m_default.is_anthropic(&anthropic_provider));
    }
}
