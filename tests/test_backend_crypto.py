from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hermes_kakao_mcp.backend_crypto import (
    PAGE_SIZE,
    RESERVE_BYTES,
    SQLITE_MAGIC,
    _wal_checksum,
    decrypt_snapshot,
    parse_committed_wal,
    read_stable_snapshot,
    sqlite_header_oracle,
)

KEY = bytes(range(32))
SALT = bytes(range(16, 32))
WAL_SALT = bytes(range(8))


def plain_page(page_number: int, fill: int) -> bytes:
    page = bytearray([fill] * PAGE_SIZE)
    if page_number == 1:
        page[:16] = SQLITE_MAGIC
        page[16:18] = PAGE_SIZE.to_bytes(2, "big")
        page[18] = 2
        page[19] = 2
        page[20] = RESERVE_BYTES
        page[21:24] = bytes((64, 32, 32))
        page[44:48] = (4).to_bytes(4, "big")
        page[28:32] = (2).to_bytes(4, "big")
    page[-RESERVE_BYTES:] = b"\x00" * RESERVE_BYTES
    return bytes(page)


def encrypt_page(plain: bytes, page_number: int, *, wal: bool = False) -> bytes:
    usable = PAGE_SIZE - RESERVE_BYTES
    iv = bytes([page_number + 20]) * 16
    start = 0 if wal or page_number != 1 else 16
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(plain[start:usable]) + encryptor.finalize()
    prefix = b"" if start == 0 else SALT
    return prefix + encrypted + iv + b"\x00" * 64


def build_wal(encrypted_page: bytes, *, page_number: int = 2, database_pages: int = 2) -> bytes:
    header = bytearray(32)
    header[0:4] = (0x377F0682).to_bytes(4, "big")
    header[4:8] = (3007000).to_bytes(4, "big")
    header[8:12] = PAGE_SIZE.to_bytes(4, "big")
    header[12:16] = (1).to_bytes(4, "big")
    header[16:24] = WAL_SALT
    checksum = _wal_checksum(header[:24], "little")
    header[24:28] = checksum[0].to_bytes(4, "big")
    header[28:32] = checksum[1].to_bytes(4, "big")

    frame = bytearray(24)
    frame[0:4] = page_number.to_bytes(4, "big")
    frame[4:8] = database_pages.to_bytes(4, "big")
    frame[8:16] = WAL_SALT
    checksum = _wal_checksum(frame[:8], "little", checksum)
    checksum = _wal_checksum(encrypted_page, "little", checksum)
    frame[16:20] = checksum[0].to_bytes(4, "big")
    frame[20:24] = checksum[1].to_bytes(4, "big")
    return bytes(header + frame + encrypted_page)


def test_database_oracle_and_base_decryption_round_trip() -> None:
    first = encrypt_page(plain_page(1, 0), 1)
    second = encrypt_page(plain_page(2, 7), 2)

    assert sqlite_header_oracle(first, KEY)
    plain = decrypt_snapshot(first + second, b"", KEY)

    assert plain[:16] == SQLITE_MAGIC
    assert plain[18:20] == b"\x01\x01"
    assert plain[PAGE_SIZE : PAGE_SIZE * 2 - RESERVE_BYTES] == bytes([7]) * (
        PAGE_SIZE - RESERVE_BYTES
    )


def test_committed_wal_frame_is_validated_decrypted_and_applied() -> None:
    base = encrypt_page(plain_page(1, 0), 1) + encrypt_page(plain_page(2, 3), 2)
    replacement = plain_page(2, 9)
    wal = build_wal(encrypt_page(replacement, 2, wal=True))

    frames, database_pages = parse_committed_wal(wal)
    plain = decrypt_snapshot(base, wal, KEY)

    assert len(frames) == 1
    assert database_pages == 2
    assert plain[PAGE_SIZE : PAGE_SIZE * 2] == replacement
    assert int.from_bytes(plain[28:32], "big") == 2


def test_wal_page_one_uses_full_codec_page_not_database_salt_layout() -> None:
    base = encrypt_page(plain_page(1, 0), 1) + encrypt_page(plain_page(2, 3), 2)
    replacement = bytearray(plain_page(1, 0))
    replacement[24:28] = (99).to_bytes(4, "big")
    wal = build_wal(encrypt_page(bytes(replacement), 1, wal=True), page_number=1)

    plain = decrypt_snapshot(base, wal, KEY)

    assert plain[:16] == SQLITE_MAGIC
    assert int.from_bytes(plain[24:28], "big") == 99


def test_wal_checksum_tamper_ends_valid_chain_without_applying_frame() -> None:
    page = encrypt_page(plain_page(2, 9), 2, wal=True)
    wal = bytearray(build_wal(page))
    wal[-1] ^= 1

    frames, database_pages = parse_committed_wal(wal)

    assert frames == ()
    assert database_pages is None


def test_stale_wal_generation_tail_is_ignored_after_last_valid_commit() -> None:
    page = encrypt_page(plain_page(2, 9), 2, wal=True)
    current = build_wal(page)
    stale = bytearray(build_wal(page)[32:])
    stale[8:16] = b"stale!!!"

    frames, database_pages = parse_committed_wal(current + stale)

    assert len(frames) == 1
    assert database_pages == 2


def test_stable_snapshot_reads_database_and_optional_wal(tmp_path: Path) -> None:
    database = tmp_path / "chat.edb"
    database.write_bytes(b"a" * PAGE_SIZE)
    wal = tmp_path / "chat.edb-wal"
    wal.write_bytes(b"b" * 32)

    snapshot = read_stable_snapshot(database)
    try:
        assert len(snapshot.database) == PAGE_SIZE
        assert len(snapshot.wal) == 32
    finally:
        snapshot.wipe()

    assert set(snapshot.database) <= {0}
    assert set(snapshot.wal) <= {0}
