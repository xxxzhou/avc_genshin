"""统一异常体系（docs/design/02 §4.3、04-任务契约与注册.md）。

异常本身是控制流的一部分（非全是错误）：``NormalEnd``/``Retry`` 是任务请求的特殊终止，
``TaskError`` 系列携带失败分类，供结构化日志（03 §7）和 AI 失败回流（06 §6）使用。
"""

from __future__ import annotations

# ── 失败分类字符串（写进 JSONL 的 failure_type 字段，AI 据此定向修正）──
# 与 docs/design/02 §4.3 对齐；新增分类时同步更新。
FAILURE_TYPES = frozenset(
    {
        "UnexpectedScene",  # 期望场景与实际不符（如弹窗打断）
        "TemplateNotFound",  # 模板/图标匹配失败
        "StuckAt",  # 导航卡住（位置/场景长期无进展）
        "InputConflict",  # 输入通道权属冲突
        "Timeout",  # 等待/动作超时
        "PolicyViolation",  # 触发护栏（如试图消耗被禁资源）
        "TaskError",  # 通用任务错误（未细分）
    }
)


class AvcsError(Exception):
    """avc_genshin 所有框架异常的基类。"""


# ── 任务控制流异常（非纯错误，Runtime 识别后走对应出口）──


class NormalEnd(AvcsError):
    """任务请求**正常**终止（如检测到已完成、主动跳过）。非失败，不计 failure。"""

    def __init__(self, reason: str = ""):
        super().__init__(reason)
        self.reason = reason


class Retry(AvcsError):
    """任务请求**重试**当前流程（可携带 attempts 上限由 Runtime 裁决）。"""

    def __init__(self, reason: str = "", attempts: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


class CancelledError(AvcsError):
    """统一取消（Ctrl+C / 用户停止 / 超时）。对应 01 §4.4 的 CancellationToken。"""


# ── 任务失败异常（带 failure_type，记 failure 事件 + 存证）──


class TaskError(AvcsError):
    """通用任务失败。子类通过 ``failure_type`` 标识分类。"""

    failure_type: str = "TaskError"

    def __init__(self, reason: str = "", **context):
        super().__init__(reason)
        self.reason = reason
        self.context = context


class TemplateNotFound(TaskError):
    failure_type = "TemplateNotFound"


class UnexpectedScene(TaskError):
    failure_type = "UnexpectedScene"

    def __init__(self, expected: str = "", got: str = "", **ctx):
        super().__init__(reason=ctx.get("reason", ""), expected=expected, got=got, **ctx)
        self.expected = expected
        self.got = got


class StuckAt(TaskError):
    failure_type = "StuckAt"

    def __init__(self, pos=None, duration: float = 0.0, **ctx):
        super().__init__(reason=ctx.get("reason", ""), pos=pos, duration=duration, **ctx)
        self.pos = pos
        self.duration = duration


class Timeout(TaskError):
    failure_type = "Timeout"


# ── 基础设施异常（可靠性地基，02 §2 §5）──


class InputConflict(AvcsError):
    """输入通道权属冲突：两个写者抢同一通道（如两个守护都要按 F）。

    属框架主动拒绝，防"误按 F 弹面板"类副作用（01 §8.2）。
    """

    failure_type = "InputConflict"

    def __init__(self, channel: str = "", holders=(), **ctx):
        super().__init__(ctx.get("reason", f"input conflict on {channel}"))
        self.channel = channel
        self.holders = tuple(holders)


class PolicyViolation(AvcsError):
    """护栏拦截：任务试图做策略禁止的事（消耗被禁资源 / 进禁入区域 / 超时预算）。"""

    failure_type = "PolicyViolation"

    def __init__(self, rule: str = "", **ctx):
        super().__init__(ctx.get("reason", f"policy violation: {rule}"))
        self.rule = rule
