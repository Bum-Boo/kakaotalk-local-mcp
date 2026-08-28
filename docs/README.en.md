[한국어](../README.md) | **English** | [日本語](README.ja.md) | [中文](README.zh-CN.md)

# KakaoTalk Local MCP

[![CI](https://github.com/Bum-Boo/kakaotalk-local-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Bum-Boo/kakaotalk-local-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D4.svg)](#requirements)

An **unofficial, local-first bridge** connecting the KakaoTalk PC app for Windows to a local MCP client. It handles only chat rooms that the user has explicitly allowed, and message sending and automatic replies are disabled by default.

> [!WARNING]
> This project is not affiliated with Kakao Corp. and is not an official Kakao product. KakaoTalk updates may break its functionality. Before use, review the KakaoTalk Terms of Service and applicable laws yourself.

## Key features

- Accesses only chat rooms registered in the allowlist.
- Exposes user-defined opaque `room_id` values externally instead of actual room titles.
- On first observation, saves the current state as a baseline so that old conversations are not replayed as new messages.
- Blocks duplicate messages and duplicate operations through fingerprint and idempotency state.
- Reply delivery follows the sequence `prepare → user approval → commit → readback`.
- `send_enabled` and `auto_reply_enabled` default to `false`.
- Can optionally identify schedule candidates locally and pass them to a separate scheduling agent.
- The optional backend watcher processes only a small, explicitly selected set of rooms and does not save raw keys or plaintext databases to files.
- Does not call an AI model while idle.

## Safety boundaries

This project does not provide the following capabilities:

- Extraction of KakaoTalk account passwords, sessions, or credentials
- Implementation of a private network protocol
- Unrestricted collection of all chat rooms
- Full conversation export
- Storage of raw database keys or plaintext databases
- Bulk message sending
- Automatic replies without approval

Do not expose the local MCP server directly to the internet or a public network. We recommend that you do not place real configuration files, state databases, logs, or chat captures in a Git repository or cloud-synced folder.

## Requirements

- Windows 10 or Windows 11
- A signed-in KakaoTalk PC app
- Python 3.11 or later
- PowerShell
- An MCP client capable of running a stdio MCP server

## Installation

Clone the repository in PowerShell, then run the installation script.

```powershell
git clone https://github.com/Bum-Boo/kakaotalk-local-mcp.git
cd kakaotalk-local-mcp
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

The installation script creates a project-specific `.venv` and copies the safe example configuration only if `config.json` does not already exist.

## Basic configuration

`config.json` is not included in the public repository. Start with both sending and schedule automation turned off.

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

### Registering a chat room

Open exactly one target chat room in its own window, then run the command below to register it without displaying the room title in the console.

```powershell
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json adopt-open-room --room-id self-test
```

If exactly one chat room is not open, the configuration is left unchanged. The `room_id` is a local alias used by MCP and may differ from the actual chat-room title.

After applying the configuration, check it with the following commands.

```powershell
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json validate-config
.\scripts\doctor.cmd
```

## Connecting an MCP client

Register the following executable in your MCP client's stdio server settings. Replace the path with the actual repository path.

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

After connecting, call only `kakao_health` first to verify the local bridge status and confirm that sending is disabled.

## Available tools

| Tool | Description |
|---|---|
| `kakao_health` | Checks runtime status and approved source aliases without reading messages. |
| `kakao_allowed_rooms` | Returns only allowed opaque room IDs. |
| `kakao_read_room` | Reads a bounded set of recent messages and their fingerprint from an allowed room. |
| `kakao_observe_room` | Creates a baseline or generates new-message events. |
| `kakao_poll_events` | Retrieves new events stored locally. |
| `kakao_poll_schedule_candidates` | Retrieves schedule candidates awaiting analysis. |
| `kakao_get_schedule_candidate` | Retrieves one candidate by its opaque candidate ID. |
| `kakao_update_schedule_candidate` | Records the candidate's processing status. |
| `kakao_prepare_reply` | Prepares a one-time send approval bound to the current fingerprint. |
| `kakao_commit_reply` | Sends an approved draft exactly once and verifies the result again. |
| `kakao_operation_status` | Checks the current status of a prepared operation. |

## Sending messages

Even when an actual message must be sent, follow this sequence:

1. Check the latest fingerprint with `kakao_read_room`.
2. Show the proposed message to the user.
3. Prepare a one-time operation with `kakao_prepare_reply`.
4. The user explicitly approves it in the current turn.
5. Call `kakao_commit_reply` exactly once.
6. If a newer message has appeared or the readback result is unclear, do not retry automatically.

If `send_enabled` is `false` in the configuration, the commit step does not send the message.

## Optional watcher

Run the standard UI watcher as follows:

```powershell
.\.venv\Scripts\hermes-kakao-watch.exe --once
.\.venv\Scripts\hermes-kakao-watch.exe
```

Use the optional backend watcher only after configuring separately approved room IDs and the currently verified KakaoTalk version.

```json
{
  "backend_collector": {
    "enabled": true,
    "mode": "ram_only_v2",
    "room_ids": ["approved-room-one"],
    "max_batch_rows": 200,
    "bootstrap_retry_seconds": 30,
    "expected_client_version": "currently verified version"
  }
}
```

If the KakaoTalk version differs from the configured value, the backend watcher stops before accessing data.

## Development and validation

```powershell
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run python tests\smoke_mcp.py
```

GitHub Actions also tests combinations of Windows and Ubuntu with Python 3.11 and 3.12.

## Please credit the creator

When publishing an article, video, demo, research project, or derivative project that uses this project, we would appreciate it if you mention the creator and repository as follows:

> Made with [KakaoTalk Local MCP](https://github.com/Bum-Boo/kakaotalk-local-mcp) by [@Bum-Boo](https://github.com/Bum-Boo)

You must retain the copyright and license notices required by the MIT License. This request for public attribution does not add a legal condition or restriction; it simply helps people find the project's creator and original repository.

## Projects that inspired this work

This project was inspired by ideas and prior work from the following open-source projects. We thank their creators for making their excellent work public.

- [kronenz/kakaotalk-mcp](https://github.com/kronenz/kakaotalk-mcp) — Win32 window discovery and MCP connection approach
- [johklo/moltbot](https://github.com/johklo/moltbot) — Baselines, message fingerprints, and pre-send revalidation
- [channprj/kmsg](https://github.com/channprj/kmsg) — Local aliases, bounded state management, and fail-closed design
- [is-theo/kakao-cli-win](https://github.com/is-theo/kakao-cli-win) — Starting point for research into the Windows v2 SQLCipher structure

The revisions consulted and their license information are recorded in [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md). This does not mean that code from these projects is bundled unchanged or that they provide official support.

## Privacy, security, and license

- Privacy boundaries: [`PRIVACY.md`](../PRIVACY.md)
- Vulnerability reporting and threat model: [`SECURITY.md`](../SECURITY.md)
- Third-party notices: [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)
- License: [MIT](../LICENSE)
