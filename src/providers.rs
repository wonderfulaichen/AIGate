//! 供应商配置 — 从 providers.json 加载, 构建 model→provider 路由表.
//!
//! 配置文件格式见 providers.json 中的中文说明.

use std::collections::HashMap;
use std::time::Duration;

use reqwest::Client;
use serde::{Deserialize, Serialize};
use tracing::{info, warn};

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
    /// 额外注入到请求 body 的字段 (任意 JSON 键值对).
    #[serde(default)]
    pub extra_body: Option<serde_json::Value>,
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
    /// 额外注入的 HTTP 请求头.
    #[serde(default)]
    pub headers: Option<HashMap<String, String>>,
    /// 该供应商支持的模型, key 是 model id.
    pub models: HashMap<String, ModelConfig>,
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

    /// 获取供应商的 API key: 优先 key_store, 再回退默认值.
    pub async fn api_key(&self, provider: &ProviderConfig, key_store: &crate::keys::KeyStore) -> Result<String, String> {
        if let Some(k) = key_store.get(&provider.api_key_env).await {
            if !k.is_empty() {
                return Ok(k);
            }
        }
        if let Some(default) = &provider.api_key_default {
            Ok(default.clone())
        } else {
            Err(format!(
                "API key not set: env var {} is empty (provider: {})",
                provider.api_key_env, provider.name
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
                        extra_body: None,
                    },
                );
                added += 1;
            }
        }
        (added, skipped)
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
    let models_url = provider.endpoint.replace("/chat/completions", "/models");
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
                    extra_body: None,
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
}
