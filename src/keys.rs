//! API Key 管理 — 存储密钥到 data/keys.json, 支持管理面板在线修改.
//!
//! 查询优先级: 环境变量 → keys.json → api_key_default (providers.json).

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use tokio::sync::RwLock;

/// 密钥存储.
#[derive(Clone)]
pub struct KeyStore {
    path: PathBuf,
    keys: Arc<RwLock<HashMap<String, String>>>,
}

impl KeyStore {
    /// 从 data/ 目录加载密钥文件.
    pub fn new(data_dir: &str) -> Self {
        let dir = PathBuf::from(data_dir);
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("keys.json");
        let keys = match std::fs::read_to_string(&path) {
            Ok(content) => serde_json::from_str(&content).unwrap_or_default(),
            Err(_) => HashMap::new(),
        };
        Self {
            path,
            keys: Arc::new(RwLock::new(keys)),
        }
    }

    /// 查询密钥: 环境变量 → keys.json.
    pub async fn get(&self, env_var: &str) -> Option<String> {
        // 1. 环境变量优先
        if let Ok(val) = std::env::var(env_var) {
            if !val.is_empty() {
                return Some(val);
            }
        }
        // 2. keys.json
        let keys = self.keys.read().await;
        keys.get(env_var).cloned().filter(|s| !s.is_empty())
    }

    /// 设置密钥 (更新内存 + 持久化).
    pub async fn set(&self, env_var: &str, value: &str) -> Result<(), String> {
        {
            let mut keys = self.keys.write().await;
            if value.is_empty() {
                keys.remove(env_var);
            } else {
                keys.insert(env_var.to_string(), value.to_string());
            }
        }
        self.persist().await
    }

    /// 获取所有密钥的脱敏视图: (env_var, 是否已配置, 后 4 位).
    pub async fn masked_view(&self, env_vars: &[String]) -> Vec<KeyEntry> {
        let keys = self.keys.read().await;
        env_vars
            .iter()
            .map(|var| {
                // 先检查环境变量, 再检查 keys.json
                let value = std::env::var(var)
                    .ok()
                    .filter(|s| !s.is_empty())
                    .or_else(|| keys.get(var).cloned().filter(|s| !s.is_empty()));
                let suffix = value
                    .as_ref()
                    .and_then(|v| {
                        if v.len() > 4 {
                            Some(v[v.len() - 4..].to_string())
                        } else {
                            Some(v.clone())
                        }
                    });
                KeyEntry {
                    env_var: var.clone(),
                    configured: value.is_some(),
                    suffix,
                }
            })
            .collect()
    }

    /// 持久化到文件.
    async fn persist(&self) -> Result<(), String> {
        let keys = self.keys.read().await;
        let content = serde_json::to_string_pretty(&*keys).map_err(|e| e.to_string())?;
        tokio::fs::write(&self.path, content)
            .await
            .map_err(|e| e.to_string())?;
        Ok(())
    }
}

/// 密钥的脱敏展示.
#[derive(Debug, Clone, serde::Serialize)]
pub struct KeyEntry {
    pub env_var: String,
    pub configured: bool,
    pub suffix: Option<String>,
}
