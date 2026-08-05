"""高层 API g.*（docs/design/01 §6、05 §2）—— AI 写任务的主要工具箱。

同步外壳：AI 调用同步阻塞；内部经同步桥把 avc 操作提交到 loop（avc 安全）。
每次 g.* 调用，框架在内部自动：检查 token（取消点）→ 经 InputAuthority（输入类）→
读 SharedState（场景/检测）→ 过 Policy（护栏）→ 写 Observe（日志）。任务作者无感。

三层（05 §1）：g.* 高层语义（本模块）/ ctx.* 运行时控制 / vision.* 视觉原语（降级）。

移动/对话（teleport_to/go_to/talk）依赖领域能力（abilities/navigation、fighter），
属阶段五+，本阶段抛 NotImplementedError 并指向后续。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from framework.scene import Scene

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.runtime import Runtime


# ── 运行时决策层接缝（01 §11，后置；现在 NoOpDecider）──


@dataclass
class Decision:
    answer: dict
    confidence: float  # [0,1]
    source: str  # "cv"|"llm"|"vlm"|"human"
    rationale: str


class Decider:  # Protocol-like 基类
    def decide(self, question: str, schema: dict, context: Any = None) -> Decision:
        raise NotImplementedError


class NoOpDecider(Decider):
    """现阶段唯一实现：决策层后置。接入时只替换注入，g.decide 签名不变。"""

    def decide(self, *a, **k) -> Decision:
        raise NotImplementedError("运行时决策层后置（见 docs/design/01-执行引擎.md §11）")


class HighLevelApi:
    """``g.*`` —— AI 写任务的高层语义 API。"""

    def __init__(self, ctx: "GameContext", *, runtime: "Runtime"):
        self.ctx = ctx
        self.runtime = runtime
        self._loop = runtime.loop
        self._decider: Decider = NoOpDecider()

    # ── 内部 ──

    @property
    def _token(self):
        return self.runtime._token

    def _call(self, coro, timeout: float | None = 60):
        """同步外壳 → async 内核：提交到 loop 阻塞等结果。前后检查取消。"""
        t = self._token
        if t is not None:
            t.check()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        finally:
            if t is not None:
                t.check()

    def _observe(self, kind: str, **fields) -> None:
        if self.runtime._observe is not None:
            self.runtime._observe.event(kind, **fields)

    # ── 纯读（inline，读 SharedState；GIL 下引用/字典成员读写原子）──

    @property
    def scene(self):
        return self.runtime.shared.scene

    def is_loading(self) -> bool:
        s = self.runtime.shared.scene
        return s is not None and s.scene is Scene.LOADING

    def detect_objects(self, cls: str | None = None):
        """读共享检测结果（FrameDaemon 推理）。cls 指定取一类，None 取全部。"""
        dets = self.runtime.shared.detections
        return dets.get(cls, []) if cls else dets

    # ── 世界模型（06）──

    def set_flag(self, key: str, val: Any) -> None:
        self.runtime.shared.set_flag(key, val)

    def get_flag(self, key: str, default: Any = None) -> Any:
        return self.runtime.shared.get_flag(key, default)

    def has_flag(self, key: str) -> bool:
        return self.runtime.shared.has_flag(key)

    # ── 操作（桥接到 loop；avc 调用在 loop 线程）──

    def click(self, x: float, y: float, button: str | int = "left") -> None:
        self._call(self._click(x, y, button))

    async def _click(self, x, y, button):
        self.ctx.click_at(x, y, button)
        self._observe("action", action="click", x=x, y=y)

    def press(self, key, hold: float = 0.0) -> None:
        self._call(self._press(key, hold))

    async def _press(self, key, hold):
        self.ctx.press(key, hold)
        self._observe("action", action="press", key=str(key), hold=hold)

    def hotkey(self, *keys) -> None:
        self._call(self._hotkey(keys))

    async def _hotkey(self, keys):
        self.ctx.hotkey(*keys)
        self._observe("action", action="hotkey", keys=[str(k) for k in keys])

    def type_text(self, text: str) -> None:
        self._call(self._type_text(text))

    async def _type_text(self, text):
        self.ctx.type_text(text)
        self._observe("action", action="type_text", text=text)

    def move_to(self, x: float, y: float) -> None:
        self._call(self._move_to(x, y))

    async def _move_to(self, x, y):
        self.ctx.ic.moveTo(int(x), int(y))

    def scroll(self, dx: int, dy: int) -> None:
        self._call(self._scroll(dx, dy))

    async def _scroll(self, dx, dy):
        self.ctx.ic.scroll(dx, dy)

    def capture(self):
        """即时截图（桥接到 loop）。"""
        return self._call(self._capture())

    async def _capture(self):
        return self.ctx.capture()

    # ── 等待（桥接；loop 上轮询）──

    def wait_until(self, pred: Callable[[], bool], timeout: float = 30.0, interval: float = 0.2) -> bool:
        return self._call(self._wait_until(pred, timeout, interval), timeout=timeout + 2)

    async def _wait_until(self, pred, timeout, interval):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if pred():
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    def wait_scene(self, scene: Scene, timeout: float = 30.0, stable: float = 0.4) -> bool:
        return self._call(self._wait_scene(scene, timeout, stable), timeout=timeout + 2)

    async def _wait_scene(self, scene, timeout, stable):
        deadline = time.monotonic() + timeout
        since = None
        while True:
            s = self.runtime.shared.scene
            ok = s is not None and s.scene is scene
            if ok:
                if since is None:
                    since = time.monotonic()
                if time.monotonic() - since >= stable:
                    return True
            else:
                since = None
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    def wait_main_ui(self, timeout: float = 30.0) -> bool:
        return self.wait_scene(Scene.MAIN_UI, timeout=timeout)

    def wait_loading(self, timeout: float = 60.0) -> bool:
        """等加载结束（离开 LOADING 场景）。"""
        return self._call(self._wait_not_scene(Scene.LOADING, timeout), timeout=timeout + 2)

    async def _wait_not_scene(self, scene, timeout):
        deadline = time.monotonic() + timeout
        while True:
            s = self.runtime.shared.scene
            if s is None or s.scene is not scene:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    def wait_text(self, kw: str, timeout: float = 10.0) -> bool:
        from abilities import vision_utils as vu

        return self.wait_until(lambda: vu.find_text(self.ctx, kw) is not None, timeout=timeout)

    def wait_template(self, path: str, timeout: float = 10.0, threshold: float = 0.8) -> bool:
        from abilities import vision_utils as vu

        return self.wait_until(lambda: vu.find_template(self.ctx, path, threshold) is not None, timeout=timeout)

    # ── 即时检测（桥接）──

    def find_template(self, path: str, threshold: float = 0.8):
        from abilities import vision_utils as vu

        return self._call(_run(vu.find_template, self.ctx, path, threshold))

    def find_text(self, kw: str):
        from abilities import vision_utils as vu

        return self._call(_run(vu.find_text, self.ctx, kw))

    # ── 移动 / 对话（领域能力）──

    def teleport_to(self, name_or_pos: str | tuple[float, float], map_name: str = "Teyvat") -> tuple[float, float]:
        """传送到指定位置（Phase B 实现）。

        Args:
            name_or_pos: 传送点名称（如"蒙德城"）或坐标 (x, y)
            map_name: 地图名称（默认"Teyvat"）

        Returns:
            实际到达位置 (tran_x, tran_y)
        """
        from abilities.navigation.tp import Teleporter

        teleporter = Teleporter(self.ctx, self)
        return self._call(self._teleport_coro(teleporter, name_or_pos, map_name))

    async def _teleport_coro(self, teleporter, name_or_pos, map_name):
        result = teleporter.teleport_to(name_or_pos, map_name)
        self._observe("action", action="teleport_to", target=str(name_or_pos))
        return result

    def go_to(self, pos, *, tolerance: float = 4.0, timeout: float = 240.0) -> bool:
        """走到指定位置（Phase B 实现）。

        Args:
            pos: Waypoint 对象或坐标 (x, y)
            tolerance: 到达距离阈值
            timeout: 超时秒数

        Returns:
            是否到达
        """
        from abilities.navigation.navigator import Navigator
        from abilities.navigation.path_executor import Waypoint

        if isinstance(pos, tuple):
            pos = Waypoint(x=pos[0], y=pos[1])
        nav = Navigator(self.ctx, self)
        return self._call(self._go_to_coro(nav, pos, tolerance, timeout), timeout=timeout + 10)

    async def _go_to_coro(self, nav, pos, tolerance, timeout):
        result = nav.go_to(pos, tolerance=tolerance, timeout=timeout)
        self._observe("action", action="go_to", target=f"({pos.x:.1f},{pos.y:.1f})")
        return result

    def talk(self, option: str) -> None:
        """选择对话选项（模糊文本匹配，Phase A 实现）。"""
        from abilities.dialog import talk as _talk

        self._call(self._talk_coro(option))

    async def _talk_coro(self, option):
        _talk(self.ctx, option)
        self._observe("action", action="talk", option=option)

    def talk_skip(self, timeout: float = 30.0) -> bool:
        """跳过对话直到离开 DIALOG 场景（Phase A 实现）。"""
        from abilities.dialog import talk_skip as _skip

        return self._call(self._skip_coro(timeout), timeout=timeout + 5)

    async def _skip_coro(self, timeout):
        _skip(self.ctx, timeout)
        self._observe("action", action="talk_skip")

    # ── 战斗（领域能力，Phase C）──

    def has_enemy(self) -> bool:
        """即时血条检测（红色色块）。不读 shared.detections（bgi_world 是否含稳定
        “敌人”类未验证）；血条是战斗专属可靠信号。详见 abilities/fighter.py。"""
        from abilities.fighter import SimpleFighter

        fighter = SimpleFighter(self.ctx, self)
        return self._call(_run(fighter.has_enemy), timeout=10)

    def find_nearest_enemy(self):
        """最近敌人（血条框，截图缓冲坐标系），无则 None。"""
        from abilities.fighter import SimpleFighter

        fighter = SimpleFighter(self.ctx, self)
        return self._call(_run(fighter.find_nearest_enemy), timeout=10)

    def is_q_ready(self) -> bool:
        """Q（元素爆发）是否就绪（q_classify 分类 Q 图标 ROI）。"""
        from abilities.fighter import SimpleFighter

        fighter = SimpleFighter(self.ctx, self)
        return self._call(_run(fighter.is_q_ready), timeout=15)  # 首次懒加载 ONNX，给足

    def fight(self, duration_s: float = 30, rotation: list | None = None) -> None:
        """站桩连招（阻塞 duration_s 或敌人清场）。rotation 见 fighter.DEFAULT_ROTATION。"""
        from abilities.fighter import SimpleFighter

        fighter = SimpleFighter(self.ctx, self)
        self._call(self._fight_coro(fighter, duration_s, rotation), timeout=duration_s + 15)

    async def _fight_coro(self, fighter, duration_s, rotation):
        fighter.fight(duration_s, rotation)
        self._observe("action", action="fight", duration=duration_s)

    def fight_until_clear(self, timeout: float = 120) -> bool:
        """战斗到清场（has_enemy 持续 False）或超时。返回是否清场完成。"""
        from abilities.fighter import SimpleFighter

        fighter = SimpleFighter(self.ctx, self)
        return self._call(
            self._fight_until_clear_coro(fighter, timeout), timeout=timeout + 30
        )

    async def _fight_until_clear_coro(self, fighter, timeout):
        result = fighter.fight_until_clear(timeout=timeout)
        self._observe("action", action="fight_until_clear", cleared=result)
        return result

    # ── 运行时控制（委托 Runtime；ctx 也提供同名方法，04 §10）──

    def mount(self, name: str, **opts) -> None:
        self.runtime.mount(name, **opts)

    def unmount(self, name: str) -> None:
        self.runtime.unmount(name)

    def suspend_all(self) -> None:
        self.runtime.suspend_all()

    def resume_all(self) -> None:
        self.runtime.resume_all()

    def run(self, name: str, **params):
        """任务组合（与 ctx.run 同义，委托 Runtime._run_inline；阶段四已落地）。"""
        return self.ctx.run(name, **params)

    # ── 决策（预留，后置）──

    def decide(self, question: str, schema: dict | None = None, *, context="auto", timeout: float = 30) -> Decision:
        return self._decider.decide(question, schema or {}, context)


async def _run(fn, *args):
    """把同步 abilities 调用包成协程（在 loop 线程执行，avc 安全）。"""
    return fn(*args)
