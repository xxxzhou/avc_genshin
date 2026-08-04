"""InputAuthority —— 输入通道权属（docs/design/02 §2）。

把输入按**通道分组**，守护/主任务**声明并租用**它要用的通道；InputAuthority 保证
同一通道同一时刻只有一个写者（或高优先级抢占低优先级）。这让并发模型**可证明安全**：

  - 「边走边拾取」合法：``go_to`` 持 {MOVE, MOUSE_MOVE}、``auto_pick`` 持 {INTERACT}，
    通道不重叠 → 共存（02 §2.2）。
  - 两个都想持 {INTERACT} → 冲突被框架拒绝（抛 InputConflict，带双方名）→ AI 据此修正。

仲裁规则见 02 §2.4。抢占：高优先级取走通道后，低优先级租约的 ``active`` 变 False，
该守护检测到后自行挂起（释放 + 等待 + 场景恢复后重获）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from framework.errors import InputConflict

if TYPE_CHECKING:
    from collections.abc import Iterable


class InputChannel(str, Enum):
    """输入通道分组（02 §2.2）。守护用 owns_keys 声明它碰哪些通道。"""

    MOVE = "move"  # W/A/S/D（移动）
    INTERACT = "interact"  # F（交互/拾取）
    COMBAT = "combat"  # E/Q/普攻/num1~4（战斗）
    MENU = "menu"  # Esc/M/Tab/J（开菜单）
    MOUSE_MOVE = "mouse_move"
    MOUSE_CLICK = "mouse_click"


@dataclass
class _Slot:
    """某通道当前的持有者（一个 InputLease 或空）。"""

    lease: "InputLease | None" = None


class InputLease:
    """一次通道租用。``active`` 表示是否仍持有**全部**声明的通道。

    被高优先级抢占任一通道 → active=False（守护应挂起）。
    空 owns_keys（如 loading_wait）→ 永远 active（all() of empty = True）。
    """

    __slots__ = ("authority", "channels", "holder", "priority", "_released")

    def __init__(self, authority: "InputAuthority", channels: frozenset[InputChannel], holder: str, priority: int):
        self.authority = authority
        self.channels = channels
        self.holder = holder
        self.priority = priority
        self._released = False

    @property
    def active(self) -> bool:
        if self._released:
            return False
        return all(self.authority._holders.get(c) is not None and self.authority._holders[c].lease is self for c in self.channels)

    def release(self) -> None:
        self.authority.release(self)

    def __repr__(self) -> str:
        ch = ",".join(c.value for c in self.channels) or "(none)"
        return f"<InputLease {self.holder} pri={self.priority} [{ch}] active={self.active}>"


class InputAuthority:
    """通道权属仲裁器（单次运行内，loop 线程操作 → 无锁）。"""

    def __init__(self):
        self._holders: dict[InputChannel, _Slot] = {}

    def acquire(
        self,
        channels: "Iterable[InputChannel]",
        holder: str,
        priority: int = 0,
    ) -> InputLease:
        """租用一组通道。

        - 空闲 → 授予；被低优先级持有 → 抢占（其租约 active→False）；
        - 被同/高优先级的**他者**持有 → 抛 InputConflict（原子：任一冲突则全不授）；
        - 同 holder 再获 → 刷新（不冲突）。
        """
        chs = frozenset(channels)
        # 冲突检测
        conflicts: list[tuple[InputChannel, str, int]] = []
        for c in chs:
            slot = self._holders.get(c)
            if slot is None or slot.lease is None:
                continue
            if slot.lease.holder == holder:
                continue  # 同 holder，刷新
            if slot.lease.priority >= priority:
                conflicts.append((c, slot.lease.holder, slot.lease.priority))
        if conflicts:
            chans = ", ".join(c.value for c, _, _ in conflicts)
            holders = sorted({h for _, h, _ in conflicts} | {holder})
            raise InputConflict(
                channel=chans,
                holders=holders,
                reason=f"{holder} 申请 [{chans}] 与 {holders} 冲突（同/高优先级）",
            )
        # 授予（可能抢占低优先级持有者，其 lease 自动 active→False）
        lease = InputLease(self, chs, holder, priority)
        for c in chs:
            self._holders[c] = _Slot(lease=lease)
        return lease

    def release(self, lease: InputLease) -> None:
        """释放租约持有的通道（仅清自己作为 holder 的通道）。"""
        lease._released = True
        for c in lease.channels:
            slot = self._holders.get(c)
            if slot is not None and slot.lease is lease:
                del self._holders[c]

    def holder_of(self, channel: InputChannel) -> str | None:
        slot = self._holders.get(channel)
        return slot.lease.holder if (slot and slot.lease) else None

    def __repr__(self) -> str:
        held = {c: s.lease.holder for c, s in self._holders.items() if s.lease}
        return f"<InputAuthority held={held}>"
