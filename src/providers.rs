//! 供应商配置 — 从 providers.json 加载, 构建 model→provider 路由表.
//!
//! 配置文件格式见 providers.json 中的中文说明.

use std::collections::HashMap;

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

/// 运行时路由条目 — model id → (供应商配置, 模型配置).
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

    /// 按模型 id 查找路由条目 (返回克隆, 支持 RwLock 场景).
    pub fn lookup(&self, model_id: &str) -> Option<RouteEntry> {
        self.routes.get(model_id).cloned()
    }

    /// 获取所有已注册的 model id (用于 /v1/models).
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
}
