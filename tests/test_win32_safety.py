from threading import RLock
from types import SimpleNamespace

import pytest

from hermes_kakao_mcp.adapters.win32 import Win32KakaoAdapter, _utf16_code_unit_length
from hermes_kakao_mcp.errors import AdapterError


class FakeClipboard:
    def __init__(self, formats: list[int], text: str = "") -> None:
        self.formats = formats
        self.text = text
        self.read_count = 0

    def OpenClipboard(self) -> None:
        return None

    def CloseClipboard(self) -> None:
        return None

    def CountClipboardFormats(self) -> int:
        return len(self.formats)

    def EnumClipboardFormats(self, current: int) -> int:
        if not self.formats:
            return 0
        if current == 0:
            return self.formats[0]
        try:
            index = self.formats.index(current) + 1
        except ValueError:
            return 0
        return self.formats[index] if index < len(self.formats) else 0

    def IsClipboardFormatAvailable(self, format_id: int) -> bool:
        return format_id in self.formats

    def GetClipboardData(self, format_id: int) -> str:
        self.read_count += 1
        return self.text


def adapter_with_clipboard(clipboard: FakeClipboard) -> Win32KakaoAdapter:
    adapter = object.__new__(Win32KakaoAdapter)
    adapter.clipboard = clipboard
    adapter.win32con = SimpleNamespace(
        CF_TEXT=1,
        CF_OEMTEXT=7,
        CF_UNICODETEXT=13,
        CF_LOCALE=16,
    )
    return adapter


def test_windows_edit_length_uses_utf16_code_units_for_emoji_titles() -> None:
    assert _utf16_code_unit_length("일반 제목") == 5
    assert _utf16_code_unit_length("🌴") == 2
    assert _utf16_code_unit_length("방🌴") == 3


def test_plain_text_clipboard_can_be_snapshotted() -> None:
    clipboard = FakeClipboard([13, 16], "keep me")
    adapter = adapter_with_clipboard(clipboard)
    assert adapter._snapshot_text_clipboard() == (True, "keep me")


def test_complex_clipboard_fails_before_reading_payload() -> None:
    clipboard = FakeClipboard([13, 49161], "must not be read")
    adapter = adapter_with_clipboard(clipboard)
    with pytest.raises(AdapterError, match="left untouched"):
        adapter._snapshot_text_clipboard()
    assert clipboard.read_count == 0


def test_prepare_transcript_tail_presses_end_without_escape(monkeypatch) -> None:
    class FakeUser32:
        def __init__(self) -> None:
            self.events: list[tuple[int, int]] = []

        def keybd_event(self, virtual_key: int, scan_code: int, flags: int, extra: int) -> None:
            self.events.append((virtual_key, flags))

    adapter = object.__new__(Win32KakaoAdapter)
    adapter.user32 = FakeUser32()
    adapter.win32con = SimpleNamespace(VK_END=0x23)
    monkeypatch.setattr("hermes_kakao_mcp.adapters.win32.time.sleep", lambda _: None)

    adapter._prepare_transcript_tail()
    assert adapter.user32.events == [(0x23, 0), (0x23, 0x0002)]


def test_chat_search_switches_from_friends_tab_then_tabs_to_edit(monkeypatch) -> None:
    class FakeUser32:
        def __init__(self) -> None:
            self.focus = 1003
            self.events: list[tuple[int, int]] = []

        def GetForegroundWindow(self) -> int:
            return 1

        def GetFocus(self) -> int:
            return self.focus

        def SetFocus(self, hwnd: int) -> int:
            self.focus = hwnd
            return hwnd

        def keybd_event(self, virtual_key: int, scan_code: int, flags: int, extra: int) -> None:
            self.events.append((virtual_key, flags))

    adapter = object.__new__(Win32KakaoAdapter)
    panel_responses = iter(([], [1150]))
    edit_responses = iter(([], [], [100]))
    ctrl_calls: list[int] = []

    def visible_descendants(parent: int, *, class_name: str, control_id: int | None = None):
        assert parent == 1
        if control_id == 1150:
            return next(panel_responses)
        if control_id == 1003:
            return [1003]
        if class_name == "Edit":
            return next(edit_responses)
        return []

    adapter._visible_descendants = visible_descendants
    adapter._ctrl = ctrl_calls.append
    adapter.user32 = FakeUser32()
    adapter.win32gui = SimpleNamespace(
        GetDlgCtrlID=lambda hwnd: hwnd,
        GetParent=lambda hwnd: 1150,
        IsChild=lambda parent, child: False,
    )
    adapter.win32con = SimpleNamespace(VK_TAB=0x09)
    monkeypatch.setattr("hermes_kakao_mcp.adapters.win32.time.sleep", lambda _: None)

    assert adapter._ensure_chat_search_edit(1) == 100
    assert ctrl_calls == [0x09]
    assert adapter.user32.events == [(0x09, 0), (0x09, 0x0002)] * 2


def test_chat_search_reuses_already_open_unique_edit() -> None:
    adapter = object.__new__(Win32KakaoAdapter)
    ctrl_calls: list[int] = []
    key_events: list[tuple[int, int]] = []

    def visible_descendants(parent: int, *, class_name: str, control_id: int | None = None):
        if control_id == 1150:
            return [1150]
        if control_id == 1003:
            return [1003]
        if class_name == "Edit":
            return [100]
        return []

    adapter._visible_descendants = visible_descendants
    adapter._ctrl = ctrl_calls.append
    adapter.user32 = SimpleNamespace(
        GetForegroundWindow=lambda: 1,
        GetFocus=lambda: 1003,
        SetFocus=lambda hwnd: hwnd,
        keybd_event=lambda virtual_key, scan_code, flags, extra: key_events.append(
            (virtual_key, flags)
        ),
    )
    adapter.win32gui = SimpleNamespace(
        GetDlgCtrlID=lambda hwnd: hwnd,
        GetParent=lambda hwnd: 1150,
        IsChild=lambda parent, child: False,
    )
    adapter.win32con = SimpleNamespace(VK_TAB=0x09)

    assert adapter._ensure_chat_search_edit(1) == 100
    assert ctrl_calls == []
    assert key_events == []


def test_post_message_none_return_is_not_treated_as_failure(monkeypatch) -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.posts: list[int] = []

        def SendMessage(self, hwnd: int, message: int, wparam: int, lparam: object) -> int:
            return 0 if message == 0x000E else 1

        def PostMessage(self, hwnd: int, message: int, wparam: int, lparam: int) -> None:
            self.posts.append(message)

    adapter = object.__new__(Win32KakaoAdapter)
    adapter._lock = RLock()
    adapter._find_exact_room = lambda title: 10
    adapter._find_child = lambda parent, class_name: 20
    adapter.win32api = FakeApi()
    adapter.win32con = SimpleNamespace(
        WM_SETTEXT=0x000C,
        WM_KEYDOWN=0x0100,
        WM_KEYUP=0x0101,
        VK_RETURN=0x0D,
    )
    monkeypatch.setattr("hermes_kakao_mcp.adapters.win32.time.sleep", lambda _: None)

    adapter.send_text("allowed-room", "hello")
    assert adapter.win32api.posts == [0x0100, 0x0101]


def test_single_room_adoption_requires_exactly_one_window() -> None:
    class FakeGui:
        def __init__(self, titles: list[str]) -> None:
            self.titles = titles

        def EnumWindows(self, callback, extra) -> None:
            for index in range(len(self.titles)):
                callback(index, extra)

        def IsWindowVisible(self, hwnd: int) -> bool:
            return True

        def GetClassName(self, hwnd: int) -> str:
            return "EVA_Window_Dblclk"

        def GetWindowText(self, hwnd: int) -> str:
            return self.titles[hwnd]

    adapter = object.__new__(Win32KakaoAdapter)
    adapter.win32gui = FakeGui(["카카오톡", "private title"])
    assert adapter.single_open_room_title() == "private title"

    adapter.win32gui = FakeGui(["카카오톡", "one", "two"])
    with pytest.raises(AdapterError, match="Exactly one"):
        adapter.single_open_room_title()


def test_room_session_auto_opens_and_closes_only_its_own_window() -> None:
    adapter = object.__new__(Win32KakaoAdapter)
    adapter._lock = RLock()
    open_state = {"value": False}
    calls: list[tuple[str, object]] = []

    def find_exact(title: str) -> int:
        calls.append(("find", title))
        if not open_state["value"]:
            raise AdapterError("room_not_open", "closed")
        return 42

    def open_exact(title: str) -> tuple[int, int]:
        calls.append(("open", title))
        open_state["value"] = True
        return 42, 77

    def close_exact(hwnd: int, title: str) -> None:
        calls.append(("close", (hwnd, title)))
        open_state["value"] = False

    adapter._find_exact_room = find_exact
    adapter._open_exact_room = open_exact
    adapter._close_exact_room = close_exact
    adapter._restore_foreground = lambda hwnd: calls.append(("restore", hwnd))

    with adapter.room_session("allowed-room"):
        assert open_state["value"] is True

    assert calls == [
        ("find", "allowed-room"),
        ("open", "allowed-room"),
        ("close", (42, "allowed-room")),
        ("restore", 77),
    ]


def test_room_session_leaves_preexisting_window_open() -> None:
    adapter = object.__new__(Win32KakaoAdapter)
    adapter._lock = RLock()
    calls: list[tuple[str, object]] = []
    adapter._find_exact_room = lambda title: 42
    adapter._open_exact_room = lambda title: calls.append(("open", title))
    adapter._close_exact_room = lambda hwnd, title: calls.append(("close", (hwnd, title)))

    with adapter.room_session("allowed-room"):
        pass

    assert calls == []


def test_room_discovery_collects_overlapping_pages_and_stops_at_bottom() -> None:
    adapter = object.__new__(Win32KakaoAdapter)
    responses = iter(
        [
            *(f"room-{index}" for index in range(10)),
            *(f"room-{index}" for index in range(1, 11)),
            "room-11",
            "room-12",
            *("room-12" for _ in range(5)),
        ]
    )
    wheel_calls: list[int] = []
    adapter._open_room_list_row = lambda room_list, width, y: next(responses)
    adapter._wheel_room_list = lambda room_list, direction: wheel_calls.append(direction)

    titles = adapter._collect_discovered_room_titles(
        room_list=100,
        width=800,
        y_positions=[36 + index * 72 for index in range(10)],
        max_pages=20,
    )

    assert set(titles) == {f"room-{index}" for index in range(13)}
    assert wheel_calls == [-1] * 8
