# AIGate

本地 OpenAI 兼容反向代理，支持多供应商路由、模型名映射、实时监控面板，并提供桌面应用体验（原生窗口、系统托盘、后台运行）。

## 功能

- **多供应商路由** — 在 `providers.json` 中配置多个上游 API，由统一入口转发
- **模型名映射** — 客户端用带后缀的模型 ID（如 `deepseek-v4-flash-DS`），自动替换为上游真实模型名
- **思考强度注入** — 对支持推理的模型自动注入 `reasoning_effort` 参数
- **实时面板** — 桌面内嵌 WebView 管理面板，查看请求统计、Token 用量、趋势图表
- **系统托盘** — 最小化到托盘后台运行，不占任务栏
- **日志持久化** — 请求记录以 JSON Lines 格式留存，支持导出

## 快速开始

### 1. 下载

从 [Releases](https://github.com/wonderfulaichen/AIGate/releases) 下载最新版 `AIGate.exe`，放到一个空目录。

### 2. 配置

首次运行会自动生成 `providers.json` 和 `.env` 文件。

编辑 `.env` 填入你的 API Key：

```
OPENCODE_ZEN_KEY=public
OPENCODE_GO_KEY=sk-your-go-key-here
DEEPSEEK_API_KEY=sk-your-deepseek-key
```

编辑 `providers.json` 配置上游端点，默认包含示例配置，请替换 `endpoint` 为你的实际 API 地址。

### 3. 运行

双击启动 `AIGate.exe`。

桌面窗口会自动打开管理面板，地址为 `http://127.0.0.1:8787`。

最小化窗口可收起到系统托盘，双击托盘图标恢复。

### 4. 客户端配置

在支持 OpenAI 兼容 API 的客户端（如 Trae、CodeBuddy、NextChat 等）中：

```
API 地址: http://127.0.0.1:8787/v1/chat/completions
API Key:  任意值（实际由 .env 中的 key 决定）
模型 ID:  见 providers.json 中的 key（如 deepseek-v4-flash-DS）
```

## 配置文件

### providers.json

| 字段 | 说明 |
|------|------|
| `name` | 供应商名称（仅用于显示） |
| `endpoint` | 上游 chat/completions 端点 URL |
| `api_key_env` | 从哪个环境变量读取 API Key |
| `api_key_default` | 未配置时的默认值 |
| `models` | 该供应商支持的模型映射表 |

模型 ID 命名规范：`<真实模型名>-<供应商缩写>`，如 `deepseek-v4-flash-DS`。

### .env

```
PORT=8787                    # 监听端口，默认 8787
RUST_LOG=info                # 日志级别
OPENCODE_ZEN_KEY=public      # Zen 套餐 Key
OPENCODE_GO_KEY=             # Go 套餐 Key
DEEPSEEK_API_KEY=            # DeepSeek 官方 Key
```

## 开发构建

```bash
# 需要 Rust 工具链
cargo build --release
```

构建产物在 `target/release/AIGate.exe`。

## License

MIT
