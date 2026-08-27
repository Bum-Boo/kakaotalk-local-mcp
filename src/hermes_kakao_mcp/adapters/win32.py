"""Fail-closed Win32 adapter.

Window discovery and clipboard-based transcript access were inspired by
https://github.com/kronenz/kakaotalk-mcp. This module is an original,
narrower implementation; see THIRD_PARTY_NOTICES.md for the reviewed revision.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from ..errors import AdapterError
from .base import KakaoAdapter

KAKAO_WINDOW_CLASS = "EVA_Window_Dblclk"
KAKAO_MAIN_TITLE = "카카오톡"
CHAT_LIST_CLASS = "EVA_VH_ListControl_Dblclk"
CHAT_EDIT_CLASS = "RICHEDIT50W"
MAIN_SEARCH_EDIT_CONTROL_ID = 100
MAIN_CHAT_LIST_CONTROL_ID = 1003
MAIN_CHAT_PANEL_CONTROL_ID = 1150
ROOM_OPEN_TIMEOUT_SECONDS = 3.0
DISCOVERY_ROW_HEIGHT = 72
DISCOVERY_FIRST_ROW_CENTER = 36
DISCOVERY_MAX_PAGES = 240


def _utf16_code_unit_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


class Win32KakaoAdapter(KakaoAdapter):
    """Narrow Win32 adapter.

    It never lists room titles, accesses KakaoTalk databases, or touches network
    credentials. A caller-supplied exact allowlisted title may be searched in the
    KakaoTalk main window, and the resulting top-level title must match before any
    transcript is read.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise AdapterError("windows_required", "The Win32 adapter must run in Windows")
        try:
            import win32api
            import win32clipboard
            import win32con
            import win32gui
            import win32process
        except ImportError as exc:
            raise AdapterError("pywin32_missing", "pywin32 is required on Windows") from exc

        self.win32api = win32api
        self.clipboard = win32clipboard
        self.win32con = win32con
        self.win32gui = win32gui
        self.win32process = win32process
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self._lock = threading.RLock()

    def health(self) -> dict[str, Any]:
        main = self.win32gui.FindWindow(KAKAO_WINDOW_CLASS, KAKAO_MAIN_TITLE)
        return {
            "running": bool(main),
            "adapter": "win32",
            "open_chat_count": len(self._open_chat_windows()),
        }

    def _open_chat_windows(self) -> list[tuple[int, str]]:
        windows: list[tuple[int, str]] = []

        def callback(hwnd: int, _: object) -> bool:
            if not self.win32gui.IsWindowVisible(hwnd):
                return True
            if self.win32gui.GetClassName(hwnd) != KAKAO_WINDOW_CLASS:
                return True
            title = self.win32gui.GetWindowText(hwnd)
            if title and title != KAKAO_MAIN_TITLE:
                windows.append((hwnd, title))
            return True

        self.win32gui.EnumWindows(callback, None)
        return windows

    def single_open_room_title(self) -> str:
        """Return one local title for setup without exposing a room-list API."""
        windows = self._open_chat_windows()
        if len(windows) != 1:
            raise AdapterError(
                "single_room_required",
                "Exactly one KakaoTalk chat window must be open for safe local adoption",
                open_chat_count=len(windows),
            )
        return windows[0][1]

    def _find_exact_room(self, exact_title: str) -> int:
        matches: list[int] = []

        def callback(hwnd: int, _: object) -> bool:
            if (
                self.win32gui.IsWindowVisible(hwnd)
                and self.win32gui.GetClassName(hwnd) == KAKAO_WINDOW_CLASS
                and self.win32gui.GetWindowText(hwnd) == exact_title
            ):
                matches.append(hwnd)
            return True

        self.win32gui.EnumWindows(callback, None)
        if len(matches) != 1:
            code = "room_not_open" if not matches else "ambiguous_room"
            raise AdapterError(code, "Exactly one already-open allowed room window is required")
        return matches[0]

    def _visible_descendants(
        self,
        parent: int,
        *,
        class_name: str,
        control_id: int | None = None,
    ) -> list[int]:
        matches: list[int] = []

        def callback(hwnd: int, _: object) -> bool:
            if not self.win32gui.IsWindowVisible(hwnd):
                return True
            if self.win32gui.GetClassName(hwnd) != class_name:
                return True
            if control_id is not None and self.win32gui.GetDlgCtrlID(hwnd) != control_id:
                return True
            matches.append(hwnd)
            return True

        self.win32gui.EnumChildWindows(parent, callback, None)
        return matches

    def _close_unapproved_new_windows(
        self,
        before_handles: set[int],
        exact_title: str,
    ) -> int:
        unexpected = [
            hwnd
            for hwnd, title in self._open_chat_windows()
            if hwnd not in before_handles and title != exact_title
        ]
        for hwnd in unexpected:
            self.win32gui.PostMessage(hwnd, self.win32con.WM_CLOSE, 0, 0)
        return len(unexpected)

    def _ensure_chat_search_edit(self, main: int) -> int:
        if self.user32.GetForegroundWindow() != main:
            raise AdapterError(
                "room_search_foreground_unavailable",
                "KakaoTalk main window was not foreground for local room search",
            )

        def focus_visible_list() -> None:
            room_lists = self._visible_descendants(
                main,
                class_name=CHAT_LIST_CLASS,
                control_id=MAIN_CHAT_LIST_CONTROL_ID,
            )
            if len(room_lists) != 1:
                raise AdapterError(
                    "room_search_focus_unavailable",
                    "KakaoTalk visible list was not uniquely available for local room search",
                )
            room_list = room_lists[0]
            focused = self.user32.GetFocus()
            if focused != room_list and not (
                focused and self.win32gui.IsChild(room_list, focused)
            ):
                self.user32.SetFocus(room_list)
                focused = self.user32.GetFocus()
            if focused != room_list and not (
                focused and self.win32gui.IsChild(room_list, focused)
            ):
                raise AdapterError(
                    "room_search_focus_unavailable",
                    "KakaoTalk visible list could not be focused for local room search",
                )

        focus_visible_list()

        chat_panels = self._visible_descendants(
            main,
            class_name="EVA_Window",
            control_id=MAIN_CHAT_PANEL_CONTROL_ID,
        )
        if not chat_panels:
            self._ctrl(self.win32con.VK_TAB)
            for _ in range(10):
                time.sleep(0.05)
                chat_panels = self._visible_descendants(
                    main,
                    class_name="EVA_Window",
                    control_id=MAIN_CHAT_PANEL_CONTROL_ID,
                )
                if chat_panels:
                    break
        if len(chat_panels) != 1:
            raise AdapterError(
                "room_chat_tab_unavailable",
                "KakaoTalk chat-list tab was not uniquely available",
            )
        chat_panel = chat_panels[0]

        def visible_search_edits() -> list[int]:
            return [
                hwnd
                for hwnd in self._visible_descendants(main, class_name="Edit")
                if self.win32gui.GetDlgCtrlID(hwnd) == MAIN_SEARCH_EDIT_CONTROL_ID
                and self.win32gui.GetParent(hwnd) == chat_panel
            ]

        search_edits = visible_search_edits()
        if not search_edits:
            self.user32.SetFocus(chat_panel)
            focused = self.user32.GetFocus()
            if focused != chat_panel and not (
                focused and self.win32gui.IsChild(chat_panel, focused)
            ):
                raise AdapterError(
                    "room_search_focus_unavailable",
                    "KakaoTalk chat panel could not be focused for local room search",
                )
            key_up = 0x0002
            for _ in range(4):
                self.user32.keybd_event(self.win32con.VK_TAB, 0, 0, 0)
                self.user32.keybd_event(self.win32con.VK_TAB, 0, key_up, 0)
                time.sleep(0.15)
                search_edits = visible_search_edits()
                if search_edits:
                    break
        if len(search_edits) != 1:
            raise AdapterError(
                "room_search_unavailable",
                "KakaoTalk chat search edit was not uniquely available",
            )
        return search_edits[0]

    def _open_exact_room(self, exact_title: str) -> tuple[int, int]:
        """Open one caller-approved exact title through KakaoTalk's visible UI."""
        main = self.win32gui.FindWindow(KAKAO_WINDOW_CLASS, KAKAO_MAIN_TITLE)
        if not main:
            raise AdapterError("kakao_not_running", "KakaoTalk main window was not found")

        main_was_visible = bool(self.win32gui.IsWindowVisible(main))
        previous = self.user32.GetForegroundWindow()
        current_thread = self.kernel32.GetCurrentThreadId()
        target_thread, _ = self.win32process.GetWindowThreadProcessId(main)
        foreground_thread = 0
        if previous:
            foreground_thread, _ = self.win32process.GetWindowThreadProcessId(previous)
        attached_foreground = False
        attached_target = False
        edit = 0
        edit_parent = 0
        change_command = 0
        opened_room = 0
        try:
            if not main_was_visible:
                self.user32.ShowWindow(main, 4)  # SW_SHOWNOACTIVATE
                time.sleep(0.15)

            if foreground_thread and foreground_thread != current_thread:
                attached_foreground = bool(
                    self.user32.AttachThreadInput(current_thread, foreground_thread, True)
                )
            if target_thread != current_thread:
                attached_target = bool(
                    self.user32.AttachThreadInput(current_thread, target_thread, True)
                )
            self.user32.ShowWindow(main, 5)  # SW_SHOW
            self.user32.SetForegroundWindow(main)

            edit = self._ensure_chat_search_edit(main)
            self.win32gui.SendMessage(
                edit,
                self.win32con.WM_SETTEXT,
                0,
                exact_title,
            )
            accepted_length = int(
                self.win32gui.SendMessage(
                    edit,
                    self.win32con.WM_GETTEXTLENGTH,
                    0,
                    0,
                )
            )
            if accepted_length != _utf16_code_unit_length(exact_title):
                raise AdapterError(
                    "room_search_unavailable",
                    "KakaoTalk chat search could not enter an exact local title",
                )
            edit_parent = self.win32gui.GetParent(edit)
            change_command = self.win32api.MAKELONG(
                self.win32gui.GetDlgCtrlID(edit),
                self.win32con.EN_CHANGE,
            )
            self.win32gui.SendMessage(
                edit_parent,
                self.win32con.WM_COMMAND,
                change_command,
                edit,
            )
            time.sleep(0.6)

            result_lists = self._visible_descendants(
                main,
                class_name=CHAT_LIST_CLASS,
                control_id=MAIN_CHAT_LIST_CONTROL_ID,
            )
            if len(result_lists) != 1:
                raise AdapterError(
                    "room_search_unavailable",
                    "KakaoTalk filtered chat result list was not uniquely available",
                )
            result_list = result_lists[0]
            _, _, list_right, list_bottom = self.win32gui.GetClientRect(result_list)
            y_positions = list(
                range(
                    DISCOVERY_FIRST_ROW_CENTER,
                    list_bottom,
                    DISCOVERY_ROW_HEIGHT,
                )
            )
            for y in y_positions:
                candidate_before = {hwnd for hwnd, _ in self._open_chat_windows()}
                result_click = self.win32api.MAKELONG(max(1, list_right // 2), y)
                self.win32gui.SendMessage(
                    result_list,
                    self.win32con.WM_LBUTTONDOWN,
                    self.win32con.MK_LBUTTON,
                    result_click,
                )
                self.win32gui.SendMessage(
                    result_list,
                    self.win32con.WM_LBUTTONUP,
                    0,
                    result_click,
                )
                self.win32gui.SendMessage(
                    result_list,
                    self.win32con.WM_LBUTTONDBLCLK,
                    self.win32con.MK_LBUTTON,
                    result_click,
                )
                self.win32gui.SendMessage(
                    result_list,
                    self.win32con.WM_LBUTTONUP,
                    0,
                    result_click,
                )
                deadline = time.monotonic() + 1.2
                new_rows: list[tuple[int, str]] = []
                while time.monotonic() < deadline:
                    new_rows = [
                        (hwnd, title)
                        for hwnd, title in self._open_chat_windows()
                        if hwnd not in candidate_before
                    ]
                    if new_rows:
                        break
                    time.sleep(0.03)
                if not new_rows:
                    continue
                if len(new_rows) != 1:
                    for hwnd, title in new_rows:
                        self._close_exact_room(hwnd, title)
                    raise AdapterError(
                        "ambiguous_room",
                        "One filtered room row opened more than one window",
                    )
                hwnd, title = new_rows[0]
                if title == exact_title:
                    time.sleep(0.8)
                    opened_room = hwnd
                    return opened_room, previous
                self._close_exact_room(hwnd, title)
                self.user32.ShowWindow(main, 5)  # SW_SHOW
            raise AdapterError(
                "room_open_failed",
                "No visible filtered result matched the exact local room title",
                checked_rows=len(y_positions),
            )
        finally:
            if edit and self.win32gui.IsWindow(edit):
                self.win32gui.SendMessage(edit, self.win32con.WM_SETTEXT, 0, "")
                if edit_parent and change_command:
                    self.win32gui.SendMessage(
                        edit_parent,
                        self.win32con.WM_COMMAND,
                        change_command,
                        edit,
                    )
            if attached_target:
                self.user32.AttachThreadInput(current_thread, target_thread, False)
            if attached_foreground:
                self.user32.AttachThreadInput(current_thread, foreground_thread, False)
            if not main_was_visible and self.win32gui.IsWindow(main):
                self.user32.ShowWindow(main, 0)  # SW_HIDE
            if not opened_room:
                self._restore_foreground(previous)

    def _close_exact_room(self, hwnd: int, exact_title: str) -> None:
        if not self.win32gui.IsWindow(hwnd):
            return
        if (
            self.win32gui.GetClassName(hwnd) != KAKAO_WINDOW_CLASS
            or self.win32gui.GetWindowText(hwnd) != exact_title
        ):
            raise AdapterError(
                "room_cleanup_mismatch",
                "Refused to close a window that no longer matched the auto-opened room",
            )
        self.win32gui.PostMessage(hwnd, self.win32con.WM_CLOSE, 0, 0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not self.win32gui.IsWindow(hwnd) or not self.win32gui.IsWindowVisible(hwnd):
                return
            time.sleep(0.05)
        raise AdapterError(
            "room_cleanup_failed",
            "The auto-opened KakaoTalk room did not close after the read session",
        )

    def _wheel_room_list(self, room_list: int, direction: int) -> None:
        left, top, right, bottom = self.win32gui.GetWindowRect(room_list)
        point = self.win32api.MAKELONG((left + right) // 2, (top + bottom) // 2)
        wparam = self.win32api.MAKELONG(
            0,
            (direction * self.win32con.WHEEL_DELTA) & 0xFFFF,
        )
        self.win32gui.SendMessage(room_list, self.win32con.WM_MOUSEWHEEL, wparam, point)
        time.sleep(0.05)

    def _open_room_list_row(self, room_list: int, width: int, y: int) -> str | None:
        before_handles = {hwnd for hwnd, _ in self._open_chat_windows()}
        point = self.win32api.MAKELONG(max(1, width // 2), y)
        self.win32gui.SendMessage(
            room_list,
            self.win32con.WM_LBUTTONDOWN,
            self.win32con.MK_LBUTTON,
            point,
        )
        self.win32gui.SendMessage(room_list, self.win32con.WM_LBUTTONUP, 0, point)
        self.win32gui.SendMessage(
            room_list,
            self.win32con.WM_LBUTTONDBLCLK,
            self.win32con.MK_LBUTTON,
            point,
        )
        self.win32gui.SendMessage(room_list, self.win32con.WM_LBUTTONUP, 0, point)
        deadline = time.monotonic() + 1.2
        new_rows: list[tuple[int, str]] = []
        while time.monotonic() < deadline:
            new_rows = [
                (hwnd, title)
                for hwnd, title in self._open_chat_windows()
                if hwnd not in before_handles
            ]
            if new_rows:
                break
            time.sleep(0.03)
        if not new_rows:
            return None
        if len(new_rows) != 1:
            for hwnd, title in new_rows:
                self._close_exact_room(hwnd, title)
            raise AdapterError(
                "room_discovery_ambiguous",
                "One room-list row opened more than one KakaoTalk window",
            )
        hwnd, title = new_rows[0]
        try:
            return title
        finally:
            self._close_exact_room(hwnd, title)

    def _collect_discovered_room_titles(
        self,
        room_list: int,
        width: int,
        y_positions: list[int],
        *,
        max_pages: int = DISCOVERY_MAX_PAGES,
    ) -> list[str]:
        if len(y_positions) < 2:
            raise AdapterError(
                "room_discovery_layout_changed",
                "KakaoTalk room-list geometry did not expose enough row positions",
            )

        def scan_page() -> set[str]:
            return {
                title
                for y in y_positions
                if (title := self._open_room_list_row(room_list, width, y)) is not None
            }

        titles = scan_page()
        if not titles:
            raise AdapterError("room_discovery_empty", "No KakaoTalk room rows could be opened")
        self._wheel_room_list(room_list, -1)
        second_page = scan_page()
        overlap = len(titles & second_page)
        if second_page and overlap < max(1, len(y_positions) - 2):
            raise AdapterError(
                "room_discovery_gap",
                "KakaoTalk room-list scroll moved too far to prove complete discovery",
            )
        titles.update(second_page)
        stagnant_pages = 0
        page_count = 2
        while page_count < max_pages:
            self._wheel_room_list(room_list, -1)
            title = self._open_room_list_row(room_list, width, y_positions[-1])
            page_count += 1
            if title is None or title in titles:
                stagnant_pages += 1
            else:
                titles.add(title)
                stagnant_pages = 0
            if stagnant_pages >= 5:
                return sorted(titles, key=str.casefold)
        raise AdapterError(
            "room_discovery_incomplete",
            "KakaoTalk room discovery reached its bounded page limit",
            max_pages=max_pages,
        )

    def discover_room_titles(self) -> list[str]:
        """Discover all visible chat-list rooms without reading their messages."""
        with self._lock:
            if self._open_chat_windows():
                raise AdapterError(
                    "room_discovery_requires_closed_rooms",
                    "Close all separate KakaoTalk room windows before discovery",
                )
            main = self.win32gui.FindWindow(KAKAO_WINDOW_CLASS, KAKAO_MAIN_TITLE)
            if not main:
                raise AdapterError("kakao_not_running", "KakaoTalk main window was not found")
            previous = self.user32.GetForegroundWindow()
            main_was_visible = bool(self.win32gui.IsWindowVisible(main))
            main_was_iconic = bool(self.win32gui.IsIconic(main))
            if not main_was_visible or main_was_iconic:
                self.user32.ShowWindow(main, 4)  # SW_SHOWNOACTIVATE
                time.sleep(0.2)
            try:
                panels = self._visible_descendants(
                    main,
                    class_name="EVA_Window",
                    control_id=MAIN_CHAT_PANEL_CONTROL_ID,
                )
                room_lists = self._visible_descendants(
                    main,
                    class_name=CHAT_LIST_CLASS,
                    control_id=MAIN_CHAT_LIST_CONTROL_ID,
                )
                if len(panels) != 1 or len(room_lists) != 1:
                    raise AdapterError(
                        "room_discovery_unavailable",
                        "KakaoTalk must be on its chat-list tab for room discovery",
                    )
                room_list = room_lists[0]
                _, _, width, height = self.win32gui.GetClientRect(room_list)
                y_positions = list(
                    range(
                        DISCOVERY_FIRST_ROW_CENTER,
                        height,
                        DISCOVERY_ROW_HEIGHT,
                    )
                )
                for _ in range(DISCOVERY_MAX_PAGES + 10):
                    self._wheel_room_list(room_list, 1)
                return self._collect_discovered_room_titles(
                    room_list,
                    width,
                    y_positions,
                )
            finally:
                for _ in range(DISCOVERY_MAX_PAGES + 10):
                    if 'room_list' in locals():
                        self._wheel_room_list(room_list, 1)
                if main_was_iconic and self.win32gui.IsWindow(main):
                    self.user32.ShowWindow(main, 6)  # SW_MINIMIZE
                elif not main_was_visible and self.win32gui.IsWindow(main):
                    self.user32.ShowWindow(main, 0)  # SW_HIDE
                self._restore_foreground(previous)

    @contextmanager
    def room_session(self, exact_room_title: str) -> Iterator[None]:
        """Open a missing allowed room once, then close only that auto-opened window."""
        with self._lock:
            opened_here = False
            room = 0
            previous_foreground = 0
            try:
                try:
                    room = self._find_exact_room(exact_room_title)
                except AdapterError as exc:
                    if exc.code != "room_not_open":
                        raise
                    room, previous_foreground = self._open_exact_room(exact_room_title)
                    opened_here = True
                yield
            finally:
                if opened_here and room:
                    cleanup_during_error = sys.exc_info()[0] is not None
                    try:
                        self._close_exact_room(room, exact_room_title)
                    except AdapterError:
                        if not cleanup_during_error:
                            raise
                    finally:
                        if previous_foreground:
                            self._restore_foreground(previous_foreground)

    def _find_child(self, parent: int, class_name: str) -> int:
        matches: list[int] = []

        def callback(hwnd: int, _: object) -> bool:
            if self.win32gui.GetClassName(hwnd) == class_name:
                matches.append(hwnd)
                return False
            return True

        with suppress(Exception):
            self.win32gui.EnumChildWindows(parent, callback, None)
        if not matches:
            raise AdapterError("control_not_found", "Required KakaoTalk control was not found")
        return matches[0]

    def _activate_and_focus(self, window: int, control: int) -> None:
        current_thread = self.kernel32.GetCurrentThreadId()
        target_thread, _ = self.win32process.GetWindowThreadProcessId(window)
        attached = bool(self.user32.AttachThreadInput(current_thread, target_thread, True))
        try:
            self.user32.ShowWindow(window, 9)
            self.user32.SetForegroundWindow(window)
            self.user32.SetFocus(control)
            if self.user32.GetFocus() != control:
                raise AdapterError("focus_failed", "Could not focus the KakaoTalk control")
        finally:
            if attached:
                self.user32.AttachThreadInput(current_thread, target_thread, False)

    def _restore_foreground(self, previous: int) -> None:
        if not previous or not self.win32gui.IsWindow(previous):
            return
        try:
            self.user32.ShowWindow(previous, 5)
            self.user32.SetForegroundWindow(previous)
        except Exception:
            pass

    def _snapshot_text_clipboard(self) -> tuple[bool, str]:
        for attempt in range(10):
            try:
                self.clipboard.OpenClipboard()
                break
            except Exception as exc:
                if attempt == 9:
                    raise AdapterError(
                        "clipboard_busy",
                        "Could not acquire the Windows clipboard for a safe snapshot",
                    ) from exc
                time.sleep(0.05)
        try:
            count = self.clipboard.CountClipboardFormats()
            formats: set[int] = set()
            current_format = 0
            while True:
                current_format = self.clipboard.EnumClipboardFormats(current_format)
                if not current_format:
                    break
                formats.add(current_format)
            safe_text_formats = {
                self.win32con.CF_TEXT,
                self.win32con.CF_OEMTEXT,
                self.win32con.CF_UNICODETEXT,
                self.win32con.CF_LOCALE,
            }
            if formats - safe_text_formats:
                raise AdapterError(
                    "clipboard_complex",
                    "Clipboard contains rich or non-text data and was left untouched",
                )
            has_text = bool(self.clipboard.IsClipboardFormatAvailable(self.win32con.CF_UNICODETEXT))
            if count and not has_text:
                raise AdapterError(
                    "clipboard_non_text",
                    "Clipboard contains non-text data; copy or clear text before reading KakaoTalk",
                )
            value = self.clipboard.GetClipboardData(self.win32con.CF_UNICODETEXT) if has_text else ""
            return bool(count), value or ""
        finally:
            self.clipboard.CloseClipboard()

    def _set_text_clipboard(self, value: str | None) -> None:
        for _ in range(10):
            try:
                self.clipboard.OpenClipboard()
                try:
                    self.clipboard.EmptyClipboard()
                    if value is not None:
                        self.clipboard.SetClipboardText(value, self.win32con.CF_UNICODETEXT)
                finally:
                    self.clipboard.CloseClipboard()
                return
            except Exception:
                time.sleep(0.05)
        raise AdapterError("clipboard_busy", "Could not acquire the Windows clipboard")

    def _read_text_clipboard(self) -> str:
        for _ in range(10):
            try:
                self.clipboard.OpenClipboard()
                try:
                    if not self.clipboard.IsClipboardFormatAvailable(self.win32con.CF_UNICODETEXT):
                        return ""
                    return self.clipboard.GetClipboardData(self.win32con.CF_UNICODETEXT) or ""
                finally:
                    self.clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.05)
        return ""

    def _ctrl(self, virtual_key: int) -> None:
        key_up = 0x0002
        self.user32.keybd_event(self.win32con.VK_CONTROL, 0, 0, 0)
        self.user32.keybd_event(virtual_key, 0, 0, 0)
        self.user32.keybd_event(virtual_key, 0, key_up, 0)
        self.user32.keybd_event(self.win32con.VK_CONTROL, 0, key_up, 0)

    def _prepare_transcript_tail(self) -> None:
        key_up = 0x0002
        self.user32.keybd_event(self.win32con.VK_END, 0, 0, 0)
        self.user32.keybd_event(self.win32con.VK_END, 0, key_up, 0)
        time.sleep(0.12)

    def read_raw(self, exact_room_title: str) -> str:
        with self._lock:
            room = self._find_exact_room(exact_room_title)
            transcript = self._find_child(room, CHAT_LIST_CLASS)
            previous = self.user32.GetForegroundWindow()
            clipboard_had_data, previous_text = self._snapshot_text_clipboard()
            copied = ""
            try:
                self._set_text_clipboard(None)
                self._activate_and_focus(room, transcript)
                time.sleep(0.12)
                self._prepare_transcript_tail()
                self._ctrl(ord("A"))
                time.sleep(0.10)
                self._ctrl(ord("C"))
                for _ in range(10):
                    time.sleep(0.08)
                    copied = self._read_text_clipboard()
                    if copied:
                        break
            finally:
                try:
                    self._set_text_clipboard(previous_text if clipboard_had_data else None)
                finally:
                    self._restore_foreground(previous)
            if not copied.strip():
                raise AdapterError("empty_transcript", "KakaoTalk returned no readable transcript text")
            return copied

    def send_text(self, exact_room_title: str, text: str) -> None:
        with self._lock:
            room = self._find_exact_room(exact_room_title)
            edit = self._find_child(room, CHAT_EDIT_CLASS)
            existing_length = self.win32api.SendMessage(edit, 0x000E, 0, 0)  # WM_GETTEXTLENGTH
            if existing_length:
                raise AdapterError(
                    "input_not_empty",
                    "KakaoTalk input already contains a user draft; nothing was overwritten",
                )
            result = self.win32api.SendMessage(edit, self.win32con.WM_SETTEXT, 0, text)
            if not result:
                raise AdapterError("input_failed", "KakaoTalk rejected the text input")
            time.sleep(0.08)
            try:
                self.win32api.PostMessage(edit, self.win32con.WM_KEYDOWN, self.win32con.VK_RETURN, 0)
                self.win32api.PostMessage(edit, self.win32con.WM_KEYUP, self.win32con.VK_RETURN, 0)
            except Exception as exc:
                raise AdapterError(
                    "send_unknown",
                    "The send keystroke outcome is unknown; do not retry automatically",
                ) from exc


def win32_runtime_details() -> dict[str, str]:
    return {"platform": sys.platform, "python": sys.version.split()[0]}
