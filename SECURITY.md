# Security policy

## Supported versions

Only the latest `main` branch is supported while the project is pre-1.0.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities that could expose KakaoTalk data, local paths, credentials, room identities, or message content. Use GitHub's private security advisory feature for this repository. Include the affected revision, reproduction steps, and the smallest redacted diagnostic output needed to reproduce the issue.

## Threat model

This project assumes:

- the user owns and controls the Windows account and KakaoTalk session;
- the MCP server and state remain local to that account;
- every readable room is explicitly allowlisted;
- outbound sending and automatic replies are disabled by default;
- source text is untrusted input, including text later shown to an LLM;
- client updates may invalidate internal layouts and must fail closed.

The project does **not** defend a compromised Windows account, administrator, kernel, or malicious KakaoTalk client. It must not be exposed directly to a LAN or the public internet.

## Security invariants

- No session-token, password, or credential extraction.
- No private KakaoTalk network protocol implementation.
- No raw database key, plaintext database, or full-history export on disk.
- No room access outside the local allowlist.
- First observation creates a baseline; history is not replayed as new activity.
- Sending requires local opt-in plus prepare/commit/readback gates.
- Unknown client versions stop before backend key discovery or database mapping.
- Logs and health endpoints must not include room titles or message bodies.

## Safe diagnostics

Before sharing logs or bug reports, remove:

- `config.json` and all backups;
- `state/`, SQLite, WAL, and SHM files;
- room titles, participant names, aliases, and message text;
- usernames, home directories, profile paths, task XML, and environment dumps.
