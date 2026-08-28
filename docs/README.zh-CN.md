[한국어](../README.md) | [English](README.en.md) | [日本語](README.ja.md) | **中文**

# KakaoTalk Local MCP

[![CI](https://github.com/Bum-Boo/kakaotalk-local-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Bum-Boo/kakaotalk-local-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D4.svg)](#环境要求)

一个将 Windows 版 KakaoTalk PC 应用连接到本地 MCP 客户端的**非官方、本地优先桥接工具**。它只处理用户明确允许的聊天室，并且默认禁用消息发送和自动回复。

> [!WARNING]
> 本项目与 Kakao Corp. 无关，也不是 Kakao 的官方产品。KakaoTalk 更新可能会导致功能中断。使用前，请自行查阅 KakaoTalk 服务条款及相关法律。

## 主要特点

- 只访问已加入允许列表的聊天室。
- 对外公开用户指定的不透明 `room_id`，而不是实际聊天室标题。
- 首次观察时将当前状态保存为基线，不会把过去的对话重新当作新消息播放。
- 通过 fingerprint 和 idempotency 状态阻止相同消息和重复操作。
- 回复发送遵循 `prepare → 用户批准 → commit → readback` 顺序。
- `send_enabled` 和 `auto_reply_enabled` 的默认值均为 `false`。
- 可选择在本地筛选日程候选项，并将其传递给独立的日程管理代理。
- 可选的 backend watcher 只处理少量明确选定的聊天室，并且不会把 raw key 或明文数据库保存到文件中。
- 空闲时不会调用 AI 模型。

## 安全边界

本项目不提供以下功能：

- 提取 KakaoTalk 账户密码、会话或凭据
- 实现私有网络协议
- 不受限制地收集所有聊天室
- 导出完整对话
- 存储 raw DB key 或明文数据库
- 批量发送消息
- 未经批准的自动回复

请勿将本地 MCP 服务器直接暴露在互联网或公共网络中。建议不要将真实配置、状态数据库、日志和聊天截图放入 Git 仓库或云同步文件夹。

## 环境要求

- Windows 10 或 Windows 11
- 已登录的 KakaoTalk PC 应用
- Python 3.11 或更高版本
- PowerShell
- 能够运行 stdio MCP 服务器的 MCP 客户端

## 安装

在 PowerShell 中克隆仓库，然后运行安装脚本。

```powershell
git clone https://github.com/Bum-Boo/kakaotalk-local-mcp.git
cd kakaotalk-local-mcp
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

安装脚本会创建项目专用的 `.venv`，并且仅当 `config.json` 不存在时才复制安全的示例配置。

## 基本配置

公开仓库中不包含 `config.json`。开始使用时，请保持发送和日程自动化功能均处于关闭状态。

```json
{
  "adapter": "win32",
  "send_enabled": false,
  "auto_reply_enabled": false,
  "schedule_automation_enabled": false,
  "backend_collector": null,
  "rooms": []
}
```

### 注册聊天室

只在单独窗口中打开一个目标聊天室，然后运行以下命令，即可在不向控制台显示聊天室标题的情况下完成注册。

```powershell
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json adopt-open-room --room-id self-test
```

如果打开的聊天室并非恰好一个，配置不会被更改。`room_id` 是 MCP 使用的本地别名，可以与实际聊天室标题不同。

应用配置后，请运行以下命令进行检查。

```powershell
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json validate-config
.\scripts\doctor.cmd
```

## 连接 MCP 客户端

在 MCP 客户端的 stdio 服务器设置中注册以下可执行文件。请将路径替换为仓库的实际路径。

```json
{
  "mcpServers": {
    "kakaotalk-local": {
      "command": "C:\\Windows\\System32\\cmd.exe",
      "args": [
        "/d",
        "/s",
        "/c",
        "C:\\path\\to\\kakaotalk-local-mcp\\scripts\\run-mcp.cmd"
      ]
    }
  }
}
```

连接后，请先只调用 `kakao_health`，检查本地桥接状态并确认发送功能已禁用。

## 提供的工具

| 工具 | 说明 |
|---|---|
| `kakao_health` | 在不读取消息的情况下检查运行状态和已批准的来源别名。 |
| `kakao_allowed_rooms` | 仅返回已允许的不透明聊天室 ID。 |
| `kakao_read_room` | 读取已允许聊天室中数量受限的近期消息及其 fingerprint。 |
| `kakao_observe_room` | 创建基线或生成新消息事件。 |
| `kakao_poll_events` | 获取本地存储的新事件。 |
| `kakao_poll_schedule_candidates` | 获取等待分析的日程候选项。 |
| `kakao_get_schedule_candidate` | 通过不透明的 candidate ID 获取一个候选项。 |
| `kakao_update_schedule_candidate` | 记录候选项的处理状态。 |
| `kakao_prepare_reply` | 准备绑定当前 fingerprint 的一次性发送批准。 |
| `kakao_commit_reply` | 仅发送一次已批准的草稿，并再次核验结果。 |
| `kakao_operation_status` | 检查已准备操作的当前状态。 |

## 发送消息

即使确实需要发送消息，也请遵循以下顺序：

1. 使用 `kakao_read_room` 检查最新 fingerprint。
2. 向用户展示拟发送的草稿。
3. 使用 `kakao_prepare_reply` 准备一次性操作。
4. 用户在当前轮次中明确批准。
5. 仅调用一次 `kakao_commit_reply`。
6. 如果出现了更新的消息，或者 readback 结果不明确，请勿自动重试。

如果配置中的 `send_enabled` 为 `false`，commit 阶段不会发送消息。

## 可选 watcher

普通 UI watcher 可按以下方式运行：

```powershell
.\.venv\Scripts\hermes-kakao-watch.exe --once
.\.venv\Scripts\hermes-kakao-watch.exe
```

只有在配置了另行批准的聊天室 ID 和当前已验证的 KakaoTalk 版本时，才应使用可选的 backend watcher。

```json
{
  "backend_collector": {
    "enabled": true,
    "mode": "ram_only_v2",
    "room_ids": ["approved-room-one"],
    "max_batch_rows": 200,
    "bootstrap_retry_seconds": 30,
    "expected_client_version": "当前已验证的版本"
  }
}
```

如果 KakaoTalk 版本与配置值不同，backend watcher 会在访问数据前停止。

## 开发与验证

```powershell
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run python tests\smoke_mcp.py
```

GitHub Actions 还会检查 Windows 和 Ubuntu 与 Python 3.11、3.12 的组合。

## 敬请注明创作者

公开使用本项目的文章、视频、演示、研究或衍生项目时，如果能按以下方式同时提及创作者和仓库，我们将不胜感激：

> Made with [KakaoTalk Local MCP](https://github.com/Bum-Boo/kakaotalk-local-mcp) by [@Bum-Boo](https://github.com/Bum-Boo)

请务必保留 MIT 许可证要求的版权和许可证声明。上述公开署名请求不会增加任何法律条件或限制；其目的只是帮助人们找到项目创作者和原始仓库。

## 启发本项目的项目

本项目受到以下开源项目的理念和前期工作的启发。感谢各位创作者公开这些优秀成果。

- [kronenz/kakaotalk-mcp](https://github.com/kronenz/kakaotalk-mcp) — Win32 窗口发现和 MCP 连接方式
- [johklo/moltbot](https://github.com/johklo/moltbot) — 基线、消息 fingerprint 和发送前重新验证
- [channprj/kmsg](https://github.com/channprj/kmsg) — 本地别名、受限状态管理和 fail-closed 设计
- [is-theo/kakao-cli-win](https://github.com/is-theo/kakao-cli-win) — Windows v2 SQLCipher 结构研究的起点

查阅过的 revision 及其许可证信息记录在 [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) 中。这并不意味着本项目原样捆绑了上述项目的代码，也不表示本项目获得其官方支持。

## 隐私、安全与许可证

- 隐私边界：[`PRIVACY.md`](../PRIVACY.md)
- 漏洞报告与威胁模型：[`SECURITY.md`](../SECURITY.md)
- 第三方声明：[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)
- 许可证：[MIT](../LICENSE)
