"""工具函数（docs/design/03 §6）。

sleep / wait_until / 坐标转换 / 1080p 归一化 / **拟人化抖动原语**。

⚠️ 关键事实（与 IMPLEMENTATION §5.3 / CLAUDE §8 的旧描述不符，已修订）：
avc 的 Python 绑定**没有** ``setHumanize`` / ``setJitterSeed`` / ``setClickHoldMs``
（见 swig/python/avc/input.py）。avc 仅提供 ``setMoveDurationMs``(动画移动) /
``setMoveSteps``(插值) / ``setKeyDelayMs``(按键间隔) 这些平滑手段。
因此**拟人化（坐标抖动 + 随机操作间隔 + 按住时长随机）由框架层实现**，
本模块提供原语，由 ``GameContext`` / ``high_level_api`` 在每次输入时套用。
CLAUDE §8「拟人化必须启用 / 0.8–1.2× 抖动」的要求不变，只是落点在框架而非 avc。
"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


# ── RNG（可复现：jitter_seed 固定 → 回归/调试可重放；CLAUDE §8）──


def make_rng(seed: int | None = None) -> random.Random:
    """None → 真随机；int → 可复现。"""
    return random.Random(seed) if seed is not None else random.Random()


_RNG = make_rng(int(os.getenv("AVC_JITTER_SEED")) if os.getenv("AVC_JITTER_SEED") else None)


def rng() -> random.Random:
    """全局 RNG（由 Runtime 启动时按 config.jitter_seed 重置）。"""
    return _RNG


def set_seed(seed: int | None) -> None:
    """重置全局 RNG。None=真随机；int=可复现（debug/回归）。

    由 ``GameContext`` / Runtime 启动时按 ``config.jitter_seed`` 调用，
    使整次运行的抖动序列可复现。
    """
    global _RNG
    _RNG = make_rng(seed)


# ── 拟人化原语 ──


def jitter(value: float, factor: float = 0.15) -> float:
    """``value × (1 + uniform(-factor, factor))``，即 0.85–1.15×（factor=0.15）。"""
    return value * (1.0 + _RNG.uniform(-factor, factor))


def jitter_coord(x: float, y: float, px: float = 2.0) -> tuple[float, float]:
    """坐标加 ±``px`` 像素抖动（避免每次精确落点）。"""
    return x + _RNG.uniform(-px, px), y + _RNG.uniform(-px, px)


def rand_in_range(lo: float, hi: float) -> float:
    return _RNG.uniform(lo, hi)


def human_delay(base: float, factor: float = 0.15) -> float:
    """``base`` 秒的操作间隔，叠加 0.8–1.2× 抖动（CLAUDE §8）。"""
    return max(0.0, jitter(base, factor))


# ── 时间 / 等待 ──


def sleep(seconds: float) -> None:
    """阻塞 sleep。阶段三接入 CancellationToken 后，由 Runtime 提供可取消版本。"""
    if seconds > 0:
        time.sleep(seconds)


def wait_until(pred: Callable[[], bool], timeout: float = 30.0, interval: float = 0.2) -> bool:
    """轮询 ``pred`` 直到为真或超时。返回是否在超时前满足。

    阶段三接入 token 后，``pred`` 内的取消点会抛 ``CancelledError`` 中断等待。
    """
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(min(interval, max(0.0, deadline - time.monotonic())))


# ── 坐标 / 1080p ──

# 设计基准分辨率（CLAUDE §8）：所有模板/坐标基于 1080p。
NATIVE_W, NATIVE_H = 1920, 1080


def scale_to_native(x: float, y: float, cur_w: int, cur_h: int) -> tuple[float, float]:
    """把当前分辨率下的坐标缩放到 1080p 基准（启动已强制 1080p，通常为恒等）。

    预留给多分辨率鲁棒性：若将来放宽 1080p 约束，模板坐标须经此转换。
    """
    if (cur_w, cur_h) == (NATIVE_W, NATIVE_H):
        return x, y
    return x * NATIVE_W / cur_w, y * NATIVE_H / cur_h


# ── 文件系统 ──


def ensure_dir(path: str | Path) -> Path:
    """递归创建目录（已存在不报错）。用于 logs/ debug/ cache/ 运行时建目录。"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
