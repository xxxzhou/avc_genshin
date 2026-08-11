"""Observe —— 可观测性地基（docs/design/02 §4、03 §7）。

执行时间线 + 结构化失败分类 + 失败自动存证。这是 **AI 迭代能收敛的眼睛**：
AI 没法坐到屏幕前，系统必须抓全诊断信息让 AI 远程定位（02 §4.1）。

- ``event(kind, **fields)``：追加一条时间线 + 写 JSONL（自动带 ts/scene）。
- ``failure(failure_type, **fields)``：记 failure 事件 + 自动存证（截图 + 期望 + timeline_tail）。
- ``save_evidence(ctx, tag)``：存当前帧到 ``debug/<run_id>/``，返回路径。

任务作者**不手写日志**——g.* 每次调用框架已记录；失败由框架自动存证。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from framework.errors import FAILURE_TYPES
from framework.logging import JsonlLogger

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.shared import SharedState


_SENTINEL = object()  # _transition 模式「尚无上次 ok 值」标记


class Observe:
    """运行期观测器：内存时间线（供 failure 上下文）+ JSONL 持久化 + 存证。"""

    TIMELINE_CAP = 500  # 内存保留最近 N 条（failure 的 timeline_tail 从此取）
    _THROTTLE_INTERVAL = 1.0  # 秒；time-window 节流模式默认间隔（防热轮询爆 JSONL）

    def __init__(
        self,
        logger: JsonlLogger,
        shared: "SharedState",
        debug_dir: str | Path = "debug",
    ):
        self.logger = logger
        self.shared = shared
        self.debug_dir = Path(debug_dir) / logger.run_id
        self._timeline: list[dict[str, Any]] = []
        # 订阅者：终端实时打印等（事件路径末尾 fan-out，异常不阻断）
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        # 节流状态（仅 opt-in：事件传 throttle_key/_quiet/_transition 才生效）
        self._last_emit: dict[str, float] = {}    # throttle_key -> 上次落地 monotonic
        self._elided: dict[str, int] = {}          # throttle_key -> 窗口内被折叠计数
        self._quiet_seen: set[str] = set()         # _quiet 模式已发首次的 key
        self._last_ok: dict[str, Any] = {}         # _transition 模式上次 ok 值

    # ── 订阅 ──

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """注册事件订阅者（如终端实时打印）。订阅者异常绝不阻断事件路径。"""
        self._subscribers.append(fn)

    # ── 事件 ──

    def event(self, kind: str, *, level: str = "info", task: str | None = None, **fields: Any) -> None:
        """记一条事件。自动注入 scene（来自 SharedState）+ ts/run_id（在 logger 注入）。

        节流（opt-in，见 ``设计实现.md §4.4``）—— 事件传以下任一控制标志才启用，否则
        全量落地（保留既有行为，不破坏 runtime/g.* 事件）：

        - ``throttle_key="..."``：time-window 模式，同 key 成功/观测事件每 ``_THROTTLE_INTERVAL``
          秒至多一条，窗口内其余折叠计数进下条的 ``elided`` 字段。
        - ``_quiet=True``：首次命中模式（同 key 整 run 只发首条成功），轮询探针用（scene classifier）。
        - ``_transition=True``：``ok`` 跳变才发（如 survival.low_hp False↔True）。

        **``ok is False`` 在 time-window 模式永不节流**（失败每次即时浮现）；但 ``_quiet``/
        ``_transition`` 显式模式优先——调用方主动要的激进抑制（防 10Hz 爆炸）即便失败也折叠。
        ``_quiet``/``_transition``/``throttle_key`` 为控制标志，不入写入内容。
        """
        quiet = fields.pop("_quiet", False)
        transition = fields.pop("_transition", False)
        throttle_key = fields.pop("throttle_key", None)
        throttle_active = quiet or transition or throttle_key is not None

        scene = self.shared.scene.scene.value if (self.shared and self.shared.scene) else None
        entry: dict[str, Any] = {"level": level, "event": kind, "scene": scene}
        if task:
            entry["task"] = task
        entry.update(fields)

        if throttle_active:
            allowed, elided = self._throttle_allow(entry, throttle_key, quiet, transition)
            if not allowed:
                return
            if elided:
                entry["elided"] = elided
                entry["sampled"] = True

        self._timeline.append(entry)
        if len(self._timeline) > self.TIMELINE_CAP:
            # 丢弃最旧的（保留尾部，failure 上下文用近期事件）
            del self._timeline[: len(self._timeline) - self.TIMELINE_CAP]
        self.logger.log(entry)
        # 订阅者 fan-out（异常吞掉，绝不阻断事件路径）
        for fn in self._subscribers:
            try:
                fn(entry)
            except Exception:
                pass

    def _throttle_allow(
        self, entry: dict[str, Any], key: str | None, quiet: bool, transition: bool
    ) -> tuple[bool, int]:
        """节流决策。返回 (是否落地, 折叠进本条的 elided 数)。

        优先级：``_quiet`` > ``_transition`` > time-window（``throttle_key``）。

        - **显式模式优先于失败直通**：``_quiet``/``_transition`` 是调用方主动要的激进抑制
          （场景分类器 10Hz、血量跳变），即便 ``ok=False`` 也按其规则折叠——否则 10Hz 的
          「未找到」会爆 JSONL。「``ok=False`` 永不节流」**只**作用于默认 time-window 模式：
          那里一次失败（如 nav.step stuck）值得每次即时浮现，不被 1/s 窗口吃掉。
        - key 缺省取 event 名。``ok=False`` 落地时把该 key 累积 elided 带上清零。
        """
        k = key or entry.get("event", "")
        ok = entry.get("ok")
        if quiet:
            # 同 key 整 run 只发首条（任意 ok）；其余折叠。场景分类器「找到一次」足够。
            if k in self._quiet_seen:
                self._elided[k] = self._elided.get(k, 0) + 1
                return False, 0
            self._quiet_seen.add(k)
            return True, self._elided.pop(k, 0)
        if transition:
            # ok 跳变才发（如 low_hp False↔True）；同值重复折叠。
            last = self._last_ok.get(k, _SENTINEL)
            if last is not _SENTINEL and last == ok:
                self._elided[k] = self._elided.get(k, 0) + 1
                return False, 0
            self._last_ok[k] = ok
            return True, self._elided.pop(k, 0)
        # time-window 模式：ok=False 永不节流（失败每次即时浮现，不被窗口吃掉）
        if ok is False:
            return True, self._elided.pop(k, 0)
        now = time.monotonic()
        last_t = self._last_emit.get(k)
        if last_t is not None and now - last_t < self._THROTTLE_INTERVAL:
            self._elided[k] = self._elided.get(k, 0) + 1
            return False, 0
        self._last_emit[k] = now
        return True, self._elided.pop(k, 0)

    def failure(self, failure_type: str, *, task: str | None = None, **fields: Any) -> None:
        """记 failure 事件 + 自动存证（截图路径写入 shot 字段）。

        failure_type 取 errors.FAILURE_TYPES 之一（02 §4.3）。附带最近 N 步时间线，
        让 AI 拿到 failure 行 + 上下文即可定向修正，不必整段重写。
        """
        if failure_type not in FAILURE_TYPES:
            failure_type = "TaskError"
        tail = self._timeline[-12:]  # 最近 12 步作为上下文
        self.event(
            "failure",
            level="error",
            task=task,
            failure_type=failure_type,
            timeline_tail=tail,
            **fields,
        )

    # ── 存证 ──

    def save_evidence(self, ctx: "GameContext", tag: str = "evidence") -> str | None:
        """存当前帧到 debug/<run_id>/<ts>_<tag>.png，返回路径（失败时 None）。"""
        try:
            buf = ctx.capture()
            if buf is None:
                return None
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            ts = f"{self.logger.ts():.2f}".replace(".", "_")
            path = self.debug_dir / f"{ts}_{tag}.png"
            buf.save(str(path))
            return str(path)
        except Exception:
            return None  # 存证失败不应淹没原始 failure

    # ── 查询 ──

    def timeline(self) -> list[dict[str, Any]]:
        """整条时间线（供 AI 回流诊断）。"""
        return list(self._timeline)


class _NullObserve:
    """``Observe`` 的 no-op 占位。

    无 runtime / 无活跃 run 时 ``ctx.observe`` / ``g.observe`` 返回本单例。
    **签名与 ``Observe`` 钉死一致**；调用方永不判空（``ctx.observe`` 永可调用），
    见 ``设计实现.md §4.4 能力可观测性约定``。
    """

    _self = None  # 单例锚点

    def __new__(cls):
        if cls._self is None:
            cls._self = super().__new__(cls)
        return cls._self

    def event(self, kind: str, *, level: str = "info", task: str | None = None, **fields: Any) -> None:
        pass

    def failure(self, failure_type: str, *, task: str | None = None, **fields: Any) -> None:
        # 不做 failure_type 校验（no-op 无意义；Observe.failure 会校验）
        pass

    def save_evidence(self, ctx: "GameContext", tag: str = "evidence") -> None:
        return None

    def subscribe(self, fn) -> None:
        pass

    def timeline(self) -> list[dict[str, Any]]:
        return []


# 全局单例：ctx.observe / g.observe 在无活跃 run 时返回它（调用方永不判空）
_NULL = _NullObserve()
