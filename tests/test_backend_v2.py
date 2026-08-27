from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import make_settings

import hermes_kakao_mcp.backend_v2 as backend_v2
from hermes_kakao_mcp.backend_v2 import (
    OTHER_SENDER,
    SELF_SENDER,
    infer_self_author_id,
    load_contacts,
    max_chat_log_id,
    read_chat_records,
    resolve_room_chat_ids,
)
from hermes_kakao_mcp.config import BackendCollectorConfig, RoomConfig
from hermes_kakao_mcp.errors import AdapterError


def metadata_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE talkUser(userid INTEGER PRIMARY KEY, nickName TEXT, friendNickName TEXT);
        INSERT INTO talkUser VALUES (20, 'direct', '직접 상대');
        CREATE TABLE chatRoomList(
            chatId INTEGER PRIMARY KEY,
            chatRoomTitle TEXT,
            directChatMemberId INTEGER,
            titleDisplayMembers TEXT
        );
        INSERT INTO chatRoomList VALUES (101, '지정 그룹', NULL, '[]');
        INSERT INTO chatRoomList VALUES (102, '', 20, '[]');
        CREATE TABLE chatMembers(chatId INTEGER, userId INTEGER, isActive INTEGER);
        INSERT INTO chatMembers VALUES (101, 1, 1), (101, 2, 1);
        INSERT INTO chatMembers VALUES (102, 1, 1), (102, 20, 1);
        """
    )
    return connection


def test_metadata_maps_exact_titles_and_infers_unique_self_member() -> None:
    connection = metadata_connection()
    try:
        rooms = (
            RoomConfig("room-group", "지정 그룹"),
            RoomConfig("room-direct", "직접 상대"),
        )
        contacts = load_contacts(connection)
        mapping = resolve_room_chat_ids(connection, rooms, contacts)
        self_author_id = infer_self_author_id(connection, tuple(mapping.values()))
    finally:
        connection.close()

    assert set(mapping) == {"room-group", "room-direct"}
    assert mapping["room-group"] == 101
    assert mapping["room-direct"] == 102
    assert self_author_id == 1


def test_self_identity_fails_closed_when_member_intersection_is_ambiguous() -> None:
    connection = metadata_connection()
    connection.execute("INSERT INTO chatMembers VALUES (102, 2, 1)")
    try:
        with pytest.raises(AdapterError, match="exactly one"):
            infer_self_author_id(connection, (101, 102))
    finally:
        connection.close()


def test_self_identity_uses_direct_room_structure_to_narrow_common_members() -> None:
    connection = metadata_connection()
    connection.executescript(
        """
        INSERT INTO chatRoomList VALUES (103, '다른 그룹', NULL, '[]');
        INSERT INTO chatMembers VALUES (103, 1, 1), (103, 2, 1);
        """
    )
    try:
        self_author_id = infer_self_author_id(connection, (101, 103))
    finally:
        connection.close()

    assert self_author_id == 1


def test_chat_reader_advances_over_non_text_and_labels_only_roles() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE chatLogs(
            id INTEGER PRIMARY KEY,
            authorId INTEGER,
            type INTEGER,
            message TEXT,
            sendAt INTEGER
        );
        INSERT INTO chatLogs VALUES (1, 2, 1, '이전 문맥', 100);
        INSERT INTO chatLogs VALUES (2, 1, 1, '내 메시지', 101);
        INSERT INTO chatLogs VALUES (3, 2, 1, '9월 3일 회의', 102);
        INSERT INTO chatLogs VALUES (4, 2, 2, 'attachment', 103);
        """
    )
    try:
        records, observed_through = read_chat_records(
            connection,
            after_log_id=1,
            self_author_id=1,
            limit=20,
            context_before=1,
        )
        maximum = max_chat_log_id(connection)
    finally:
        connection.close()

    assert observed_through == 4
    assert maximum == 4
    assert [(record.log_id, record.sender) for record in records] == [
        (1, OTHER_SENDER),
        (2, SELF_SENDER),
        (3, OTHER_SENDER),
    ]
    assert all(record.sender in {SELF_SENDER, OTHER_SENDER} for record in records)


def test_bootstrap_opens_selected_rooms_sequentially(monkeypatch, tmp_path) -> None:
    user_dir = tmp_path / "user"
    chat_data = user_dir / "chat_data"
    chat_data.mkdir(parents=True)
    (user_dir / "appstate.dat").write_bytes(b"v2")
    for name in ("chatListInfo.edb", "chatLogs_101.edb", "chatLogs_102.edb"):
        (chat_data / name).write_bytes(b"x" * 4096)

    rooms = {
        "room-one": RoomConfig("room-one", "첫 방", schedule_watch_enabled=True),
        "room-two": RoomConfig("room-two", "둘 방", schedule_watch_enabled=True),
    }
    settings = replace(
        make_settings(tmp_path),
        adapter="win32",
        rooms=rooms,
        backend_collector=BackendCollectorConfig(
            True, "ram_only_v2", tuple(rooms), 200, 30.0
        ),
    )

    class Adapter:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.win32gui = SimpleNamespace(FindWindow=lambda *_: 1)
            self.win32process = SimpleNamespace(GetWindowThreadProcessId=lambda *_: (1, 99))

        @contextmanager
        def room_session(self, _title):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                yield
            finally:
                self.active -= 1

    def fake_recover(_pid, targets):
        return (
            {path: bytearray(b"k" * 32) for path in targets},
            backend_v2.RecoveryStats(1, 1, len(targets)),
        )

    def fake_open(path, _key):
        assert path.name == "chatListInfo.edb"
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE chatRoomList(
                chatId INTEGER, chatRoomTitle TEXT,
                directChatMemberId INTEGER, titleDisplayMembers TEXT
            );
            INSERT INTO chatRoomList VALUES (101, '첫 방', NULL, '[]');
            INSERT INTO chatRoomList VALUES (102, '둘 방', NULL, '[]');
            CREATE TABLE chatMembers(chatId INTEGER, userId INTEGER, isActive INTEGER);
            INSERT INTO chatMembers VALUES (101, 1, 1), (101, 2, 1);
            INSERT INTO chatMembers VALUES (102, 1, 1), (102, 3, 1);
            """
        )
        return connection

    monkeypatch.setattr(backend_v2, "_active_user_dir", lambda: user_dir)
    monkeypatch.setattr(backend_v2, "_kakao_client_version", lambda _pid: "26.7.1.5263")
    monkeypatch.setattr(backend_v2, "recover_loaded_keys", fake_recover)
    monkeypatch.setattr(backend_v2, "open_encrypted_snapshot", fake_open)
    adapter = Adapter()
    collector = backend_v2.EphemeralV2Collector(settings, adapter)

    result = collector.bootstrap()

    assert result["watched_room_count"] == 2
    assert result["client_version_verified"] is True
    assert collector.self_author_id == 1
    assert adapter.max_active == 1
    assert adapter.active == 0
    collector.close()


def test_bootstrap_rejects_unverified_client_version_before_opening_room(
    monkeypatch, tmp_path
) -> None:
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "appstate.dat").write_bytes(b"v2")
    rooms = {"room-one": RoomConfig("room-one", "private", schedule_watch_enabled=True)}
    settings = replace(
        make_settings(tmp_path),
        adapter="win32",
        rooms=rooms,
        backend_collector=BackendCollectorConfig(
            True, "ram_only_v2", tuple(rooms), 200, 30.0
        ),
    )

    class Adapter:
        win32gui = SimpleNamespace(FindWindow=lambda *_: 1)
        win32process = SimpleNamespace(GetWindowThreadProcessId=lambda *_: (1, 99))

        @contextmanager
        def room_session(self, _title):
            raise AssertionError("version mismatch must stop before room access")
            yield

    monkeypatch.setattr(backend_v2, "_active_user_dir", lambda: user_dir)
    monkeypatch.setattr(backend_v2, "_kakao_client_version", lambda _pid: "99.0.0.0")

    with pytest.raises(AdapterError) as raised:
        backend_v2.EphemeralV2Collector(settings, Adapter()).bootstrap()

    assert raised.value.code == "backend_client_version_mismatch"


def test_source_identity_changes_when_database_file_is_replaced(tmp_path) -> None:
    source = tmp_path / "chatLogs_123.edb"
    replacement = tmp_path / "replacement.edb"
    source.write_bytes(b"first")
    replacement.write_bytes(b"second")
    original_hash = backend_v2._source_hash(source)

    replacement.replace(source)

    assert backend_v2._source_hash(source) != original_hash
