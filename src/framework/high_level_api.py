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
import math
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

    def _save_evidence(self, tag: str) -> str | None:
        """失败时存截图（teardown 竞态时返 None）。"""
        if self.runtime._observe is not None:
            try:
                return self.runtime._observe.save_evidence(self.ctx, tag)
            except Exception:
                return None
        return None

    @property
    def observe(self):
        """结构化观测句柄（``设计实现.md §2``），与 ``ctx.observe`` 同源。

        永可调用、永不返回 None：活跃 run 内返真 ``Observe``，否则 ``_NullObserve``。
        ability 取 ``self.g.observe`` 或 ``self.ctx.observe`` 均可。**保留** ``_observe``
        的 None-guard（处理 teardown 竞态：runtime.py 置 ``_observe=None`` 早于守护取消）。
        """
        from framework.observe import _NULL

        if self.runtime._observe is not None:
            return self.runtime._observe
        return _NULL

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
        """滚轮滚动。dx/dy 为滚轮格数（1=1格，avc 内部 ×WHEEL_DELTA 发送）。"""
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

        return self.wait_until(lambda: vu.find_text(self.ctx, kw, _quiet=True) is not None, timeout=timeout)

    def wait_template(self, path: str, timeout: float = 10.0, threshold: float = 0.8) -> bool:
        from abilities import vision_utils as vu

        return self.wait_until(lambda: vu.find_template(self.ctx, path, threshold, _quiet=True) is not None, timeout=timeout)

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

        # ⚠ 直接在工作线程执行领域函数（而非包 _coro 提交到 loop）：teleport_to 内部
        # 会调 g.*（wait_scene/wait_main_ui/click），若在 loop 线程同步阻塞执行，
        # 内部 g.* 桥提交到同一 loop 被卡住 → 死锁（TimeoutError）。工作线程直调 +
        # 内部 g.* 桥正常回 loop（loop 空闲），与 verify do_map_calib 同模式。
        teleporter = Teleporter(self.ctx, self)
        result = teleporter.teleport_to(name_or_pos, map_name)
        self._observe("action", action="teleport_to", target=str(name_or_pos))
        return result

    def find_blossom_and_nearest_tp(
        self, flower_type: str = "", exclude: list | None = None
    ) -> dict | None:
        """在大地图上找地脉花，返回花信息和最近传送点。

        需在 MAP 场景下调用（由调用方保证先打开地图）。
        流程：检测花图标 → SIFT 定位视口 → 屏幕坐标转游戏坐标 → 查最近传送点。

        Args:
            flower_type: 筛选花类型，""=不限，"revelation"=启示之花，"wealth"=藏金之花
            exclude: 花位置黑名单 [(x,y),...]——距黑名单点 <500 单位的花跳过
                （失败换花重试用，如山地花走不到）

        Returns:
            {"blossom_type": str, "blossom_pos": (x,y), "nearest_tp": TpPosition, "screen_pos": (x,y)}
            或 None（未检测到花 / SIFT 定位失败）
        """
        from abilities.navigation.map_ops import MapController, MAP_CENTER_X, MAP_CENTER_Y, MAP_SCALE_FACTOR
        from abilities.navigation.position import PositionGetter
        from abilities.navigation.tp import TpDatabase
        from avc._core import KeyCode
        from framework import utils

        mc = MapController(self.ctx, self)
        # ⚠ 同下：地图交互期间 SourcePlayer 帧不可靠，统一 sc 直抓（2026-08-15 实机）
        frame = self.ctx._capture_sc()
        if frame is None:
            self._observe("detect.blossom", ability="tp", phase="observe",
                          step="capture_initial", ok=False, reason="no_frame")
            return None

        # ★ 调整缩放到固定等级再检测（BGI LocateLeyLineOutcrop 做法）
        # 高 zoom 下 SIFT 误差被放大，坐标偏移大→选错传送点。
        # 调到 ~3.0 后花图标仍可见，误差放大系数减小。
        # ⚠ 注意：放大时视口范围缩小，需先把花移到中心避免丢失。
        zoom = mc.measure_zoom_level(frame) or 3.0
        blossoms = mc.find_blossom_on_map(frame)
        self._observe("detect.blossom", ability="tp", phase="observe",
                      step="find_initial", zoom_measured=zoom,
                      count=len(blossoms), ok=len(blossoms) > 0,
                      reason=None if blossoms else "no_blossom_initial")
        if blossoms:
            # 把最近的花移到视口中心，再缩放
            best = blossoms[0]
            north_delta = (MAP_CENTER_Y - best.screen_y) * zoom / MAP_SCALE_FACTOR
            west_delta = (MAP_CENTER_X - best.screen_x) * zoom / MAP_SCALE_FACTOR
            if abs(north_delta) > 100 or abs(west_delta) > 100:
                mc.drag_map(north_delta, west_delta, zoom)
                utils.sleep(0.3)
                frame = self.ctx._capture_sc()
                if frame is None:
                    self._observe("detect.blossom", ability="tp", phase="observe",
                                  step="recapture_after_drag", ok=False, reason="no_frame")
                    return None
        mc.set_zoom_level(3.0, frame)
        # ⚠ 2026-08-15 实机（r_20260815_093453/093814/094006 三连漏检）：缩放动画 +
        # SourcePlayer 地图交互期间帧冻结/滞后——ctx.capture() 的重试帧漏检，而失败
        # 后的存证帧 live 模板 1.0 命中。改用 _capture_sc() 直抓（IScreenCapture 归一化，
        # 实机始终真实，同传送冻结案结论）+ 重试 5 次 × 0.7s 兜住动画 settle。
        blossoms: list = []
        for attempt in range(5):
            utils.sleep(0.7 if attempt else 0.4)
            frame = self.ctx._capture_sc()
            if frame is None:
                continue
            found = mc.find_blossom_on_map(frame)
            # 诊断：逐次存 sc 帧本体 + 计数事件（2026-08-15 三连漏检定位用，稳后删）
            try:
                from framework import logging as _flog  # noqa: F401
                self.ctx.observe.debug_dir.mkdir(parents=True, exist_ok=True)
                ev_path = str(self.ctx.observe.debug_dir / f"sc_try{attempt}.png")
                frame.save(ev_path)
            except Exception:
                ev_path = None
            self._observe("detect.blossom", ability="tp", phase="observe",
                          step="retry", attempt=attempt, count=len(found),
                          evidence=ev_path)
            if flower_type:
                found = [b for b in found if b.blossom_type == flower_type]
            if found:
                blossoms = found
                break
        if frame is None:
            self._observe("detect.blossom", ability="tp", phase="observe",
                          step="recapture_after_zoom", ok=False, reason="no_frame")
            return None

        # 1. 检测花图标（blossoms 已在重试循环中填充）
        if not blossoms:
            # ⚠ 失败时存图：让 AI 能看到当时地图状态（视口是否对/zoom 是否对）
            evidence = self._save_evidence("find_blossom_no_match")
            self._observe("detect.blossom", ability="tp", phase="observe",
                          step="find_after_zoom", ok=False,
                          reason="no_blossom_after_zoom",
                          flower_type_filter=flower_type or None,
                          zoom_set=3.0,
                          evidence=evidence)
            return None

        # 2. SIFT 定位视口中心
        pg = PositionGetter(self.ctx)
        viewport = pg.get_position_from_big_map(frame)
        # 2.5 视口漂移守卫：地图记忆上次视图（任务间残留）。若视口中心远离玩家已知
        # 位置（ctx._shared_pos_prev，传送种子/上次定位），或 prev 未知（每 run 首找，
        # 2026-08-22 实机 r_20260822_015715：首找 prev=None 守卫被跳过，视图残留纳塔
        # 又选了跨区花）→ M/M 复位到玩家中心重检一次。
        prev = getattr(self.ctx, "_shared_pos_prev", None)
        viewport_near_player = (
            viewport is not None
            and isinstance(prev, tuple)
            and len(prev) == 2
            and math.hypot(viewport[0] - prev[0], viewport[1] - prev[1]) <= 3000.0
        )
        if viewport is not None and not viewport_near_player:
            self._observe("detect.blossom", ability="tp", phase="decide",
                          step="view_reset", ok=True,
                          reason="viewport_far_from_player" if viewport is not None
                          else "no_prev_reset_to_player",
                          viewport=None if viewport is None
                          else (round(viewport[0]), round(viewport[1])),
                          prev=prev)
            self.ctx.release_all_keys()
            self.ctx.press(KeyCode.m)  # 关图
            utils.sleep(0.6)
            self.ctx.press(KeyCode.m)  # 重开（以玩家为中心，zoom 保持）
            if not self.wait_scene(Scene.MAP, timeout=8.0):
                return None
            utils.sleep(0.5)
            frame = self.ctx._capture_sc()
            if frame is None:
                return None
            found = mc.find_blossom_on_map(frame)
            if flower_type:
                found = [b for b in found if b.blossom_type == flower_type]
            if not found:
                evidence = self._save_evidence("find_blossom_after_reset_no_match")
                self._observe("detect.blossom", ability="tp", phase="observe",
                              step="find_after_reset", ok=False,
                              reason="no_blossom_after_view_reset",
                              evidence=evidence)
                return None
            blossoms = found
            viewport = pg.get_position_from_big_map(frame)
            zoom = mc.measure_zoom_level(frame) or 3.0
        if viewport is None:
            evidence = self._save_evidence("find_blossom_sift_fail")
            self._observe("detect.blossom", ability="tp", phase="observe",
                          step="sift_viewport", ok=False, reason="sift_failed",
                          blossom_count=len(blossoms),
                          evidence=evidence)
            return None

        # 3. 测量缩放
        zoom = mc.measure_zoom_level(frame)
        if zoom is None:
            zoom = 3.0  # 兜底默认

        # 4. 取最近视口中心的花，转游戏坐标（exclude=失败黑名单：距这些位置
        # <500 单位的花跳过——2026-08-22 实机奥藏山山地花旋转 30s×3 失败走不到，
        # 换花重试比死磕一朵地形差的花划算）
        if exclude:
            def _excluded(b) -> bool:
                gp = mc.screen_to_game(b.screen_x, b.screen_y, viewport, zoom)
                return any(math.hypot(gp[0] - ex[0], gp[1] - ex[1]) < 500.0 for ex in exclude)
            before = len(blossoms)
            blossoms = [b for b in blossoms if not _excluded(b)]
            if before != len(blossoms):
                self._observe("detect.blossom", ability="tp", phase="decide",
                              step="exclude_blacklist", ok=True,
                              excluded=before - len(blossoms), remaining=len(blossoms))
        if not blossoms:
            self._observe("detect.blossom", ability="tp", phase="observe",
                          step="all_excluded", ok=False, reason="all_blossoms_blacklisted")
            return None

        best = blossoms[0]
        game_pos = mc.screen_to_game(best.screen_x, best.screen_y, viewport, zoom)

        # 5. 查最近传送点（排除秘境 Domain —— 地脉花在野外，需走锚点/神像）
        db = TpDatabase()
        candidates = db.find_nearest(game_pos[0], game_pos[1], n=10)
        # 过滤掉 Domain 类型（OneTimeDomain 一次性秘境 / BlessDomain 圣遗物本 / etc）
        nearest = [
            p for p in candidates
            if "Domain" not in p.type and p.type != "Domain"
        ]
        if not nearest:
            self._observe("detect.blossom", ability="tp", phase="observe",
                          step="nearest_tp", ok=False, reason="no_tp_in_db",
                          game_pos=game_pos,
                          candidates=[(p.name, p.type) for p in candidates[:5]])
            return None

        self._observe(
            "action",
            action="find_blossom",
            blossom_type=best.blossom_type,
            pos=f"({game_pos[0]:.0f},{game_pos[1]:.0f})",
            nearest_tp=nearest[0].name,
        )
        return {
            "blossom_type": best.blossom_type,
            "blossom_pos": game_pos,
            "nearest_tp": nearest[0],
            "screen_pos": (best.screen_x, best.screen_y),
        }

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
        result = nav.go_to(pos, tolerance=tolerance, timeout=timeout)
        self._observe("action", action="go_to", target=f"({pos.x:.1f},{pos.y:.1f})")
        return result

    def talk(self, option: str) -> None:
        """选择对话选项（模糊文本匹配，Phase A 实现）。"""
        from abilities.dialog import talk as _talk

        self._token.check() if self._token is not None else None
        _talk(self.ctx, option)
        self._observe("action", action="talk", option=option)

    def talk_skip(self, timeout: float = 30.0) -> bool:
        """跳过对话直到离开 DIALOG 场景（Phase A 实现）。"""
        from abilities.dialog import talk_skip as _skip

        self._token.check() if self._token is not None else None
        ok = _skip(self.ctx, timeout)
        self._observe("action", action="talk_skip")
        return ok

    # ── 战斗（领域能力，Phase C）──

    def has_enemy(self) -> bool:
        """即时血条检测（红色色块）= 战斗态敌人。详见 abilities/fighter.py。"""
        from abilities.fighter import SimpleFighter

        fighter = SimpleFighter(self.ctx, self)
        return self._call(_run(fighter.has_enemy), timeout=10)

    def scan_enemies(self, conf: float | None = None):
        """世界敌人识别（bgi_world ``"enemy identify"``）→ 敌人列表（含发呆态）。

        血条检测只认战斗态；巡逻/扫描用这个才能“看到”发呆的怪。每个元素是
        Detection（x1,y1,x2,y2,score,name,cx,cy,w,h）。"""
        from abilities.fighter import SimpleFighter

        fighter = SimpleFighter(self.ctx, self)
        return self._call(_run(fighter.find_enemies, conf=conf), timeout=15)  # 首次懒加载 ONNX

    def has_enemy_in_world(self, conf: float | None = None) -> bool:
        """屏幕上是否有世界敌人（含发呆态）。"""
        from abilities.fighter import SimpleFighter

        fighter = SimpleFighter(self.ctx, self)
        return self._call(_run(fighter.has_enemy_in_world, conf=conf), timeout=15)

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

    def is_low_hp(self) -> bool:
        """当前角色红血（读 SharedState，auto_eat 守护 150ms 写入）。"""
        return self.runtime.shared.low_hp

    def fight(self, duration_s: float = 30, rotation: list | None = None) -> None:
        """站桩连招（阻塞 duration_s 或敌人清场）。rotation 见 fighter.DEFAULT_ROTATION。"""
        from abilities.fighter import SimpleFighter

        fighter = SimpleFighter(self.ctx, self)
        self._token.check() if self._token is not None else None
        fighter.fight(duration_s, rotation)
        self._observe("action", action="fight", duration=duration_s)

    def fight_until_clear(self, timeout: float = 120) -> bool:
        """战斗到清场（has_enemy 持续 False）或超时。返回是否清场完成。"""
        from abilities.fighter import SimpleFighter

        fighter = SimpleFighter(self.ctx, self)
        self._token.check() if self._token is not None else None
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


async def _run(fn, *args, **kwargs):
    """把同步 abilities 调用包成协程（在 loop 线程执行，avc 安全）。"""
    return fn(*args, **kwargs)
