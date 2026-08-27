"""Version-gated KakaoTalk v2 backend collector.

The initial format research was inspired by
https://github.com/is-theo/kakao-cli-win. This collector is independently
implemented with narrower RAM-only and fail-closed constraints; see
THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import sqlite3
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backend_crypto import PAGE_SIZE, open_encrypted_snapshot, sqlite_header_oracle
from .config import RoomConfig, Settings
from .errors import AdapterError, ConfigurationError

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_BASE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
MAX_REGION_BYTES = 256 << 20
CHUNK_BYTES = 4 << 20
CODEC_PATTERN = struct.pack("<IIIIIIII", 256000, 2, 16, 32, 16, 16, 4096, 0x63)
SELF_SENDER = "self"
OTHER_SENDER = "other"


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


@dataclass(frozen=True, slots=True)
class RecoveryStats:
    codec_context_count: int
    candidate_key_count: int
    matched_database_count: int


@dataclass(frozen=True, slots=True)
class BackendRecord:
    log_id: int
    sender: str
    sent_at: int
    text: str


@dataclass(slots=True)
class EphemeralRoom:
    room_id: str
    source_path: Path
    source_hash: str
    key: bytearray
    legacy_source_hash: str | None = None

    def wipe(self) -> None:
        self.key[:] = b"\x00" * len(self.key)


def _kernel32():
    if os.name != "nt":
        raise AdapterError("windows_required", "The v2 backend collector must run in Windows")
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
    library.OpenProcess.restype = wt.HANDLE
    library.CloseHandle.argtypes = (wt.HANDLE,)
    library.CloseHandle.restype = wt.BOOL
    library.VirtualQueryEx.argtypes = (
        wt.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(MEMORY_BASIC_INFORMATION),
        ctypes.c_size_t,
    )
    library.VirtualQueryEx.restype = ctypes.c_size_t
    library.ReadProcessMemory.argtypes = (
        wt.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    )
    library.ReadProcessMemory.restype = wt.BOOL
    library.QueryFullProcessImageNameW.argtypes = (
        wt.HANDLE,
        wt.DWORD,
        wt.LPWSTR,
        ctypes.POINTER(wt.DWORD),
    )
    library.QueryFullProcessImageNameW.restype = wt.BOOL
    return library


def _kakao_client_version(pid: int) -> str:
    library = _kernel32()
    handle = library.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise AdapterError("backend_client_version_unavailable", "KakaoTalk version could not be read")
    try:
        capacity = wt.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not library.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(capacity)):
            raise AdapterError(
                "backend_client_version_unavailable",
                "KakaoTalk version could not be read",
            )
        try:
            import win32api

            info = win32api.GetFileVersionInfo(buffer.value, "\\")
            major_minor = int(info["FileVersionMS"])
            build_revision = int(info["FileVersionLS"])
        except (ImportError, KeyError, OSError, TypeError, ValueError) as exc:
            raise AdapterError(
                "backend_client_version_unavailable",
                "KakaoTalk version could not be read",
            ) from exc
        return ".".join(
            str(value)
            for value in (
                major_minor >> 16,
                major_minor & 0xFFFF,
                build_revision >> 16,
                build_revision & 0xFFFF,
            )
        )
    finally:
        library.CloseHandle(handle)


def _read_exact(library, handle, address: int, size: int) -> bytearray | None:
    if not address or size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    try:
        ok = library.ReadProcessMemory(
            handle,
            ctypes.c_void_p(address),
            buffer,
            size,
            ctypes.byref(got),
        )
        if not ok or got.value != size:
            return None
        return bytearray(buffer.raw)
    finally:
        ctypes.memset(ctypes.addressof(buffer), 0, size)


def _iter_readable_regions(library, handle):
    info = MEMORY_BASIC_INFORMATION()
    address = 0
    maximum = 0x7FFFFFFFFFFF
    while address < maximum:
        queried = library.VirtualQueryEx(
            handle,
            ctypes.c_void_p(address),
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not queried:
            break
        base = int(info.BaseAddress or address)
        size = int(info.RegionSize)
        protection = int(info.Protect) & 0xFF
        if (
            info.State == MEM_COMMIT
            and protection in READABLE_BASE
            and not (info.Protect & PAGE_GUARD)
            and not (info.Protect & PAGE_NOACCESS)
        ):
            yield base, size
        address = base + (size or PAGE_SIZE)


def _candidate_keys_for_context(library, handle, context_address: int):
    raw = _read_exact(library, handle, context_address, 92)
    if raw is None:
        return
    try:
        values = struct.unpack("<" + "I" * 23, raw)
    finally:
        raw[:] = b"\x00" * len(raw)
    if not (
        values[7] == PAGE_SIZE
        and values[9] == 80
        and values[10] == 64
        and values[14] & 1
    ):
        return
    for cipher_address in (values[19], values[20]):
        cipher_raw = _read_exact(library, handle, cipher_address, 96)
        if cipher_raw is None:
            continue
        try:
            dwords = struct.unpack("<" + "I" * 24, cipher_raw)
        finally:
            cipher_raw[:] = b"\x00" * len(cipher_raw)
        for field in (2, 3):
            candidate = _read_exact(library, handle, dwords[field], 32)
            if candidate is not None:
                if len(candidate) == 32 and any(candidate):
                    yield candidate
                else:
                    candidate[:] = b"\x00" * len(candidate)


def recover_loaded_keys(
    pid: int,
    targets: tuple[Path, ...],
) -> tuple[dict[Path, bytearray], RecoveryStats]:
    """Match loaded SQLCipher raw keys to local files without persisting key material."""
    library = _kernel32()
    first_pages: dict[Path, bytearray] = {}
    for path in targets:
        try:
            if path.stat().st_size < PAGE_SIZE:
                continue
            page = bytearray(PAGE_SIZE)
            with path.open("rb", buffering=0) as source:
                if source.readinto(page) != PAGE_SIZE:
                    page[:] = b"\x00" * len(page)
                    continue
            first_pages[path] = page
        except OSError:
            continue
    if not first_pages:
        raise AdapterError("backend_targets_missing", "No encrypted KakaoTalk database targets were found")

    handle = library.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        for page in first_pages.values():
            page[:] = b"\x00" * len(page)
        raise AdapterError("process_read_failed", "KakaoTalk could not be opened for read-only memory access")

    matches: dict[Path, bytearray] = {}
    seen_contexts: set[int] = set()
    seen_key_hashes: set[bytes] = set()
    codec_count = 0
    candidate_count = 0
    overlap_size = len(CODEC_PATTERN) + 8
    complete = False
    try:
        for base, region_size in _iter_readable_regions(library, handle):
            if region_size <= 0 or region_size > MAX_REGION_BYTES:
                continue
            overlap = bytearray()
            for offset in range(0, region_size, CHUNK_BYTES):
                size = min(CHUNK_BYTES, region_size - offset)
                native_buffer = ctypes.create_string_buffer(size)
                got = ctypes.c_size_t()
                chunk = bytearray()
                search_data = bytearray()
                try:
                    ok = library.ReadProcessMemory(
                        handle,
                        ctypes.c_void_p(base + offset),
                        native_buffer,
                        size,
                        ctypes.byref(got),
                    )
                    if not ok or not got.value:
                        continue
                    chunk = bytearray(native_buffer.raw[: got.value])
                    search_data = overlap + chunk
                    search_base = base + offset - len(overlap)
                    position = 0
                    while True:
                        position = search_data.find(CODEC_PATTERN, position)
                        if position < 0:
                            break
                        context_address = search_base + position - 4
                        position += 4
                        if context_address in seen_contexts:
                            continue
                        seen_contexts.add(context_address)
                        codec_count += 1
                        for candidate in _candidate_keys_for_context(
                            library, handle, context_address
                        ):
                            try:
                                digest = hashlib.sha256(candidate).digest()
                                if digest in seen_key_hashes:
                                    continue
                                seen_key_hashes.add(digest)
                                candidate_count += 1
                                for path, first_page in first_pages.items():
                                    if path in matches:
                                        continue
                                    if sqlite_header_oracle(first_page, candidate):
                                        matches[path] = bytearray(candidate)
                            finally:
                                candidate[:] = b"\x00" * len(candidate)
                            if len(matches) == len(first_pages):
                                complete = True
                                break
                        if complete:
                            break
                    overlap[:] = search_data[-overlap_size:]
                finally:
                    chunk[:] = b"\x00" * len(chunk)
                    search_data[:] = b"\x00" * len(search_data)
                    ctypes.memset(ctypes.addressof(native_buffer), 0, size)
                if complete:
                    break
            overlap[:] = b"\x00" * len(overlap)
            if complete:
                break
    finally:
        library.CloseHandle(handle)
        for page in first_pages.values():
            page[:] = b"\x00" * len(page)
    return matches, RecoveryStats(codec_count, candidate_count, len(matches))


def _decode_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        for encoding in ("utf-8", "cp949", "euc-kr"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", "replace")
    return str(value)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _decode_text(value).strip()
    try:
        return int(text)
    except ValueError:
        return None


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [_decode_text(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _pick(columns: list[str], *candidates: str) -> str | None:
    lookup = {column.casefold(): column for column in columns}
    for candidate in candidates:
        if candidate.casefold() in lookup:
            return lookup[candidate.casefold()]
    return None


def _json_names(value: Any) -> list[str]:
    text = _decode_text(value).strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return [text]
    names: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key in ("nickName", "nickname", "name", "displayName", "userName"):
                name = item.get(key)
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
                    return
            for nested in item.values():
                walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)
        elif isinstance(item, str) and item.strip():
            names.append(item.strip())

    walk(decoded)
    return names


def load_contacts(connection: sqlite3.Connection | None) -> dict[int, str]:
    if connection is None or not _columns(connection, "talkUser"):
        return {}
    columns = _columns(connection, "talkUser")
    user_column = _pick(columns, "userid", "userId", "id")
    nickname_column = _pick(columns, "nickName", "name")
    friend_column = _pick(columns, "friendNickName")
    selected = [column for column in (user_column, nickname_column, friend_column) if column]
    if user_column is None:
        return {}
    contacts: dict[int, str] = {}
    for row in connection.execute(f"SELECT {', '.join(selected)} FROM talkUser"):
        values = dict(zip(selected, row, strict=True))
        user_id = _as_int(values.get(user_column))
        if user_id is None:
            continue
        names = [_decode_text(values.get(column)).strip() for column in selected[1:]]
        names = [name for name in names if name]
        contacts[user_id] = names[-1] if names else str(user_id)
    return contacts


def resolve_room_chat_ids(
    connection: sqlite3.Connection,
    rooms: tuple[RoomConfig, ...],
    contacts: dict[int, str],
) -> dict[str, int]:
    columns = _columns(connection, "chatRoomList")
    chat_column = _pick(columns, "chatId")
    title_column = _pick(columns, "chatRoomTitle")
    direct_column = _pick(columns, "directChatMemberId")
    members_column = _pick(columns, "titleDisplayMembers")
    selected = [column for column in (chat_column, title_column, direct_column, members_column) if column]
    if chat_column is None:
        raise AdapterError("backend_room_metadata_missing", "chatRoomList has no chatId column")

    by_title: dict[str, list[int]] = {}
    for row in connection.execute(f"SELECT {', '.join(selected)} FROM chatRoomList"):
        values = dict(zip(selected, row, strict=True))
        chat_id = _as_int(values.get(chat_column))
        if chat_id is None:
            continue
        title = _decode_text(values.get(title_column)).strip() if title_column else ""
        direct_id = _as_int(values.get(direct_column)) if direct_column else None
        member_names = _json_names(values.get(members_column)) if members_column else []
        if not title and direct_id is not None:
            title = contacts.get(direct_id, "")
        if not title and member_names:
            title = ", ".join(dict.fromkeys(member_names[:8]))
        if title:
            by_title.setdefault(title, []).append(chat_id)

    resolved: dict[str, int] = {}
    for room in rooms:
        candidates = list(dict.fromkeys(by_title.get(room.title, [])))
        if len(candidates) != 1:
            raise AdapterError(
                "backend_room_mapping_ambiguous",
                "An allowlisted room did not map to exactly one local chat database",
                room_id=room.room_id,
                candidate_count=len(candidates),
            )
        resolved[room.room_id] = candidates[0]
    return resolved


def infer_self_author_id(
    connection: sqlite3.Connection,
    chat_ids: tuple[int, ...],
) -> int:
    columns = _columns(connection, "chatMembers")
    chat_column = _pick(columns, "chatId")
    user_column = _pick(columns, "userId", "userid")
    active_column = _pick(columns, "isActive")
    if chat_column is None or user_column is None:
        raise AdapterError("backend_self_identity_missing", "chatMembers cannot identify the local user")

    def active_members(chat_id: int) -> set[int]:
        query = f"SELECT {user_column}"
        if active_column:
            query += f", {active_column}"
        query += f" FROM chatMembers WHERE {chat_column} = ?"
        members: set[int] = set()
        for row in connection.execute(query, (chat_id,)):
            user_id = _as_int(row[0])
            is_active = _as_int(row[1]) if active_column else 1
            if user_id is not None and is_active != 0:
                members.add(user_id)
        return members

    intersections: set[int] | None = None
    for chat_id in chat_ids:
        members = active_members(chat_id)
        if not members:
            raise AdapterError("backend_self_identity_missing", "A watched room has no active member set")
        intersections = members if intersections is None else intersections & members
    candidates = intersections or set()
    if len(candidates) == 1:
        return next(iter(candidates))

    room_columns = _columns(connection, "chatRoomList")
    room_chat_column = _pick(room_columns, "chatId")
    direct_column = _pick(room_columns, "directChatMemberId")
    structural_candidates: set[int] = set()
    if room_chat_column and direct_column:
        query = (
            f"SELECT {room_chat_column}, {direct_column} FROM chatRoomList "
            f"WHERE {direct_column} IS NOT NULL AND {direct_column} != 0"
        )
        for chat_id, direct_member_id in connection.execute(query):
            direct_id = _as_int(direct_member_id)
            room_chat_id = _as_int(chat_id)
            if direct_id is None or room_chat_id is None:
                continue
            possible_self = active_members(room_chat_id) - {direct_id}
            if len(possible_self) == 1:
                structural_candidates.update(possible_self)
    structural_matches = candidates & structural_candidates
    if len(structural_matches) == 1:
        return next(iter(structural_matches))

    raise AdapterError(
        "backend_self_identity_ambiguous",
        "The watched-room member intersection did not identify exactly one local user",
        candidate_count=len(candidates),
        structural_match_count=len(structural_matches),
    )


def read_chat_records(
    connection: sqlite3.Connection,
    *,
    after_log_id: int,
    self_author_id: int,
    limit: int,
    context_before: int = 3,
) -> tuple[tuple[BackendRecord, ...], int]:
    columns = _columns(connection, "chatLogs")
    id_column = _pick(columns, "id", "logId", "_id")
    author_column = _pick(columns, "authorId", "userId")
    type_column = _pick(columns, "type")
    message_column = _pick(columns, "message")
    time_column = _pick(columns, "sendAt", "createdAt", "sentAt", "created_at")
    required = (id_column, author_column, type_column, message_column, time_column)
    if any(column is None for column in required):
        raise AdapterError("backend_chat_schema_unsupported", "chatLogs is missing required columns")
    assert id_column and author_column and type_column and message_column and time_column

    maximum_row = connection.execute(f"SELECT MAX({id_column}) FROM chatLogs").fetchone()
    maximum = _as_int(maximum_row[0]) if maximum_row and maximum_row[0] is not None else 0
    if maximum < after_log_id:
        raise AdapterError(
            "backend_cursor_ahead",
            "The stored backend cursor is ahead of the current chat database",
        )

    selected = f"{id_column}, {author_column}, {type_column}, {message_column}, {time_column}"
    prior_rows = connection.execute(
        f"SELECT {selected} FROM chatLogs WHERE {id_column} <= ? AND {type_column} = 1 "
        f"ORDER BY {id_column} DESC LIMIT ?",
        (after_log_id, context_before),
    ).fetchall()
    new_rows = connection.execute(
        f"SELECT {selected} FROM chatLogs WHERE {id_column} > ? ORDER BY {id_column} LIMIT ?",
        (after_log_id, limit),
    ).fetchall()
    observed_through = after_log_id
    records: list[BackendRecord] = []
    for row in [*reversed(prior_rows), *new_rows]:
        log_id = _as_int(row[0])
        author_id = _as_int(row[1])
        message_type = _as_int(row[2])
        sent_at = _as_int(row[4]) or 0
        if log_id is None:
            continue
        if log_id > observed_through:
            observed_through = log_id
        if message_type != 1 or author_id is None:
            continue
        text = _decode_text(row[3]).strip()
        if not text:
            continue
        records.append(
            BackendRecord(
                log_id=log_id,
                sender=SELF_SENDER if author_id == self_author_id else OTHER_SENDER,
                sent_at=sent_at,
                text=text,
            )
        )
    return tuple(records), observed_through


def max_chat_log_id(connection: sqlite3.Connection) -> int:
    columns = _columns(connection, "chatLogs")
    id_column = _pick(columns, "id", "logId", "_id")
    if id_column is None:
        raise AdapterError("backend_chat_schema_unsupported", "chatLogs has no log id column")
    row = connection.execute(f"SELECT MAX({id_column}) FROM chatLogs").fetchone()
    return _as_int(row[0]) if row and row[0] is not None else 0


def source_signature(path: Path) -> tuple[int, int, int, int]:
    database = path.stat()
    wal_path = path.with_name(f"{path.name}-wal")
    try:
        wal = wal_path.stat()
        return database.st_size, database.st_mtime_ns, wal.st_size, wal.st_mtime_ns
    except FileNotFoundError:
        return database.st_size, database.st_mtime_ns, 0, 0


def _active_user_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Kakao/KakaoTalk/users"
    users = [path for path in root.iterdir() if path.is_dir() and (path / "appstate.dat").is_file()]
    if len(users) != 1:
        raise AdapterError(
            "backend_user_directory_ambiguous",
            "Exactly one active KakaoTalk user directory is required",
            directory_count=len(users),
        )
    return users[0]


def _target_paths(user_dir: Path) -> tuple[Path, ...]:
    paths = [user_dir / "TalkUserDB.edb", user_dir / "chat_data/chatListInfo.edb"]
    paths.extend(sorted((user_dir / "chat_data").glob("chatLogs_*.edb")))
    return tuple(path for path in paths if path.is_file())


def _legacy_source_hash(path: Path) -> str:
    return hashlib.sha256(path.name.encode("utf-8")).hexdigest()


def _source_hash(path: Path) -> str:
    stat = path.stat()
    if stat.st_ino <= 0:
        raise AdapterError(
            "backend_source_identity_unavailable",
            "The backend source has no stable filesystem identity",
        )
    identity = f"v2\0{path.name}\0{stat.st_dev}\0{stat.st_ino}".encode()
    return hashlib.sha256(identity).hexdigest()


class EphemeralV2Collector:
    def __init__(self, settings: Settings, adapter: Any) -> None:
        self.settings = settings
        self.adapter = adapter
        self.rooms: dict[str, EphemeralRoom] = {}
        self.self_author_id: int | None = None
        self.recovery_stats: RecoveryStats | None = None
        self.started_at = datetime.now(UTC).isoformat()
        self._last_signatures: dict[str, tuple[int, int, int, int]] = {}

    def close(self) -> None:
        for room in self.rooms.values():
            room.wipe()
        self.rooms.clear()
        self.self_author_id = None
        self._last_signatures.clear()

    def _selected_rooms(self) -> tuple[RoomConfig, ...]:
        backend = self.settings.backend_collector
        if backend is None or not backend.enabled:
            raise ConfigurationError("backend_disabled", "The RAM-only backend collector is disabled")
        selected: list[RoomConfig] = []
        for room_id in backend.room_ids:
            room = self.settings.rooms.get(room_id)
            if room is None or not room.enabled:
                raise ConfigurationError(
                    "backend_room_not_allowed",
                    "A backend room id is not enabled in the local allowlist",
                    room_id=room_id,
                )
            selected.append(room)
        if not selected:
            raise ConfigurationError("backend_rooms_empty", "No backend rooms were selected")
        return tuple(selected)

    def bootstrap(self) -> dict[str, Any]:
        if self.settings.send_enabled or self.settings.auto_reply_enabled:
            raise ConfigurationError(
                "backend_requires_manual_send",
                "The backend collector requires send and auto-reply to remain disabled",
            )
        selected = self._selected_rooms()
        self.close()
        user_dir = _active_user_dir()
        from .adapters.win32 import KAKAO_MAIN_TITLE, KAKAO_WINDOW_CLASS

        main = self.adapter.win32gui.FindWindow(KAKAO_WINDOW_CLASS, KAKAO_MAIN_TITLE)
        if not main:
            raise AdapterError("kakao_not_running", "KakaoTalk main window was not found")
        _, pid = self.adapter.win32process.GetWindowThreadProcessId(main)
        backend = self.settings.backend_collector
        assert backend is not None
        if _kakao_client_version(pid) != backend.expected_client_version:
            raise AdapterError(
                "backend_client_version_mismatch",
                "The installed KakaoTalk version has not passed the backend safety gate",
            )
        chatlist_path = user_dir / "chat_data/chatListInfo.edb"
        talkuser_path = user_dir / "TalkUserDB.edb"
        metadata_keys: dict[Path, bytearray] = {}
        runtime_rooms: dict[str, EphemeralRoom] = {}
        codec_count = 0
        candidate_count = 0
        matched_count = 0

        def add_stats(stats: RecoveryStats) -> None:
            nonlocal codec_count, candidate_count, matched_count
            codec_count += stats.codec_context_count
            candidate_count += stats.candidate_key_count
            matched_count += stats.matched_database_count

        try:
            with self.adapter.room_session(selected[0].title):
                metadata_keys, stats = recover_loaded_keys(pid, (chatlist_path,))
            add_stats(stats)
            chatlist_key = metadata_keys.get(chatlist_path)
            if chatlist_key is None:
                raise AdapterError(
                    "backend_metadata_key_missing",
                    "The chat-list metadata key was not loaded during bootstrap",
                )

            contacts: dict[int, str] = {}
            chatlist_connection = open_encrypted_snapshot(chatlist_path, chatlist_key)
            try:
                try:
                    mapping = resolve_room_chat_ids(chatlist_connection, selected, contacts)
                except AdapterError as exc:
                    if exc.code != "backend_room_mapping_ambiguous" or not talkuser_path.is_file():
                        raise
                    talkuser_keys, talkuser_stats = recover_loaded_keys(pid, (talkuser_path,))
                    add_stats(talkuser_stats)
                    try:
                        talkuser_key = talkuser_keys.get(talkuser_path)
                        if talkuser_key is None:
                            raise
                        contacts_connection = open_encrypted_snapshot(talkuser_path, talkuser_key)
                        try:
                            contacts = load_contacts(contacts_connection)
                        finally:
                            contacts_connection.close()
                    finally:
                        for key in talkuser_keys.values():
                            key[:] = b"\x00" * len(key)
                    mapping = resolve_room_chat_ids(chatlist_connection, selected, contacts)
                self_id = infer_self_author_id(
                    chatlist_connection,
                    tuple(mapping[room.room_id] for room in selected),
                )
            finally:
                chatlist_connection.close()

            for room in selected:
                source = user_dir / "chat_data" / f"chatLogs_{mapping[room.room_id]}.edb"
                with self.adapter.room_session(room.title):
                    room_keys, room_stats = recover_loaded_keys(pid, (source,))
                add_stats(room_stats)
                key = room_keys.pop(source, None)
                try:
                    if key is None:
                        raise AdapterError(
                            "backend_room_key_missing",
                            "A watched room key was not loaded during bootstrap",
                            room_id=room.room_id,
                        )
                    runtime_rooms[room.room_id] = EphemeralRoom(
                        room_id=room.room_id,
                        source_path=source,
                        source_hash=_source_hash(source),
                        key=key,
                        legacy_source_hash=_legacy_source_hash(source),
                    )
                finally:
                    for unused in room_keys.values():
                        unused[:] = b"\x00" * len(unused)

            self.rooms = runtime_rooms
            self.self_author_id = self_id
            self.recovery_stats = RecoveryStats(codec_count, candidate_count, matched_count)
            self._last_signatures = {
                room_id: source_signature(room.source_path) for room_id, room in self.rooms.items()
            }
            return {
                "ok": True,
                "mode": "ram_only_v2",
                "watched_room_count": len(self.rooms),
                "mapped_room_count": len(mapping),
                "self_identity_unique": True,
                "client_version_verified": True,
                "key_persisted": False,
                "plaintext_db_persisted": False,
                "message_content_emitted": False,
                "matched_database_count": matched_count,
            }
        except Exception:
            for room in runtime_rooms.values():
                room.wipe()
            self.close()
            raise
        finally:
            for key in metadata_keys.values():
                key[:] = b"\x00" * len(key)

    def baseline(self, room_id: str) -> tuple[str, int]:
        room = self.rooms[room_id]
        connection = open_encrypted_snapshot(room.source_path, room.key)
        try:
            return room.source_hash, max_chat_log_id(connection)
        finally:
            connection.close()

    def read_since(
        self,
        room_id: str,
        after_log_id: int,
        limit: int,
    ) -> tuple[tuple[BackendRecord, ...], int, tuple[int, int, int, int]]:
        if self.self_author_id is None:
            raise AdapterError("backend_not_ready", "The backend collector has no self identity")
        room = self.rooms[room_id]
        signature = source_signature(room.source_path)
        connection = open_encrypted_snapshot(room.source_path, room.key)
        try:
            records, observed_through = read_chat_records(
                connection,
                after_log_id=after_log_id,
                self_author_id=self.self_author_id,
                limit=limit,
            )
        finally:
            connection.close()
        self._last_signatures[room_id] = signature
        return records, observed_through, signature

    def changed_room_ids(self) -> tuple[str, ...]:
        changed = []
        for room_id, room in self.rooms.items():
            signature = source_signature(room.source_path)
            if self._last_signatures.get(room_id) != signature:
                changed.append(room_id)
        return tuple(changed)
