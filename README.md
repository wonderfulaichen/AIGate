# AIGate

[![License](https://img.shields.io/badge/license-GPLv3-blue.svg)](LICENSE)
![Rust](https://img.shields.io/badge/Rust-1.75%2B-orange)
[![Release](https://img.shields.io/github/v/release/wonderfulaichen/AIGate)](https://github.com/wonderfulaichen/AIGate/releases)

**AIGate** 是一个本地 OpenAI 兼容反向代理，提供多供应商路由、模型名映射、参数注入、实时监控面板等功能，并以桌面应用形式交付（原生窗口 + 系统托盘 + 后台运行）。

**核心价值**：统一管理多个 AI 供应商的 API 端点，通过一套 OpenAI 兼容接口对外暴露，同时提供可视化监控和管理能力。

---

## 目录

- [功能特性](#功能特性)
- [系统要求](#系统要求)
- [安装](#安装)
  - [下载预编译包](#下载预编译包)
  - [从源码构建](#从源码构建)
- [快速开始](#快速开始)
- [配置指南](#配置指南)
  - [providers.json](#providersjson)
  - [.env 环境变量](#env-环境变量)
  - [供应商路由规则](#供应商路由规则)
  - [模型名映射](#模型名映射)
  - [参数注入](#参数注入)
- [客户端配置](#客户端配置)
- [管理面板](#管理面板)
  - [请求日志](#请求日志)
  - [Token 统计](#token-统计)
  - [趋势图表](#趋势图表)
  - [供应商管理](#供应商管理)
- [API 参考](#api-参考)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [许可](#许可)

---

## 功能特性

### 核心代理

- **多供应商路由** — 在 `providers.json` 中配置任意多个上游 API，根据模型 ID 自动路由到对应端点
- **模型名映射** — 客户端使用带后缀的模型 ID（如 `deepseek-v4-flash-DS`），网关自动替换为上游真实模型名
- **参数注入** — 对指定模型自动注入 `reasoning_effort`（思考强度）、`extra_body`（额外字段），不覆盖客户端已有值
- **API Key 管理** — 每个供应商可独立配置 API Key，通过环境变量注入，面板内可视化编辑
- **IPv4 强制解析** — 自定义 DNS 解析器过滤 IPv6 地址，避免 IPv6 不通导致连接失败
- **SSE 流式透传** — 完整支持 Server-Sent Events 流式响应，逐块转发不缓冲
- **Token 用量解析** — 从上游响应中解析 `usage` 字段，统计 prompt / completion token 数

### 桌面应用

- **原生窗口** — 基于 `winit` + `wry` 的内嵌 WebView，无需浏览器即可使用管理面板
- **系统托盘** — 最小化到托盘后台运行，双击托盘图标恢复窗口
- **托盘菜单** — 右键托盘图标显示菜单：打开面板 / 退出
- **自启动配置** — 首次运行自动生成 `providers.json` 和 `.env`

### 监控面板

- **实时请求日志** — 最近 100 条请求流水，含模型、供应商、状态码、延迟
- **Token 统计** — 按时间范围聚合 prompt / completion token 及总 token 数
- **趋势图表** — 支持小时 / 日 / 月三个粒度，含请求数和 token 量两种维度
- **饼图分布** — 按模型维度展示请求量占比
- **供应商管理** — 查看当前所有供应商配置，支持连接测试
- **API Key 管理** — 可视化编辑各供应商的 API Key，实时生效
- **日志导出** — 将请求日志导出为 JSON 文件

---

## 系统要求

| 平台 | 支持情况 |
|------|---------|
| Windows 10/11 | ✅ 原生支持 |
| macOS | 需自行构建 |
| Linux (X11/Wayland) | 需自行构建 |

运行时依赖：

- Windows: 无需额外运行时（已静态链接）
- 其他平台: 需安装 WebKit2GTK（`wry` 依赖）

### 从源码构建

构建环境：

- Rust 1.75+
- Windows: 仅需 Visual Studio Build Tools（`rust-mscv` 工具链）
- Linux: `sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev`

```bash
git clone https://github.com/wonderfulaichen/AIGate.git
cd AIGate
cargo build --release
```

构建产物在 `target/release/AIGate.exe`（Windows）或 `target/release/aigate`（其他平台）。

---

## 配置指南

### providers.json

`providers.json` 定义所有上游供应商及其模型映射，首次启动时自动生成示例配置。

#### 完整结构

```json
{
  "_说明": "供应商配置文件",
  "providers": [
    {
      "name": "供应商名称",
      "endpoint": "https://你的端点/v1/chat/completions",
      "api_key_env": "环境变量名",
      "api_key_default": "默认值（可选）",
      "headers": {
        "额外请求头": "值"
      },
      "models": {
        "客户端模型ID": {
          "upstream_model": "上游真实模型名",
          "reasoning_effort": "low|medium|high|max",
          "extra_body": {
            "额外字段": "值"
          }
        }
      }
    }
  ]
}
```

#### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 供应商标识，仅用于日志显示 |
| `endpoint` | string | 是 | 上游 chat/completions 完整 URL |
| `api_key_env` | string | 是 | 读取 API Key 的环境变量名，对应 `.env` 中的 key |
| `api_key_default` | string | 否 | 环境变量未设置时的默认值（如 `public`） |
| `headers` | object | 否 | 额外注入的 HTTP 请求头（键值对） |
| `models` | object | 是 | 模型映射表，key 是客户端使用的模型 ID |

每个模型的配置：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `upstream_model` | string | 是 | 转发时替换 `model` 字段为上游真实模型名 |
| `reasoning_effort` | string | 否 | 思考强度：`low` / `medium` / `high` / `max` |
| `extra_body` | object | 否 | 额外注入的请求体字段（任意 JSON） |

#### 模型 ID 命名规范

```
<真实模型名>-<供应商缩写>
```

例如：

| 模型 ID | 含义 |
|---------|------|
| `deepseek-v4-flash-DS` | DeepSeek 官方（DS） |
| `deepseek-v4-pro-GO` | Go 套餐（GO） |
| `big-pickle-ZEN` | Zen 免费套餐（ZEN） |

#### 示例

```json
{
  "providers": [
    {
      "name": "deepseek",
      "endpoint": "https://api.deepseek.com/v1/chat/completions",
      "api_key_env": "DEEPSEEK_API_KEY",
      "models": {
        "deepseek-v4-flash-DS": {
          "upstream_model": "deepseek-v4-flash",
          "reasoning_effort": "max"
        },
        "deepseek-v4-pro-DS": {
          "upstream_model": "deepseek-v4-pro",
          "reasoning_effort": "max"
        }
      }
    }
  ]
}
```

### .env 环境变量

```
PORT=8787                    # 监听端口，默认 8787
RUST_LOG=info                # 日志级别（info / debug / warn / error）
OPENCODE_ZEN_KEY=public      # Zen 套餐 API Key（免费模型用 public）
OPENCODE_GO_KEY=             # Go 套餐 API Key（留空则不配置）
DEEPSEEK_API_KEY=            # DeepSeek 官方 API Key
```

> 环境变量名对应 `providers.json` 中 `api_key_env` 字段值。如需新增供应商，在 `.env` 中添加对应变量即可。

### 供应商路由规则

请求处理流程：

1. 客户端发送 `POST /v1/chat/completions`，body 中包含 `model` 字段
2. AIGate 遍历所有供应商的 `models` 映射表，查找匹配的 model ID
3. 找到后：替换 `model` 为 `upstream_model` → 注入 `reasoning_effort` / `extra_body` → 设置 `Authorization` 请求头
4. 转发到对应供应商的 `endpoint`
5. 流式或非流式透传上游响应，同时解析 `usage` 字段记录 token 用量

匹配规则：**精确匹配**，客户端传的模型 ID 必须与 `models` 的 key 完全一致。

### 参数注入

**reasoning_effort**：对支持推理的模型，在请求 body 中注入 `reasoning_effort` 字段。如果客户端已携带该字段则不覆盖。

**extra_body**：在请求 body 中注入任意额外字段，可用于传递供应商特有参数。如果客户端已携带同名键则不覆盖。

### 热重载

编辑 `providers.json` 后**无需重启**实例——系统检测到文件变化后会自动重新加载路由表。

---

## 客户端配置

在任何 OpenAI 兼容的客户端中：

```
API 地址: http://127.0.0.1:8787/v1/chat/completions
API Key:  任意值（实际使用 .env 中配置的 key）
模型 ID:  见 providers.json 的 models 映射表 key（如 deepseek-v4-flash-DS）
```

### Model ID 列表

也可以通过 API 获取可用模型列表：

```bash
curl http://127.0.0.1:8787/v1/models
```

返回 OpenAI 兼容格式的模型列表。

---

## 管理面板

启动后默认自动打开管理面板（桌面窗口），也可在浏览器访问 `http://127.0.0.1:8787/admin`（桌面窗口内嵌 WebView 加载同一页面）。

### 面板标签页

#### 请求日志

以表格形式展示最近 100 条请求记录，包含时间、模型、供应商、状态码、延迟、Token 用量。支持单条点击查看详情。右上角可导出日志为 JSON 文件。

#### Token 统计

按时间范围聚合 token 数据，展示总 token、prompt token、completion token 的汇总统计。数据来源于上游响应中的 `usage` 字段，若上游未返回则基于请求/响应体大小估算（约 1 token ≈ 4 字节）。

#### 趋势图表

支持小时 / 日 / 月三个时间粒度，以折线图展示请求量和 token 用量的变化趋势。右侧饼图展示各模型的请求占比。

#### 供应商

查看当前所有已配置的供应商列表及其状态。支持连接测试：发送轻量请求验证端点可用性。支持 API Key 管理：通过面板修改各供应商的 Key，即时生效（不写入 .env 文件，仅运行时生效）。

---

## API 参考

### POST /v1/chat/completions

OpenAI 兼容的聊天补全接口，支持流式（SSE）和非流式响应。

**请求体**：与 OpenAI API 格式一致，`model` 字段使用 `providers.json` 中定义的带后缀模型 ID。

**响应**：与 OpenAI API 格式一致，流式模式下以 `text/event-stream` 逐块返回。

### GET /v1/models

返回当前所有可用模型列表，格式与 OpenAI `/v1/models` 兼容。

### GET /health

健康检查，返回 `ok`。

### GET /admin

管理面板前端页面（HTML + Tailwind CSS + Alpine.js）。

---

## 项目结构

```
AIGate/
├── build.rs                  # 构建脚本：生成 ICO 图标并嵌入 exe
├── Cargo.toml                # 项目元数据与依赖
├── .env.example              # 环境变量模板
├── .gitignore                # Git 忽略规则
├── providers.json            # 供应商路由配置（热重载）
├── icon.rc                   # Windows 资源脚本（图标嵌入）
├── start.bat                 # Windows 启动脚本（首次构建 + 配置引导）
│
├── src/
│   ├── main.rs               # 入口：初始化桌面窗口 + 系统托盘 + HTTP 服务
│   ├── config.rs             # 配置：从环境变量读取端口等参数
│   ├── providers.rs          # 供应商：加载 providers.json，构建路由表
│   ├── proxy.rs              # 代理核心：请求解析、路由、转发、SSE 透传
│   ├── keys.rs               # Key 管理：环境变量 + 运行时面板编辑
│   ├── admin.rs              # 管理后端：请求日志缓冲区 + 统计 API + 管理面板路由
│   ├── admin.html            # 管理前端：单页应用（Tailwind + Alpine.js）
│   └── store.rs              # 日志持久化：JSON Lines 文件写入
│
├── data/                     # 运行时数据目录（自动创建）
│   └── requests.jsonl        # 请求日志持久化文件
│
└── target/                   # 构建产物（已 gitignore）
```

### 模块依赖

```
main.rs
  ├── config.rs        ← .env / 环境变量
  ├── providers.rs     ← providers.json（热重载）
  ├── proxy.rs         ← 请求转发（核心业务逻辑）
  │   ├── providers.rs
  │   ├── keys.rs
  │   └── admin.rs
  ├── admin.rs         ← 管理面板 API
  │   ├── admin.html   ← 前端页面（编译后嵌入）
  │   └── store.rs
  ├── keys.rs
  └── store.rs
```

---

## 常见问题

### 启动后闪退 / 无反应

检查 `target/release/` 目录下是否有 `providers.json` 和 `.env` 文件。首次启动会自动生成，如权限不足可手动创建空文件。更详细的错误信息会通过 Windows 消息框显示。

### 请求返回 404 "未找到模型"

客户端传的模型 ID 不在 `providers.json` 的 models 映射表中。检查模型 ID 是否完全匹配（包括大小写和后缀）。

### Token 统计为 0

部分上游 API 不返回 `usage` 字段。AIGate 会在这种情况下根据请求/响应体大小估算 token 数（1 token ≈ 4 字节），但面板仍会显示估算值。

### 端口被占用

修改 `.env` 中的 `PORT` 变量，或设置系统环境变量 `PORT`。

### 如何新增一个供应商

1. 在 `providers.json` 的 `providers` 数组中添加新条目
2. 在 `.env` 中添加对应的 API Key 环境变量
3. 保存后自动生效（无需重启）

---

## 许可

[GNU General Public License v3.0](LICENSE)

Copyright (C) 2026 wonderfulaichen

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
