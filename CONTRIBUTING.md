# Contributing

Thanks for helping improve KakaoTalk Local MCP.

## Before opening a pull request

1. Use synthetic room names and message fixtures only.
2. Never commit `config.json`, state databases, task XML, logs, screenshots, local paths, credentials, raw keys, or conversation content.
3. Keep new capabilities fail-closed and scoped to explicit room IDs.
4. Run:

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```

Windows-specific changes should also be exercised on Windows. Any change touching send behavior must preserve prepare/commit/idempotency/readback gates and requires tests for stale and ambiguous outcomes.

## Scope boundaries

Pull requests implementing credential/session extraction, private network protocols, unrestricted room discovery, bulk send, full-history export, or persistence of raw keys/plaintext databases will not be accepted.
