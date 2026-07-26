# Fix: IPv6 连接失败导致 500 错误

## 问题

中转启动正常，但发送请求时返回 500/502：
```
upstream error: error sending request for url (https://example.com/v1/chat/completions)
```

## 根因

上游 API 的 DNS 同时返回 IPv4 和 IPv6 地址。你的网络 IPv6 不通：

- `curl.exe` 能成功（自动 fallback 到 IPv4）
- Rust reqwest **优先尝试 IPv6**，失败后不会 fallback 到 IPv4，直接报错

## 修复

替换 `src/main.rs`，新增 `Ipv4Resolver` 自定义 DNS 解析器，过滤掉 IPv6 地址。

### 新增代码

在 `main.rs` 顶部添加 IPv4-only DNS resolver：

```rust
use std::net::{SocketAddr, SocketAddrV4};
use std::sync::Arc;
use reqwest::dns::{Resolve, Resolving, Name};

struct Ipv4Resolver {
    inner: reqwest::dns::GaiResolver,
}

impl Ipv4Resolver {
    fn new() -> Self {
        Self { inner: reqwest::dns::GaiResolver::new() }
    }
}

impl Resolve for Ipv4Resolver {
    fn resolve(&self, name: Name) -> Resolving {
        let fut = self.inner.resolve(name);
        Box::pin(async move {
            let addrs = fut.await?;
            let ipv4: Vec<SocketAddr> = addrs
                .filter(|a| matches!(a, SocketAddr::V4(_)))
                .collect();
            Ok(Box::new(ipv4.into_iter()) as Box<dyn Iterator<Item = SocketAddr> + Send>)
        })
    }
}
```

然后修改 Client 构建：

```rust
// 修改前
let client = reqwest::Client::builder()
    .timeout(std::time::Duration::from_secs(660))
    .build()
    .expect("failed to build HTTP client");

// 修改后
let client = reqwest::Client::builder()
    .dns_resolver(Arc::new(Ipv4Resolver::new()))
    .timeout(std::time::Duration::from_secs(660))
    .build()
    .expect("failed to build HTTP client");
```

完整修复版已写入 `main_fix.txt`，可直接覆盖 `src/main.rs`：

```cmd
cd <project_dir>
copy src\main.rs src\main.rs.bak
copy main_fix.txt src\main.rs
cargo build --release
```

然后重启 `start.bat` 即可。

## 验证

```powershell
$body = '{"model":"mimo-v2.5-free","messages":[{"role":"user","content":"hi"}],"max_tokens":10}'
Invoke-RestMethod -Uri "http://127.0.0.1:8787/v1/chat/completions" -Method POST -Body $body -ContentType "application/json" -TimeoutSec 30
```

返回正常 JSON 响应即修复成功。
