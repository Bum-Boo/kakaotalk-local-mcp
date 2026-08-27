# Third-party notices

This project is an original, restricted implementation informed by the following MIT-licensed projects.
No private KakaoTalk network protocol or credential/session extraction is used. The optional v2 backend
independently implements a read-only, RAM-only SQLCipher path and does not persist raw keys or plaintext DBs.

## kronenz/kakaotalk-mcp

- Repository: https://github.com/kronenz/kakaotalk-mcp
- Inspected commit: `7b41dd9da473e7a9e2f4b07ec27bdf3289fcc5fb`
- License: MIT
- Ideas adapted: Win32 KakaoTalk window classes, exact-title window discovery, clipboard transcript extraction, and stdio FastMCP exposure.

## johklo/moltbot

- Repository: https://github.com/johklo/moltbot
- Inspected commit: `ef08636f81a25b40f65688d59203d27e9f5b442a`
- License: MIT
- Ideas adapted: startup baseline, message fingerprinting, media/self filtering, and pre-send transcript recheck.

## channprj/kmsg

- Repository: https://github.com/channprj/kmsg
- Inspected commit: `4c8ab798f4adc534361152edca7301b659390850`
- License: MIT
- Ideas adapted: local synthetic room IDs, background-safe fail-closed behavior, structured stdout/stderr separation, bounded state, and explicit interaction modes.

## is-theo/kakao-cli-win

- Repository: https://github.com/is-theo/kakao-cli-win
- Inspected commit: `411d0dd1b472ea8a0cc8bf399e3351c0651748b6`
- License: MIT
- Ideas independently reimplemented: Windows v2 SQLCipher codec-context discovery, raw-key page-header oracle, and encrypted-page layout.
- Upstream persistence/export defaults are not used: this project emits no key, memory address, plaintext DB, full-history export, or broad localhost API.

The upstream projects are not bundled as runtime dependencies and their names do not imply endorsement.
