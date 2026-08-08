"""全局热键监听（Windows，双机制保证灵敏）。

两种机制叠加，确保 F9 一定有效：
1. ``RegisterHotKey``（系统级，低延迟）：管理员权限下注册成功即优先走消息回调。
   但若本程序非管理员而其他程序（如原神）以管理员运行，注册会**静默失败**。
2. ``GetAsyncKeyState`` 轮询兜底：后台线程每 10ms 读一次物理键状态（不依赖窗口焦点，
   普通权限同样可读），带**边缘检测**（仅"未按下→按下"触发一次），按住不重复。

按 F9 触发 ``CancellationToken.cancel()``。

用法（Runtime 自动调用）：
    hk = HotkeyListener()
    hk.register(VK_F9, callback=token.cancel)
    hk.start()   # 后台线程
    ...
    hk.stop()    # Runtime shutdown 时
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import threading
import time
from typing import Callable

# Win32 常量
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000  # Win7+：按住不重复触发
VK_F9 = 0x78


def _is_win32() -> bool:
    return sys.platform == "win32"


class HotkeyListener:
    """Windows 全局热键监听器。RegisterHotKey + GetAsyncKeyState 轮询双保险。"""

    POLL_INTERVAL = 0.01  # 轮询兜底间隔（秒）

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._registrations: list[tuple[int, int, int, Callable[[], None] | None]] = []  # (id, vk, mod, cb)
        self._next_id = 1
        # 轮询用：记录各 vk 上次物理按下状态（边缘检测）
        self._prev_pressed: dict[int, bool] = {}
        self._registered_ok: list[int] = []  # RegisterHotKey 成功的 id（stop 时反注册）

    def register(self, vk: int, modifiers: int = 0, callback: Callable[[], None] | None = None) -> int:
        """注册一个全局热键。返回 hotkey id（用于 UnregisterHotKey）。

        Args:
            vk: 虚拟键码（如 VK_F9=0x78）
            modifiers: MOD_ALT/MOD_CTRL/MOD_SHIFT/MOD_NOREPEAT 组合
            callback: 触发时调用的函数
        """
        hid = self._next_id
        self._next_id += 1
        self._registrations.append((hid, vk, modifiers, callback))
        return hid

    def start(self) -> None:
        """启动监听线程。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="avcgs-hotkey")
        self._thread.start()

    def stop(self) -> None:
        """停止监听线程。"""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    # ── 后台线程 ──

    def _loop(self) -> None:
        if not _is_win32():
            return
        user32 = ctypes.windll.user32

        # 1) 尝试 RegisterHotKey（绑定本线程）；失败不阻塞，轮询兜底兜住
        msg = ctypes.wintypes.MSG()
        ok_vks: set[int] = set()  # RegisterHotKey 成功的 vk：消息路径已覆盖，轮询跳过
        for hid, vk, mod, _cb in self._registrations:
            try:
                if user32.RegisterHotKey(None, hid, mod | MOD_NOREPEAT, vk):
                    self._registered_ok.append(hid)
                    ok_vks.add(vk)
            except Exception:
                pass

        self._prev_pressed = {vk: False for _hid, vk, _mod, _cb in self._registrations}

        # 2) 循环：PeekMessage 处理消息（非阻塞）+ 轮询兜底（仅未注册成功的 vk）
        while not self._stop_event.is_set():
            # 处理已到达的热键消息（RegisterHotKey 路径，低延迟）
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                if msg.message == WM_HOTKEY:
                    self._dispatch(msg.wParam)
                if self._stop_event.is_set():
                    return

            # 轮询兜底（GetAsyncKeyState 不依赖 RegisterHotKey 权限/占用）
            for hid, vk, _mod, cb in self._registrations:
                if vk in ok_vks:
                    continue
                pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                prev = self._prev_pressed.get(vk, False)
                self._prev_pressed[vk] = pressed
                if pressed and not prev and cb is not None:  # 上升沿触发一次
                    try:
                        cb()
                    except Exception:
                        pass

            time.sleep(self.POLL_INTERVAL)

        # 清理：反注册所有热键
        for hid in self._registered_ok:
            try:
                user32.UnregisterHotKey(None, hid)
            except Exception:
                pass

    def _dispatch(self, hid: int) -> None:
        """按 hotkey id 触发回调。"""
        for reg_id, _vk, _mod, cb in self._registrations:
            if reg_id == hid and cb is not None:
                try:
                    cb()
                except Exception:
                    pass
                break
