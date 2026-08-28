[한국어](../README.md) | [English](README.en.md) | **日本語** | [中文](README.zh-CN.md)

# KakaoTalk Local MCP

[![CI](https://github.com/Bum-Boo/kakaotalk-local-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Bum-Boo/kakaotalk-local-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D4.svg)](#動作環境)

Windows版KakaoTalk PCアプリをローカルMCPクライアントに接続する、**非公式・ローカル優先のブリッジ**です。ユーザーが明示的に許可したチャットルームだけを扱い、メッセージ送信と自動返信はデフォルトで無効になっています。

> [!WARNING]
> 本プロジェクトはKakao Corp.とは関係がなく、Kakaoの公式製品ではありません。KakaoTalkのアップデートにより機能しなくなる可能性があります。利用前に、KakaoTalkの利用規約および関連法令を各自で確認してください。

## 主な特徴

- 許可リストに登録したチャットルームだけにアクセスします。
- 実際のルーム名ではなく、ユーザーが指定した不透明な`room_id`を外部に公開します。
- 初回観察時に現在の状態をベースラインとして保存し、過去の会話を新着メッセージとして再生しません。
- fingerprintとidempotency状態により、同一メッセージと重複操作を遮断します。
- 返信送信は`prepare → ユーザー承認 → commit → readback`の順序に従います。
- `send_enabled`と`auto_reply_enabled`のデフォルト値は`false`です。
- 必要に応じて予定候補をローカルで選別し、別のスケジュール管理エージェントに渡せます。
- オプションのbackend watcherは、明示的に選択した少数のルームだけを処理し、raw keyや平文データベースをファイルに保存しません。
- アイドル時にはAIモデルを呼び出しません。

## 安全上の境界

本プロジェクトは次の機能を提供しません。

- KakaoTalkアカウントのパスワード、セッション、認証情報の抽出
- 非公開ネットワークプロトコルの実装
- 全チャットルームの無制限な収集
- 会話履歴全体のエクスポート
- raw DB keyまたは平文DBの保存
- メッセージの一括送信
- 承認のない自動返信

ローカルMCPサーバーをインターネットや公開ネットワークに直接公開しないでください。実際の設定、状態DB、ログ、チャットのキャプチャは、Gitリポジトリやクラウド同期フォルダーに置かないことを推奨します。

## 動作環境

- Windows 10またはWindows 11
- ログイン済みのKakaoTalk PCアプリ
- Python 3.11以降
- PowerShell
- stdio MCPサーバーを実行できるMCPクライアント

## インストール

PowerShellでリポジトリを取得し、インストールスクリプトを実行してください。

```powershell
git clone https://github.com/Bum-Boo/kakaotalk-local-mcp.git
cd kakaotalk-local-mcp
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

インストールスクリプトはプロジェクト専用の`.venv`を作成し、`config.json`が存在しない場合に限り、安全なサンプル設定をコピーします。

## 基本設定

`config.json`は公開リポジトリに含まれません。最初は送信機能と予定の自動化をどちらも無効にした状態で開始してください。

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

### チャットルームの登録

対象のチャットルームを別ウィンドウで1つだけ開き、次のコマンドを実行すると、ルーム名をコンソールに表示せずに登録できます。

```powershell
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json adopt-open-room --room-id self-test
```

開いているチャットルームが正確に1つでない場合、設定は変更されません。`room_id`はMCPで使用するローカルエイリアスであり、実際のチャットルーム名と異なっていてもかまいません。

設定適用後、次のコマンドで確認してください。

```powershell
.\.venv\Scripts\hermes-kakao-mcp.exe --config .\config.json validate-config
.\scripts\doctor.cmd
```

## MCPクライアントへの接続

MCPクライアントのstdioサーバー設定に、次の実行ファイルを登録してください。パスは実際のリポジトリのパスに置き換える必要があります。

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

接続後は、まず`kakao_health`だけを呼び出し、ローカルブリッジの状態と送信が無効であることを確認してください。

## 提供ツール

| ツール | 説明 |
|---|---|
| `kakao_health` | メッセージを読まずに実行状態と承認済みの送信元エイリアスを確認します。 |
| `kakao_allowed_rooms` | 許可された不透明なルームIDだけを返します。 |
| `kakao_read_room` | 許可されたルームの件数を制限した最近のメッセージとfingerprintを読み取ります。 |
| `kakao_observe_room` | ベースラインを作成するか、新着メッセージイベントを生成します。 |
| `kakao_poll_events` | ローカルに保存された新着イベントを取得します。 |
| `kakao_poll_schedule_candidates` | 分析待ちの予定候補を取得します。 |
| `kakao_get_schedule_candidate` | 不透明なcandidate IDで候補を1件取得します。 |
| `kakao_update_schedule_candidate` | 候補の処理状態を記録します。 |
| `kakao_prepare_reply` | 現在のfingerprintに紐づいた1回限りの送信承認を準備します。 |
| `kakao_commit_reply` | 承認済みの下書きを1回だけ送信し、結果を再確認します。 |
| `kakao_operation_status` | 準備済み操作の現在の状態を確認します。 |

## メッセージ送信

実際に送信する必要がある場合でも、次の順序を守ってください。

1. `kakao_read_room`で最新のfingerprintを確認します。
2. 送信する下書きをユーザーに提示します。
3. `kakao_prepare_reply`で1回限りの操作を準備します。
4. ユーザーが現在のターンで明示的に承認します。
5. `kakao_commit_reply`を1回だけ呼び出します。
6. より新しいメッセージが届いている場合、またはreadbackの結果が不明確な場合は、自動的に再試行しません。

設定の`send_enabled`が`false`の場合、commit段階では送信されません。

## オプションのwatcher

通常のUI watcherは次のように実行できます。

```powershell
.\.venv\Scripts\hermes-kakao-watch.exe --once
.\.venv\Scripts\hermes-kakao-watch.exe
```

オプションのbackend watcherは、別途承認したルームIDと現在検証済みのKakaoTalkバージョンを設定した場合にのみ使用してください。

```json
{
  "backend_collector": {
    "enabled": true,
    "mode": "ram_only_v2",
    "room_ids": ["approved-room-one"],
    "max_batch_rows": 200,
    "bootstrap_retry_seconds": 30,
    "expected_client_version": "現在検証済みのバージョン"
  }
}
```

KakaoTalkのバージョンが設定値と異なる場合、backend watcherはデータにアクセスする前に停止します。

## 開発と検証

```powershell
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run python tests\smoke_mcp.py
```

GitHub Actionsでも、WindowsとUbuntu、Python 3.11と3.12の組み合わせを検査します。

## 制作者の表記をお願いします

本プロジェクトを使用した記事、動画、デモ、研究、または派生プロジェクトを公開する際は、次のように制作者とリポジトリを併記していただけると幸いです。

> Made with [KakaoTalk Local MCP](https://github.com/Bum-Boo/kakaotalk-local-mcp) by [@Bum-Boo](https://github.com/Bum-Boo)

MITライセンスが要求する著作権表示とライセンス表示は必ず維持してください。上記の公開表記のお願いは、法的な条件や制限を追加するものではなく、プロジェクトの制作者と元のリポジトリを見つけやすくするためのものです。

## 着想を得たプロジェクト

本プロジェクトは、次のオープンソースプロジェクトのアイデアと先行作業から着想を得ています。優れた成果を公開してくださった制作者の皆様に感謝します。

- [kronenz/kakaotalk-mcp](https://github.com/kronenz/kakaotalk-mcp) — Win32ウィンドウ探索とMCP接続方式
- [johklo/moltbot](https://github.com/johklo/moltbot) — ベースライン、メッセージfingerprint、送信前の再検証
- [channprj/kmsg](https://github.com/channprj/kmsg) — ローカルエイリアス、制限された状態管理、fail-closed設計
- [is-theo/kakao-cli-win](https://github.com/is-theo/kakao-cli-win) — Windows v2 SQLCipher構造研究の出発点

参照したrevisionとライセンス情報は[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)に記録されています。上記プロジェクトのコードをそのまま同梱している、または公式サポートを受けているという意味ではありません。

## プライバシー・セキュリティ・ライセンス

- プライバシーに関する境界：[`PRIVACY.md`](../PRIVACY.md)
- 脆弱性の報告と脅威モデル：[`SECURITY.md`](../SECURITY.md)
- 第三者に関する通知：[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)
- ライセンス：[MIT](../LICENSE)
