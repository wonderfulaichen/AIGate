//! API Key 管理 — 密钥按【供应商归属】存储于 data/keys.json.
//!
//! ## 设计原则: Key 是 Provider 的子资源 (包含关系)
//! 索引键为 `provider.name`, 而非自由字符串 `api_key_env`. 这样:
//! - 删除 / 重命名供应商时, 其密钥自然级联 (无孤儿 key, 无改名残留).
//! - 凭据编辑内嵌进供应商卡片, 而非一个按 env_var 罗列的独立面板.
//!
//! ## 查询优先级 (由 providers.rs 的 `api_key` 组合)
//! `keys.json[provider.name]` (面板编辑值)
//!   → 环境变量 (`provider.api_key_env`, 可选默认值源)
//!   → `provider.api_key_default` (providers.json 内置默认值).
//!
//! ## 迁移
//! 启动时兼容旧版 (按 env_var 索引) 的 keys.json: 按 providers 的
//! `api_key_env → names` 映射, 将旧 key 复制到对应 `provider.name` 下
//! (共享同一 env_var 的多个供应商各自复制, 无损迁移), 并就地重写文件.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use tokio::sync::RwLock;

/// 返回安全的密钥后缀：短值不暴露任何原文，长值按 Unicode 字符取最后四位。
fn masked_suffix(value: &str) -> Option<String> {
    let chars: Vec<char> = value.chars().collect();
    (chars.len() > 4).then(|| chars[chars.len() - 4..].iter().collect())
}

/// 密钥存储. 索引键 = 供应商名.
#[derive(Clone)]
pub struct KeyStore {
    path: PathBuf,
    keys: Arc<RwLock<HashMap<String, String>>>,
}

impl KeyStore {
    /// 从 data/ 目录加载密钥文件.
    ///
    /// 若存在旧版 (按 env_var 索引) 的 keys.json, 则按 `providers` 的
    /// `api_key_env → names` 映射迁移为按 `provider.name` 索引 (无损), 并就地重写.
    pub fn new(data_dir: &str, providers: &[crate::providers::ProviderConfig]) -> Self {
        let dir = PathBuf::from(data_dir);
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("keys.json");
        let mut keys: HashMap<String, String> = match std::fs::read_to_string(&path) {
            Ok(content) => serde_json::from_str(&content).unwrap_or_default(),
            Err(_) => HashMap::new(),
        };

        // 迁移: 旧版索引为 env_var (自由字符串). 若某个 key 不在已知 provider 名中,
        // 按其 api_key_env 映射复制到各 provider.name 下, 再移除旧 env_var 键.
        let known_names: std::collections::HashSet<&String> =
            providers.iter().map(|p| &p.name).collect();
        let mut env_to_names: HashMap<&String, Vec<&String>> = HashMap::new();
        for p in providers {
            env_to_names
                .entry(&p.api_key_env)
                .or_default()
                .push(&p.name);
        }
        let orphan_env_keys: Vec<(String, String)> = keys
            .iter()
            .filter(|(k, _)| !known_names.contains(k))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        let mut migrated = false;
        for (env_var, val) in orphan_env_keys {
            if let Some(names) = env_to_names.get(&env_var) {
                for name in names {
                    keys.entry((*name).clone()).or_insert_with(|| val.clone());
                }
                keys.remove(&env_var);
                migrated = true;
            }
        }
        if migrated {
            let _ = Self::write_atomic(&path, &keys);
        }

        Self {
            path,
            keys: Arc::new(RwLock::new(keys)),
        }
    }

    /// 查询某供应商的面板设置密钥 (仅 keys.json[provider.name], 不含环境变量/默认值).
    ///
    /// 环境变量与默认值回退由 `providers::ProviderRegistry::api_key` 负责组合.
    pub async fn get_for_provider(&self, name: &str) -> Option<String> {
        let keys = self.keys.read().await;
        keys.get(name).cloned().filter(|s| !s.is_empty())
    }

    /// 设置某供应商密钥 (更新内存 + 持久化).
    ///
    /// 空值 = 删除该供应商密钥 (回退到环境变量 / 默认值).
    pub async fn set_for_provider(&self, name: &str, value: &str) -> Result<(), String> {
        {
            let mut keys = self.keys.write().await;
            if value.is_empty() {
                keys.remove(name);
            } else {
                keys.insert(name.to_string(), value.to_string());
            }
        }
        self.persist().await
    }

    /// 删除某供应商的全部密钥 (删除 / 重命名 provider 时级联调用).
    pub async fn remove_for_provider(&self, name: &str) -> Result<(), String> {
        {
            let mut keys = self.keys.write().await;
            keys.remove(name);
        }
        self.persist().await
    }

    /// 批量删除多个供应商的密钥 (providers 保存时清理孤儿 key).
    pub async fn remove_many(&self, names: &[String]) -> Result<(), String> {
        {
            let mut keys = self.keys.write().await;
            for n in names {
                keys.remove(n);
            }
        }
        self.persist().await
    }

    /// 获取所有供应商的脱敏视图 (包含关系: 每个 provider 一行).
    ///
    /// `configured` 与 `suffix` 包含面板值与环境变量 (与查询优先级一致);
    /// `api_key_default` 仅作兜底, 不计入 "已配置" 以免所有供应商都显示已配置.
    pub async fn masked_view_for_providers(
        &self,
        providers: &[crate::providers::ProviderConfig],
    ) -> Vec<ProviderKeyView> {
        let keys = self.keys.read().await;
        providers
            .iter()
            .map(|p| {
                let value = keys
                    .get(&p.name)
                    .cloned()
                    .filter(|s| !s.is_empty())
                    .or_else(|| {
                        std::env::var(&p.api_key_env)
                            .ok()
                            .filter(|s| !s.is_empty())
                    });
                let suffix = value.as_deref().and_then(masked_suffix);
                ProviderKeyView {
                    provider: p.name.clone(),
                    env_var: p.api_key_env.clone(),
                    configured: value.is_some(),
                    suffix,
                }
            })
            .collect()
    }

    /// 持久化到文件 (原子替换, 避免写一半损坏).
    async fn persist(&self) -> Result<(), String> {
        let keys = self.keys.read().await;
        Self::write_atomic(&self.path, &*keys)
    }

    /// 原子写: 先写临时文件再 rename, 防止进程中断留下半截文件.
    fn write_atomic(path: &PathBuf, keys: &HashMap<String, String>) -> Result<(), String> {
        let content = serde_json::to_string_pretty(keys).map_err(|e| e.to_string())?;
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, &content).map_err(|e| e.to_string())?;
        std::fs::rename(&tmp, path).map_err(|e| e.to_string())?;
        Ok(())
    }
}

/// 供应商密钥的脱敏展示 (包含关系: 每行对应一个 provider).
#[derive(Debug, Clone, serde::Serialize)]
pub struct ProviderKeyView {
    /// 供应商名 (归属主键).
    pub provider: String,
    /// 该供应商可选的 "环境变量默认值源" (仅展示, 不再是 key 索引).
    pub env_var: String,
    /// 是否已配置 (面板值或环境变量任一非空).
    pub configured: bool,
    /// 密钥后 4 位 (脱敏). 短密钥不返回原文，因此可能为空.
    pub suffix: Option<String>,
}

#[cfg(test)]
mod masking_tests {
    use super::masked_suffix;

    #[test]
    fn masks_short_values_without_leaking_them() {
        assert_eq!(masked_suffix("a"), None);
        assert_eq!(masked_suffix("abcd"), None);
    }

    #[test]
    fn takes_last_four_unicode_chars_safely() {
        assert_eq!(masked_suffix("abcdefgh"), Some("efgh".to_string()));
        assert_eq!(masked_suffix("密钥-12345"), Some("2345".to_string()));
    }
}
