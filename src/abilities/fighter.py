"""SimpleFighter —— 阶段 C 简化站桩战斗（docs/design/07 §3、设计实现.md §6.4）。

对照 BetterGI AutoFight（``GameTask/AutoFight/``），借鉴其视觉策略，摒弃脚本引擎/复杂调度：

- **敌人检测（战斗态） = 红色血条色块**（``AvatarRecognition.FindBloodBars``）：avc ``IColorDetector``
  BGR(255,90,90) 精确匹配 + 连通域（下沉 avc_opencv）。血条是战斗专属的可靠信号。
- **世界敌人检测（含发呆态） = bgi_world YOLO ``"enemy identify"``**：已实机验证（风起地东 60，
  帧内 265 帧检出 enemy identify×15 / health bar×16，conf 0.74~0.96）。注意类名**带空格**，
  ``detect()`` 的 key 是 ``"enemy identify"``/``"health bar"``，不是下划线。巡逻扫描用 ``find_enemies()``。
- **Q 就绪** = ``q_classify_sim.onnx``（ROI 右下 Q 图标，类别含 ``"energy 1 cd 0"``）。
- **角色识别** = ``avatar_side_classify_sim.onnx``（右侧侧栏 4 头像）。
- **当前出战** = ``AvatarIndexRectList`` 非白块（右侧编号块，白=未出战）。

**不做**（后续增强）：JSON rotation 引擎、元素反应、E 技能 CD 的 OCR、持续索敌转视角。

风格对照 ``navigation/navigator.py`` —— 有状态循环 + 组合子能力 + ``try/finally`` 释放。

⚠️ 拟人化：avc 绑定无 ``setHumanize``，战斗按键的节奏抖动走 ``framework.utils``（与全框架一致）。
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from abilities.detector import Detection, GenshinDetector
from abilities.vision_utils import Rect
from framework import utils
from framework.resources import res

if TYPE_CHECKING:
    from avc._core import KeyCode, MouseButton
    from avc.image import IImageBuffer

    from framework.context import GameContext
    from framework.high_level_api import HighLevelApi


# ── 血条检测常量（对照 BGI AvatarRecognition.FindBloodBars / AutoFightAssets）──

_BLOOD_RGB = (255, 90, 90)  # BGI 单标量 Threshold 转 RGB 后 InRange low==high = 精确匹配
_BLOOD_ROI = (0, 0, 1500, 900)  # 排除右侧角色 UI 区（1080p）
_BLOOD_MIN_AREA = 8  # 连通域最小像素，过滤噪点
_BLOOD_EXCLUDE_X = 200  # 排除左侧 UI（队伍头像红边）
_PRE_AIM = (960, 480)  # 瞄准参考点（屏幕中心，1080p），最近敌人按距此排序

# ── 分类模型 ROI（对照 BGI AutoFightAssets，1080p）──

_Q_ROI = (1748, 914, 137, 137)  # QRectForClassify：右下 Q 技能图标
_Q_READY_KEYWORD = "energy 1 cd 0"  # 能量满且无 CD → 就绪
_Q_COOLDOWN_KEYWORD = "cd 1"  # CD 中
_Q_CONF_THRESH = 0.7  # BGI Avatar.IsBurstReadyByClassify 阈值

_AVATAR_SIDE_ROIS = [  # AvatarSideIconRectList：右侧 4 个头像（单人 4 位）
    (1765, 225, 76, 76), (1765, 315, 76, 76),
    (1765, 410, 76, 76), (1765, 500, 76, 76),
]
_AVATAR_INDEX_ROIS = [  # 右侧 4 个编号“药丸块”：非出战=白底黑字，出战=无药丸(≈空)
    (1859, 248, 28, 48), (1859, 344, 28, 48),
    (1859, 440, 28, 48), (1859, 536, 28, 48),
]
_INDEX_WHITE_GRAY = (251, 255)  # 药丸白底（BGI CountGrayMatColor(251,255)）
_INDEX_BLACK_GRAY = (50, 54)   # 药丸黑字（BGI CountGrayMatColor(50,54)）
_INDEX_ACTIVE_GAP = 0.08       # 出战槽药丸与次弱槽的 w+b 差 ≥ 此值 → 认定出战
_AVATAR_CONF_THRESH = 0.7  # BGI ClassifyAvatarName 阈值（琴/衣装放宽，这里简化统一）

# ── 连招节奏（对照 BGI Avatar.Attack / UseSkill / UseBurst）──

_ATTACK_HOLD_S = 0.05  # 普攻每次按住
_ATTACK_INTERVAL_S = 0.2  # 普攻周期（含 hold ≈ 200ms，BGI Avatar.Attack）
_CHARGE_SEC = 2.0  # 重击按住时长（BGI 默认 1000-2000ms）
_SKILL_HOLD_S = 1.0  # 长按 E（BGI KeyType.Hold = 1s）
_SKILL_TAP_SLEEP = 0.2  # 点 E 后等动画
_BURST_SLEEP = 0.2  # 点 Q 后重检间隔
_BURST_ANIM_SLEEP = 1.5  # 大招动画等待（BGI Sleep(1500)）
_BURST_RETRY = 10  # Q 释放重试上限（BGI Avatar.UseBurst）
_SWITCH_SLOT_HOLD = 0.05  # 切人键按住
_SWITCH_SLEEP = 0.25  # 切人后等到位（BGI Sleep(250)）
_STEP_DEADLINE_CHECK = True  # rotation 每步前查 deadline/敌人

# ── 索敌（转视角找敌，对齐 BGI AutoFightSeek）──
_SEEK_TURN_PX = 60  # 单次水平转视角量（moveBy；实机验证步长/方向）
_SEEK_MAX_TURNS = 8  # 判清场前最多转几次找敌
_SEEK_ALIGN_TOL = 50  # 血条中心与屏幕中心水平偏差阈值（内视为已对准）

# ── 生存检查（对照 BGI AutoEatTrigger + Avatar.ThrowWhenDefeated）──

_LOW_HP_SWITCH_COOLDOWN = 3.0  # 换人冷却（秒），防止频繁切换


# ── 默认连招（站桩通用；rotation = [(action, *args), ...]）──

DEFAULT_ROTATION: list[tuple] = [
    ("attack", 2.4),  # 普攻 ~12 拍
    ("skill", False),  # 点 E
    ("attack", 1.6),
    ("burst",),  # Q（内部判 is_q_ready，没好就跳过）
    ("attack", 2.0),
    ("switch", 2),  # 切 2 号位
    ("attack", 2.0),
    ("skill", False),
    ("switch", 1),  # 切回 1 号位
]


class SimpleFighter:
    """站桩战斗：固定连招 + 血条索敌 + Q 就绪检测 + 切人。

    用法（经 ``g.fight()`` / ``g.fight_until_clear()`` 桥接，或 abilities 内直接实例化）::

        fighter = SimpleFighter(ctx, g)
        fighter.fight_until_clear(timeout=60)
    """

    def __init__(self, ctx: "GameContext", g: "HighLevelApi"):
        self.ctx = ctx
        self.g = g
        # 懒建：模型加载慢，避免不调用识别时白建
        self._q_clf: GenshinDetector | None = None
        self._avatar_clf: GenshinDetector | None = None
        self._world_det: GenshinDetector | None = None
        self._last_hp_switch_time: float = 0.0  # 换人冷却

    # ── 懒加载分类器 ──

    def _q_classifier(self) -> GenshinDetector:
        if self._q_clf is None:
            self._q_clf = GenshinDetector(res.model("q_classify_sim.onnx"))
        return self._q_clf

    def _avatar_classifier(self) -> GenshinDetector:
        if self._avatar_clf is None:
            self._avatar_clf = GenshinDetector(res.model("avatar_side_classify_sim.onnx"))
        return self._avatar_clf

    # ── 世界敌人检测（bgi_world，含发呆怪）──

    def _world_detector(self) -> GenshinDetector:
        if self._world_det is None:
            self._world_det = GenshinDetector(res.model("bgi_world.onnx"), conf=0.3, iou=0.45)
        return self._world_det

    def find_enemies(self, *, conf: float | None = None) -> list[Detection]:
        """世界敌人识别（bgi_world ``"enemy identify"``）→ [Detection, ...]。

        与血条检测的区别：血条只在战斗态显示；``enemy identify`` 识别世界上的怪本体，
        发呆的怪也检得出（巡逻扫描用这个）。类名带空格，勿写成 ``"enemy_identify"``。
        """
        frame = self.ctx.capture()
        if frame is None:
            return []
        dets = self._world_detector().detect(frame, conf=conf)
        return dets.get("enemy identify", [])

    def has_enemy_in_world(self, *, conf: float | None = None) -> bool:
        """屏幕上是否有世界敌人（含发呆态）。"""
        return bool(self.find_enemies(conf=conf))

    # ── 敌人检测（战斗态，血条）──

    def has_enemy(self) -> bool:
        """即时截图 + 血条色块检测。

        可观测性：发 ``detect.blood``（ability=fighter, count, ok=count>0）。
        ``throttle_key`` 1/s 窗口——战斗连招每步调（~5Hz），折叠为周期快照；
        **count=0（ok=False）永不节流**，每次浮现（痛点⑤「怪检测不到」的直接证据）。
        """
        frame = self.ctx.capture()
        if frame is None:
            self.ctx.observe.event("detect.blood", ability="fighter", count=0,
                                   ok=False, reason="no_frame", throttle_key="has_enemy")
            return False
        count = len(detect_blood_bars(frame))
        self.ctx.observe.event("detect.blood", ability="fighter", count=count,
                               ok=count > 0, throttle_key="has_enemy")
        return count > 0

    def find_nearest_enemy(self) -> Rect | None:
        """最近血条框（按距 _PRE_AIM 的 |dx|+|dy| 排序），无则 None。

        可观测性：发 ``detect.blood``（ability=fighter, count, nearest, ok）。
        痛点⑤发源地——区分「真没怪」(count=0) vs「检不到」（血条色块漏检）。
        """
        frame = self.ctx.capture()
        if frame is None:
            self.ctx.observe.event("detect.blood", ability="fighter", count=0,
                                   ok=False, reason="no_frame", throttle_key="find_enemy")
            return None
        bars = detect_blood_bars(frame)
        if not bars:
            self.ctx.observe.event("detect.blood", ability="fighter", count=0,
                                   ok=False, throttle_key="find_enemy")
            return None
        bars.sort(
            key=lambda r: abs(r.cx - _PRE_AIM[0]) + abs(r.cy - _PRE_AIM[1])
        )
        nearest = bars[0]
        self.ctx.observe.event("detect.blood", ability="fighter", count=len(bars),
                               ok=True, nearest=(int(nearest.cx), int(nearest.cy)),
                               throttle_key="find_enemy")
        return nearest

    def seek_enemy(self, max_turns: int = _SEEK_MAX_TURNS) -> Rect | None:
        """转视角索敌：血条不在屏幕中心时 moveBy 旋转找敌，返回最近血条或 None。

        对齐 BGI ``AutoFightSeek``（血条阈值→旋转找敌）；简化：固定步长转，
        不复用 RotaryFactorMapping。水平偏差 > ``_SEEK_ALIGN_TOL`` 才转向血条，
        否则盲转一档继续找。⚠ 旋转方向/步长实机验证。

        可观测性：发 ``fight.enemy``（ability=fighter, turned, ok）——痛点⑤「怪检测不到」：
        转 max_turns 仍无血条 = 嫌疑（真没怪 / 血条色块漏检，看 detect.blood 配合判定）。
        """
        for attempt in range(max_turns):
            bar = self.find_nearest_enemy()
            if bar is None:
                self._rotate_camera(_SEEK_TURN_PX)  # 没看到敌 → 转一档
                continue
            dx = bar.cx - _PRE_AIM[0]
            if abs(dx) > _SEEK_ALIGN_TOL:
                self._rotate_camera(int(dx * 0.1))  # 血条偏右/左 → 转过去（比例实机调）
            self.ctx.observe.event("fight.enemy", ability="fighter", detector="blood",
                                   turned=attempt, ok=True)
            return bar
        self.ctx.observe.event("fight.enemy", ability="fighter", detector="blood",
                               turned=max_turns, ok=False, reason="not_found_after_seek")
        return None

    def _rotate_camera(self, px: int) -> None:
        """水平转视角（moveBy），异常吞掉。"""
        try:
            # 旋转用相对移动（avc moveBy 绝对坐标，原神 raw-input 视角不认）
            self.ctx.move_by_rel(int(px), 0)
        except Exception:
            pass
        utils.sleep(0.1)

    # ── Q 就绪 ──

    def is_q_ready(self) -> bool:
        """q_classify 分类 Q 图标 ROI：类名含 'energy 1 cd 0' 且非 'cd 1' 且置信度≥0.7。"""
        name, score = self._classify_crop(self._q_classifier(), _Q_ROI, label="q")
        if score < _Q_CONF_THRESH:
            return False
        return _Q_READY_KEYWORD in name and _Q_COOLDOWN_KEYWORD not in name

    # ── 当前出战角色（简化连招不强制依赖）──

    def current_avatar(self) -> str | None:
        """返回当前出战角色的英文代号（去 Costume 后缀）；识别失败 None。

        先用 AvatarIndexRectList 找非白块定位出战槽 → 对该槽 AvatarSideIconRect ROI 分类。
        """
        idx = self._active_slot_index()
        if idx is None:
            return None
        name, score = self._classify_crop(
            self._avatar_classifier(), _AVATAR_SIDE_ROIS[idx], label="avatar"
        )
        if score < _AVATAR_CONF_THRESH:
            return None
        base = name.split("Costume")[0].strip()  # BGI ClassifyAvatarCnName 去 Costume
        return base or None

    def _active_slot_index(self) -> int | None:
        """4 个编号块里“药丸最弱”的那个 = 出战槽；无法判定返回 None。

        实机标定：出战槽药丸明显更弱 —— overworld w+b 0.26~0.37（其余 0.43+），
        combat 0.0~0.03（其余 0.38+）。取 argmin + 最小间隙校验，两种场景通用。
        avc：``toGray`` → 各 ROI ``countInRange``(灰度∈[lo,hi] 占比，[0,1])。
        """
        frame = self.ctx.capture()
        if frame is None:
            return None
        gray = frame.toGray()
        if gray is None:
            return None
        pills = [
            gray.countInRange(roi, *_INDEX_WHITE_GRAY)
            + gray.countInRange(roi, *_INDEX_BLACK_GRAY)
            for roi in _AVATAR_INDEX_ROIS
        ]
        order = sorted(range(len(pills)), key=lambda i: pills[i])
        lo, second = pills[order[0]], pills[order[1]]
        if second - lo >= _INDEX_ACTIVE_GAP:
            return order[0]
        return None  # 两个槽药丸强度相近（编队未满/切换中/界面不符）→ 无法判定

    # ── 生存检查（rotation 每步前调用）──

    def _check_survival(self) -> None:
        """战斗中生存检查：红血→吃药/换人，死亡→按 Z 复活。

        读取 shared.low_hp（auto_eat 守护 150ms 写入）+ 直接像素检查兜底。
        守护在后台按 Z，fighter 在工作线程也按 Z——双保险（Z 双按无害，
        便携营养袋有自身 CD）。无药时换人保命（轮询下一槽位）。

        ⚠ 全程 try/except：生存检查不应中断战斗流程（检测失败=不处理，继续打）。
        """
        try:
            self._check_survival_inner()
        except Exception:
            pass  # 检测失败不中断战斗

    def _check_survival_inner(self) -> None:
        from avc._core import KeyCode

        from abilities.game_state import (
            has_recovery_icon,
            has_resurrection_icon,
            is_low_hp,
        )

        # 0. 死亡检测（最高优先）
        frame = self.ctx.capture()
        if frame is not None and has_resurrection_icon(self.ctx, frame):
            self._tap(KeyCode.z, 0.05)
            # 痛点③：战斗中死亡按 Z 复活（与 recover_on_death 的传送复活是两条路径，都静默→补发）
            self.ctx.observe.event("survival.revive", ability="fighter",
                                   resurrection_detected=True, path="combat_z",
                                   action="press_z", ok=False, reason="dead_in_combat")
            utils.sleep(1.0)
            return

        # 1. 红血检测：读共享状态 + 直接像素兜底（两路 source 区分）
        low = False
        source = "shared"
        if self.g.runtime is not None:
            low = self.g.runtime.shared.low_hp
        if not low and frame is not None:
            low = is_low_hp(self.ctx, frame)
            source = "pixel"
        if not low:
            return  # 健康：不发（_check_survival 每步调，避免噪声）

        # 2. 红血 + Recovery 可用 → 按 Z 吃药
        recovery = frame is not None and has_recovery_icon(self.ctx, frame)
        if recovery:
            self._tap(KeyCode.z, 0.05)
            self.ctx.observe.event("survival.check", ability="fighter", low_hp=True,
                                   source=source, recovery_icon=True, action="eat", ok=True)
            utils.sleep(0.3)
            return

        # 3. 无药 → 换人保命（轮询下一槽位，3 秒冷却）
        now = time.monotonic()
        if now - self._last_hp_switch_time < _LOW_HP_SWITCH_COOLDOWN:
            self.ctx.observe.event("survival.check", ability="fighter", low_hp=True,
                                   source=source, recovery_icon=False, action="none",
                                   reason="switch_cooldown", ok=False,
                                   throttle_key="survival:low_nodrug")
            return
        active = self._active_slot_index()
        if active is None:
            self.ctx.observe.event("survival.check", ability="fighter", low_hp=True,
                                   source=source, recovery_icon=False, action="none",
                                   reason="no_slot_detected", ok=False,
                                   throttle_key="survival:low_nodrug")
            return
        for slot in range(1, 5):
            if slot - 1 != active:
                self.switch_character(slot)
                self._last_hp_switch_time = now
                self.ctx.observe.event("survival.check", ability="fighter", low_hp=True,
                                       source=source, recovery_icon=False, action="switch",
                                       slot=slot, ok=True)
                return

    # ── 切人 ──

    def switch_character(self, slot: int) -> None:
        """slot ∈ 1..4。先按 X（GIActions.Drop，防悬空/取消攀爬）→ num{slot}。"""
        from avc._core import KeyCode

        if not 1 <= slot <= 4:
            raise ValueError(f"slot 须 1..4，收到 {slot}")
        self._tap(KeyCode.x, _SWITCH_SLOT_HOLD)
        key = (KeyCode.num1, KeyCode.num2, KeyCode.num3, KeyCode.num4)[slot - 1]
        self._tap(key, _SWITCH_SLOT_HOLD)
        utils.sleep(utils.human_delay(_SWITCH_SLEEP))

    # ── 连招主循环 ──

    def fight(self, duration_s: float = 30, rotation: list | None = None) -> None:
        """站桩连招：在 duration_s 内循环 rotation，敌人消失则提前退出。

        ``finally`` 释放所有按住的键 + 鼠标（含 ``release_all_keys`` 漏掉的鼠标/num 键）。
        """
        rotation = rotation if rotation is not None else DEFAULT_ROTATION
        self.ctx.ensure_foreground()  # 战斗全程 ic 直调，开头保证前台
        deadline = time.monotonic() + duration_s
        try:
            while time.monotonic() < deadline:
                if not self.has_enemy():
                    break  # 敌人没了，提前退出
                for step in rotation:
                    if time.monotonic() >= deadline:
                        break
                    self._check_survival()  # 每步前查血量/死亡
                    self._exec_step(step)
                    if _STEP_DEADLINE_CHECK and not self.has_enemy():
                        break
        finally:
            self._release_everything()

    def pick_drops(self, timeout: float = 8.0) -> int:
        """战斗后拾取掉落：等宝箱/花 F 图标→按 F，窗口内计数（简化版，无 YOLO 光束）。

        对齐 BGI ``ScanPickTask`` 的简化：不扫掉落光束，只按交互图标按 F；
        auto_pick 守护可覆盖多数掉落，这里是任务侧兜底。⚠ 实机验证图标命中。
        """
        from abilities.game_state import has_chest_f_icon, has_flower_f_icon
        from avc._core import KeyCode

        picked = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.ctx.capture()
            if frame is not None and (
                has_chest_f_icon(self.ctx, frame)
                or has_flower_f_icon(self.ctx, frame)
            ):
                try:
                    self.ctx.ic.press(KeyCode.f, 30)
                except Exception:
                    pass
                picked += 1
                utils.sleep(0.5)
            else:
                utils.sleep(0.2)
        return picked

    def _fight_finished(self) -> bool:
        """战斗结束判定钩子：默认血条消失（对齐 BGI CheckFightFinish 的简化；
        其"打开队伍页采样黄进度条+白块"留作实机增强，子类可覆写收紧）。"""
        return not self.has_enemy()

    def recover_on_death(self) -> bool:
        """检测死亡/复活弹窗；死亡→传送到最近七天神像复活→抛 ``Retry``。返回 False=无需处理。

        对齐 BGI ``Avatar.ThrowWhenDefeated`` → ``TpForRecover``：不点复活按钮，
        直接传七天神像（传送即复活）。战斗循环每轮开头调。优先按当前位置找最近神像，
        位置不可用时回退"七天神像-风"。

        可观测性：发 ``survival.revive``（ability=fighter, goddess_target, teleport_ok, ok）。
        痛点③「死了没复活」：resurrection 检到但 teleport_ok=False 或女神 None = 嫌疑。
        """
        from abilities.game_state import has_resurrection_icon
        from framework.errors import Retry

        frame = self.ctx.capture()
        if frame is None or not has_resurrection_icon(self.ctx, frame):
            return False
        target = self._find_nearest_goddess()
        teleport_ok = False
        try:
            self.g.teleport_to(target if target is not None else "七天神像-风")
            teleport_ok = True
        except Exception as e:
            self.ctx.observe.event("survival.revive", ability="fighter",
                                   resurrection_detected=True, path="teleport_goddess",
                                   goddess_target=target, teleport_ok=False, ok=False,
                                   reason="teleport_fail", detail=repr(e))
        if teleport_ok:
            self.ctx.observe.event("survival.revive", ability="fighter",
                                   resurrection_detected=True, path="teleport_goddess",
                                   goddess_target=target, teleport_ok=True, ok=True,
                                   action="teleport_goddess")
        raise Retry(reason="角色死亡，传送七天神像复活后重试")

    def _find_nearest_goddess(self) -> str | None:
        """按当前位置找最近的七天神像传送点名称；位置不可用返回 None。"""
        from abilities.navigation.position import PositionGetter
        from abilities.navigation.tp import TpDatabase

        try:
            pg = PositionGetter(self.ctx)
            pos = pg.get_position()
        except Exception:
            return None
        if pos is None:
            return None
        db = TpDatabase()
        goddesses = db.find_by_type("Goddess")
        if not goddesses:
            return None
        x, y = pos
        goddesses.sort(key=lambda p: math.hypot(p.x - x, p.y - y))
        return goddesses[0].name

    def fight_until_clear(
        self, timeout: float = 120, clear_stable_s: float = 1.5
    ) -> bool:
        """循环到 ``has_enemy`` 持续 False ``clear_stable_s`` 秒（清场）或超时。

        返回 True=清场完成；False=超时。内部直调 ``self.fight``（不经 g.* 桥，无 timeout 嵌套）。
        每轮先查死亡（recover_on_death，死亡抛 Retry）；敌人消失未清场先索敌再判。
        """
        deadline = time.monotonic() + timeout
        last_seen = time.monotonic()
        seek_turns = 0
        try:
            while time.monotonic() < deadline:
                if self.recover_on_death():
                    continue  # 已处理死亡（实际会抛 Retry，不会走到这）
                if self.has_enemy():
                    last_seen = time.monotonic()
                    seek_turns = 0
                    self.fight(duration_s=min(8.0, max(0.1, deadline - time.monotonic())))
                elif self._fight_finished() and time.monotonic() - last_seen >= clear_stable_s:
                    return True
                else:
                    # 敌人消失但未稳定清场：先转视角找敌，别急着判清场（对齐 BGI 索敌）
                    if seek_turns < _SEEK_MAX_TURNS:
                        seek_turns += 1
                        if self.seek_enemy(max_turns=1) is not None:
                            last_seen = time.monotonic()
                    else:
                        utils.sleep(0.1)
        finally:
            self._release_everything()
        return False

    # ── 私有：rotation 步骤派发 ──

    def _exec_step(self, step: tuple) -> None:
        action = step[0]
        if action == "attack":
            self._attack(step[1])
        elif action == "charge":
            self._charge(step[1])
        elif action == "skill":
            self._use_skill(step[1])
        elif action == "burst":
            self._use_burst()
        elif action == "switch":
            self.switch_character(step[1])
        elif action == "wait":
            utils.sleep(utils.human_delay(step[1]))
        else:
            raise ValueError(f"未知连招动作: {action}")

    # ── 私有：动作原语 ──

    def _attack(self, duration_s: float) -> None:
        """普攻：左键连按，周期 ~200ms（BGI Avatar.Attack: click + sleep 200）。"""
        from avc._core import MouseButton

        end = time.monotonic() + duration_s
        while time.monotonic() < end:
            self._tap_mouse(MouseButton.left, _ATTACK_HOLD_S)
            utils.sleep(utils.human_delay(_ATTACK_INTERVAL_S))

    def _charge(self, sec: float = _CHARGE_SEC) -> None:
        """重击：按住左键 sec 秒（BGI Avatar.ChargedAttack ~1-2s）。"""
        from avc._core import MouseButton

        self._hold_mouse(MouseButton.left, sec)

    def _use_skill(self, hold: bool = False) -> None:
        """E 元素战记：点按或长按（hold=True，~1s，BGI KeyType.Hold）。"""
        from avc._core import KeyCode

        ic = self.ctx.ic
        if hold:
            try:
                ic.keyDown(KeyCode.e)
            except Exception:
                pass
            utils.sleep(utils.human_delay(_SKILL_HOLD_S))
            try:
                ic.keyUp(KeyCode.e)
            except Exception:
                pass
        else:
            self._tap(KeyCode.e, _SWITCH_SLOT_HOLD)
        utils.sleep(utils.human_delay(_SKILL_TAP_SLEEP))

    def _use_burst(self) -> None:
        """Q 元素爆发：先检 is_q_ready；点按 Q 重试至检测到 CD 或重试耗尽。

        没好（is_q_ready=False）直接跳过；释放后等大招动画 1.5s。

        可观测性：发 ``fight.burst``（ability=fighter, q_ready, attempts, ok, reason）——
        诊断「Q 好了却没放大」(q_not_ready) vs「放了没进 CD」(no_cd_after_retry)。
        """
        from avc._core import KeyCode

        if not self.is_q_ready():
            self.ctx.observe.event("fight.burst", ability="fighter", q_ready=False,
                                   ok=False, reason="q_not_ready")
            return
        for attempt in range(_BURST_RETRY):
            self._tap(KeyCode.q, _SWITCH_SLOT_HOLD)
            utils.sleep(_BURST_SLEEP)
            if not self.is_q_ready():  # 进入 CD → 释放成功
                utils.sleep(_BURST_ANIM_SLEEP)
                self.ctx.observe.event("fight.burst", ability="fighter", q_ready=True,
                                       attempts=attempt + 1, ok=True)
                return
        self.ctx.observe.event("fight.burst", ability="fighter", q_ready=True,
                               attempts=_BURST_RETRY, ok=False, reason="no_cd_after_retry")

    # ── 私有：底层输入 helper ──

    def _tap(self, key, hold_s: float = 0.0) -> None:
        """点键。战斗要响应快，走 ic.press 直发；hold 抖动用 utils.jitter。"""
        hold_ms = int(utils.jitter(hold_s * 1000)) if hold_s > 0 else 0
        try:
            self.ctx.ic.press(key, max(0, hold_ms))
        except Exception:
            pass

    def _tap_mouse(self, button, hold_s: float = 0.05) -> None:
        ic = self.ctx.ic
        try:
            ic.mouseDown(button)
            utils.sleep(utils.jitter(hold_s))
            ic.mouseUp(button)
        except Exception:
            try:
                ic.mouseUp(button)
            except Exception:
                pass

    def _hold_mouse(self, button, sec: float) -> None:
        """按住鼠标 sec 秒（重击用；click_at 的 45-90ms 远不够）。"""
        ic = self.ctx.ic
        try:
            ic.mouseDown(button)
            utils.sleep(utils.human_delay(sec))
        finally:
            try:
                ic.mouseUp(button)
            except Exception:
                pass

    def _release_everything(self) -> None:
        """异常/正常出口兜底：mouseUp 鼠标 + release_all_keys + 补战斗键。

        补 ``release_all_keys`` 的两个坑：① 不释放鼠标按键 ② 不含 num1-4/x/z。
        每步 try/except 吞异常，避免 finally 抛错掩盖原异常。
        """
        from avc._core import MouseButton, KeyCode

        # 1. 鼠标（release_all_keys 不释放）
        for btn in (MouseButton.left, MouseButton.right, MouseButton.middle):
            try:
                self.ctx.ic.mouseUp(btn)
            except Exception:
                pass
        # 2. release_all_keys（w/a/s/d/space/shift/ctrl/alt/e/q/f）
        try:
            self.ctx.release_all_keys()
        except Exception:
            pass
        # 3. release_all_keys 漏掉的战斗键
        for k in (KeyCode.num1, KeyCode.num2, KeyCode.num3, KeyCode.num4,
                  KeyCode.x, KeyCode.z):
            try:
                self.ctx.ic.keyUp(k)
            except Exception:
                pass

    # ── 私有：分类模型推理（精确 resize，匹配 BGI YoloSharp 分类预处理）──

    def _classify_crop(
        self, clf: GenshinDetector, roi: tuple[int, int, int, int], label: str = "?"
    ) -> tuple[str, float]:
        """对 frame 的 roi 区域做分类，返回 (类名, 分数)。

        avc：``frame.crop`` 得 IImageBuffer → ``clf.classify``（avc ``classify`` 用
        exact resize，对齐 BGI YoloSharp ``Classify`` 训练分布）。

        可观测性：发 ``detect.classify``（ability=fighter, model=label, name, score, ok）。
        ``throttle_key`` 1/s 窗口——_use_burst 重试循环（最多 10 次）折叠为周期快照。
        """
        frame = self.ctx.capture()
        if frame is None:
            self.ctx.observe.event("detect.classify", ability="fighter", model=label,
                                   ok=False, reason="no_frame", throttle_key=f"clf:{label}")
            return "", 0.0
        crop = frame.crop(*roi)
        if crop is None:
            self.ctx.observe.event("detect.classify", ability="fighter", model=label,
                                   ok=False, reason="crop_fail", throttle_key=f"clf:{label}")
            return "", 0.0
        name, score = clf.classify(crop)
        self.ctx.observe.event("detect.classify", ability="fighter", model=label,
                               name=name, score=score, ok=True, throttle_key=f"clf:{label}")
        return name, score


# ── 模块级纯函数（供 game_state.py 单向 import，避免类循环依赖）──


def detect_blood_bars(frame: "IImageBuffer") -> list[Rect]:
    """frame（avc IImageBuffer）→ 血条框列表（截图缓冲坐标系）。

    avc ``IColorDetector``（avc_opencv；BGR ``inRange``(255,90,90) 精确匹配 + 8-连通
    + setMinArea/setRoi + 左侧 UI 过滤）。avc 是硬依赖，不回退 cv2。
    """
    cd = _get_blood_detector()
    out: list[Rect] = []
    for i in range(cd.detect(frame)):
        r = cd.getRegion(i)
        if r is None or r.x <= _BLOOD_EXCLUDE_X:  # 排除左侧 UI（队伍头像红边）
            continue
        out.append(Rect(r.x, r.y, r.w, r.h))
    return out


_BLOOD_CD = None  # 懒建的 avc IColorDetector


def _get_blood_detector():
    """懒建并缓存 avc ``IColorDetector``（配好血条色/ROI/minArea）。

    avc 是硬依赖：``avc_opencv`` 插件未加载 → raise（不回退 cv2）。血条
    RGB(255,90,90) → BGR(90,90,255)，精确匹配（BGI 单标量阈值 low==high）。
    """
    global _BLOOD_CD
    if _BLOOD_CD is not None:
        return _BLOOD_CD
    from avc import Vision
    from avc._core import ColorSpace

    cd = Vision.createColorDetector()
    if cd is None:
        raise RuntimeError("avc IColorDetector 不可用（avc_opencv 插件未加载）；不回退 cv2。")
    cd.setColorSpace(ColorSpace.bgr)
    cd.setRange(90, 90, 255, 90, 90, 255)  # BGR; 精确匹配
    cd.setRoi(*_BLOOD_ROI)
    cd.setMinArea(_BLOOD_MIN_AREA)
    _BLOOD_CD = cd
    return cd


def has_enemy_in_frame(frame: "IImageBuffer") -> bool:
    """帧内是否有血条（供 SceneEstimator / game_state 判 COMBAT 场景用）。"""
    return bool(detect_blood_bars(frame))
