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

    /// 查询密钥: keys.json (用户面板编辑值) 优先 → 环境变量 (默认值).
    ///
    /// 优先级设计: 用户在管理面板编辑的值写入 keys.json, 优先于系统环境变量生效;
    /// 清空 key 时 `set()` 会 remove 掉 keys.json 条目, 自动回退到环境变量默认值.
    pub async fn get(&self, env_var: &str) -> Option<String> {
        // 1. keys.json (用户面板编辑优先)
        {
            let keys = self.keys.read().await;
            if let Some(val) = keys.get(env_var) {
                if !val.is_empty() {
                    return Some(val.clone());
                }
            }
        }
        // 2. 环境变量 (默认值)
        if let Ok(val) = std::env::var(env_var) {
            if !val.is_empty() {
                return Some(val);
            }
        }
        None
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
                // keys.json (用户编辑值) 优先 → 环境变量 (默认值), 与 get() 保持一致
                let value = keys.get(var).cloned().filter(|s| !s.is_empty())
                    .or_else(|| std::env::var(var).ok().filter(|s| !s.is_empty()));
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
