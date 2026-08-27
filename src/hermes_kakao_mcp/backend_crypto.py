"""Read-only helpers for encrypted KakaoTalk v2 database snapshots.

The SQLCipher layout investigation was inspired by
https://github.com/is-theo/kakao-cli-win. This module independently implements
a RAM-only, no-key-export path; see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
RESERVE_BYTES = 80
SQLITE_MAGIC = b"SQLite format 3\x00"
WAL_MAGIC = {0x377F0682, 0x377F0683}
MAX_DATABASE_BYTES = 256 << 20
MAX_WAL_BYTES = 64 << 20


class BackendFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WalFrame:
    page_number: int
    database_pages: int
    encrypted_page: bytes


@dataclass(slots=True)
class EncryptedSnapshot:
    database: bytearray
    wal: bytearray

    def wipe(self) -> None:
        self.database[:] = b"\x00" * len(self.database)
        self.wal[:] = b"\x00" * len(self.wal)


def _aes_cbc_decrypt(key: bytes | bytearray, iv: bytes, data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def sqlite_header_oracle(first_page: bytes | bytearray, key: bytes | bytearray) -> bool:
    if len(first_page) < PAGE_SIZE or len(key) != 32:
        return False
    usable = PAGE_SIZE - RESERVE_BYTES
    iv = bytes(first_page[usable : usable + 16])
    encrypted = bytes(first_page[16:usable])
    try:
        tail = _aes_cbc_decrypt(key, iv, encrypted[:96])
    except Exception:
        return False
    if len(tail) < 84:
        return False
    page_size = int.from_bytes(tail[0:2], "big")
    schema_format = int.from_bytes(tail[28:32], "big")
    return (
        page_size == PAGE_SIZE
        and tail[2] in (1, 2)
        and tail[3] in (1, 2)
        and tail[4] == RESERVE_BYTES
        and schema_format in (0, 1, 2, 3, 4)
    )


def decrypt_database_page(
    encrypted_page: bytes | bytearray,
    key: bytes | bytearray,
    page_number: int,
) -> bytes:
    if len(encrypted_page) != PAGE_SIZE or page_number < 1:
        raise BackendFormatError("invalid encrypted database page")
    usable = PAGE_SIZE - RESERVE_BYTES
    iv = bytes(encrypted_page[usable : usable + 16])
    start = 16 if page_number == 1 else 0
    plain = _aes_cbc_decrypt(key, iv, bytes(encrypted_page[start:usable]))
    if page_number == 1:
        plain = SQLITE_MAGIC + plain
    return plain + b"\x00" * RESERVE_BYTES


def decrypt_wal_page(
    encrypted_page: bytes | bytearray,
    key: bytes | bytearray,
    page_number: int,
) -> bytes:
    """Decrypt a SQLCipher page stored in a WAL frame.

    SQLCipher writes a complete codec page to WAL. Unlike page 1 in the main
    database, the WAL frame does not reserve its first 16 bytes for the KDF salt.
    A compatibility fallback is kept for observed variants and is accepted only
    when it reconstructs a valid SQLite header.
    """
    if len(encrypted_page) != PAGE_SIZE or page_number < 1:
        raise BackendFormatError("invalid encrypted WAL page")
    usable = PAGE_SIZE - RESERVE_BYTES
    iv = bytes(encrypted_page[usable : usable + 16])
    if page_number != 1:
        return _aes_cbc_decrypt(key, iv, bytes(encrypted_page[:usable])) + b"\x00" * RESERVE_BYTES

    full = _aes_cbc_decrypt(key, iv, bytes(encrypted_page[:usable]))
    if full.startswith(SQLITE_MAGIC):
        return full + b"\x00" * RESERVE_BYTES
    legacy = SQLITE_MAGIC + _aes_cbc_decrypt(key, iv, bytes(encrypted_page[16:usable]))
    if legacy.startswith(SQLITE_MAGIC):
        return legacy + b"\x00" * RESERVE_BYTES
    raise BackendFormatError("WAL page 1 did not decrypt to a SQLite header")


def _wal_checksum(
    data: bytes | bytearray,
    byteorder: str,
    checksum: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    if len(data) % 8:
        raise BackendFormatError("WAL checksum input is not 8-byte aligned")
    first, second = checksum
    for offset in range(0, len(data), 8):
        word_a = int.from_bytes(data[offset : offset + 4], byteorder)
        word_b = int.from_bytes(data[offset + 4 : offset + 8], byteorder)
        first = (first + word_a + second) & 0xFFFFFFFF
        second = (second + word_b + first) & 0xFFFFFFFF
    return first, second


def parse_committed_wal(wal: bytes | bytearray) -> tuple[tuple[WalFrame, ...], int | None]:
    if not wal:
        return (), None
    if len(wal) < 32:
        raise BackendFormatError("truncated WAL header")
    magic = int.from_bytes(wal[0:4], "big")
    if magic not in WAL_MAGIC:
        raise BackendFormatError("unsupported WAL magic")
    page_size = int.from_bytes(wal[8:12], "big")
    if page_size == 1:
        page_size = 65536
    if page_size != PAGE_SIZE:
        raise BackendFormatError("unexpected WAL page size")
    frame_size = 24 + page_size
    complete_frame_end = 32 + ((len(wal) - 32) // frame_size) * frame_size

    stored_header_checksum = (
        int.from_bytes(wal[24:28], "big"),
        int.from_bytes(wal[28:32], "big"),
    )
    checksum_byteorder = None
    for byteorder in ("little", "big"):
        if _wal_checksum(wal[:24], byteorder) == stored_header_checksum:
            checksum_byteorder = byteorder
            break
    if checksum_byteorder is None:
        raise BackendFormatError("WAL header checksum mismatch")

    salt = bytes(wal[16:24])
    checksum = stored_header_checksum
    frames: list[WalFrame] = []
    last_commit_index: int | None = None
    committed_database_pages: int | None = None
    for offset in range(32, complete_frame_end, frame_size):
        header = wal[offset : offset + 24]
        encrypted_page = wal[offset + 24 : offset + frame_size]
        if bytes(header[8:16]) != salt:
            break
        expected = (
            int.from_bytes(header[16:20], "big"),
            int.from_bytes(header[20:24], "big"),
        )
        checksum = _wal_checksum(header[:8], checksum_byteorder, checksum)
        checksum = _wal_checksum(encrypted_page, checksum_byteorder, checksum)
        if checksum != expected:
            break
        page_number = int.from_bytes(header[:4], "big")
        database_pages = int.from_bytes(header[4:8], "big")
        if page_number < 1:
            break
        frames.append(WalFrame(page_number, database_pages, bytes(encrypted_page)))
        if database_pages:
            last_commit_index = len(frames) - 1
            committed_database_pages = database_pages

    if last_commit_index is None:
        return (), None
    return tuple(frames[: last_commit_index + 1]), committed_database_pages


def decrypt_snapshot(
    encrypted_database: bytes | bytearray,
    encrypted_wal: bytes | bytearray,
    key: bytes | bytearray,
) -> bytearray:
    if not encrypted_database or len(encrypted_database) % PAGE_SIZE:
        raise BackendFormatError("database is empty or not page-aligned")
    if len(key) != 32 or not sqlite_header_oracle(encrypted_database[:PAGE_SIZE], key):
        raise BackendFormatError("database key oracle failed")

    plain = bytearray(len(encrypted_database))
    for page_number, offset in enumerate(range(0, len(encrypted_database), PAGE_SIZE), start=1):
        page = decrypt_database_page(encrypted_database[offset : offset + PAGE_SIZE], key, page_number)
        plain[offset : offset + PAGE_SIZE] = page

    frames, committed_database_pages = parse_committed_wal(encrypted_wal)
    for frame in frames:
        page = decrypt_wal_page(frame.encrypted_page, key, frame.page_number)
        end = frame.page_number * PAGE_SIZE
        if end > MAX_DATABASE_BYTES:
            plain[:] = b"\x00" * len(plain)
            raise BackendFormatError("WAL database size is out of bounds")
        if end > len(plain):
            plain.extend(b"\x00" * (end - len(plain)))
        start = end - PAGE_SIZE
        plain[start:end] = page

    if committed_database_pages is not None:
        final_size = committed_database_pages * PAGE_SIZE
        if final_size <= 0 or final_size > MAX_DATABASE_BYTES:
            plain[:] = b"\x00" * len(plain)
            raise BackendFormatError("committed WAL database size is out of bounds")
        if final_size > len(plain):
            plain.extend(b"\x00" * (final_size - len(plain)))
        elif final_size < len(plain):
            del plain[final_size:]
        plain[28:32] = committed_database_pages.to_bytes(4, "big")

    plain[18] = 1
    plain[19] = 1
    return plain


def _signature(path: Path) -> tuple[bool, int, int]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False, 0, 0
    return True, stat.st_size, stat.st_mtime_ns


def _read_into_mutable(path: Path, expected_size: int) -> bytearray:
    if expected_size == 0:
        return bytearray()
    buffer = bytearray(expected_size)
    with path.open("rb", buffering=0) as handle:
        count = handle.readinto(buffer)
    if count != expected_size:
        buffer[:] = b"\x00" * len(buffer)
        raise BackendFormatError("short encrypted snapshot read")
    return buffer


def read_stable_snapshot(path: Path, attempts: int = 5) -> EncryptedSnapshot:
    wal_path = path.with_name(f"{path.name}-wal")
    for _attempt in range(attempts):
        before_database = _signature(path)
        before_wal = _signature(wal_path)
        if not before_database[0] or not 0 < before_database[1] <= MAX_DATABASE_BYTES:
            raise BackendFormatError("database size is out of bounds")
        if before_wal[1] > MAX_WAL_BYTES:
            raise BackendFormatError("WAL size is out of bounds")
        database = _read_into_mutable(path, before_database[1])
        wal = _read_into_mutable(wal_path, before_wal[1]) if before_wal[0] else bytearray()
        after_database = _signature(path)
        after_wal = _signature(wal_path)
        if before_database == after_database and before_wal == after_wal:
            return EncryptedSnapshot(database, wal)
        database[:] = b"\x00" * len(database)
        wal[:] = b"\x00" * len(wal)
        time.sleep(0.05)
    raise BackendFormatError("encrypted snapshot did not stabilize")


def deserialize_read_only(plain: bytearray) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(plain)
        connection.execute("PRAGMA query_only=ON")
        check = connection.execute("PRAGMA quick_check(1)").fetchone()
        if check is None or check[0] != "ok":
            raise BackendFormatError("SQLite quick_check failed")
        return connection
    except Exception:
        connection.close()
        raise
    finally:
        plain[:] = b"\x00" * len(plain)


def open_encrypted_snapshot(path: Path, key: bytearray) -> sqlite3.Connection:
    snapshot = read_stable_snapshot(path)
    plain: bytearray | None = None
    try:
        plain = decrypt_snapshot(snapshot.database, snapshot.wal, key)
        return deserialize_read_only(plain)
    finally:
        snapshot.wipe()
        if plain is not None:
            plain[:] = b"\x00" * len(plain)
