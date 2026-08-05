"""SimpleFighter —— 阶段 C 简化站桩战斗（docs/design/07 §3、设计实现.md §6.4）。

对照 BetterGI AutoFight（``GameTask/AutoFight/``），借鉴其视觉策略，摒弃脚本引擎/复杂调度：

- **敌人检测 = 红色血条色块**（``AvatarRecognition.FindBloodBars``）：``cv2.inRange``
  RGB(255,90,90) 精确匹配 + ``connectedComponents``。不用 bgi_world YOLO（其是否含稳定
  “敌人”类未验证）；血条是战斗专属的可靠信号。
- **Q 就绪** = ``q_classify_sim.onnx``（ROI 右下 Q 图标，类别含 ``"energy 1 cd 0"``）。
- **角色识别** = ``avatar_side_classify_sim.onnx``（右侧侧栏 4 头像）。
- **当前出战** = ``AvatarIndexRectList`` 非白块（右侧编号块，白=未出战）。

**不做**（后续增强）：JSON rotation 引擎、元素反应、E 技能 CD 的 OCR、持续索敌转视角。

风格对照 ``navigation/navigator.py`` —— 有状态循环 + 组合子能力 + ``try/finally`` 释放。

⚠️ 拟人化：avc 绑定无 ``setHumanize``，战斗按键的节奏抖动走 ``framework.utils``（与全框架一致）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import cv2
import numpy as np

from abilities.detector import GenshinDetector
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
_AVATAR_INDEX_ROIS = [  # AvatarIndexRectList：右侧 4 个编号块（白=未出战）
    (1859, 256, 28, 24), (1859, 352, 28, 24),
    (1859, 448, 28, 24), (1859, 544, 28, 24),
]
_INDEX_WHITE_GRAY = (251, 255)  # 灰度落在此区间视为“白”（BGI CountGrayMatColor）
_INDEX_WHITE_RATIO = 0.5  # 白占比 > 此值 → 该位未出战
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
_SEEK_TURN_PX = 60  # 单次水平转视角量（MoveMouseBy；实机验证步长/方向）
_SEEK_MAX_TURNS = 8  # 判清场前最多转几次找敌
_SEEK_ALIGN_TOL = 50  # 血条中心与屏幕中心水平偏差阈值（内视为已对准）


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

    # ── 懒加载分类器 ──

    def _q_classifier(self) -> GenshinDetector:
        if self._q_clf is None:
            self._q_clf = GenshinDetector(res.model("q_classify_sim.onnx"))
        return self._q_clf

    def _avatar_classifier(self) -> GenshinDetector:
        if self._avatar_clf is None:
            self._avatar_clf = GenshinDetector(res.model("avatar_side_classify_sim.onnx"))
        return self._avatar_clf

    # ── 敌人检测 ──

    def has_enemy(self) -> bool:
        """即时截图 + 血条色块检测。"""
        frame = self.ctx.capture()
        if frame is None:
            return False
        return has_enemy_in_frame(frame)

    def find_nearest_enemy(self) -> Rect | None:
        """最近血条框（按距 _PRE_AIM 的 |dx|+|dy| 排序），无则 None。"""
        frame = self.ctx.capture()
        if frame is None:
            return None
        bars = detect_blood_bars(frame)
        if not bars:
            return None
        bars.sort(
            key=lambda r: abs(r.cx - _PRE_AIM[0]) + abs(r.cy - _PRE_AIM[1])
        )
        return bars[0]

    def seek_enemy(self, max_turns: int = _SEEK_MAX_TURNS) -> Rect | None:
        """转视角索敌：血条不在屏幕中心时 MoveMouseBy 旋转找敌，返回最近血条或 None。

        对齐 BGI ``AutoFightSeek``（血条阈值→旋转找敌）；简化：固定步长转，
        不复用 RotaryFactorMapping。水平偏差 > ``_SEEK_ALIGN_TOL`` 才转向血条，
        否则盲转一档继续找。⚠ 旋转方向/步长实机验证。
        """
        for _ in range(max_turns):
            bar = self.find_nearest_enemy()
            if bar is None:
                self._rotate_camera(_SEEK_TURN_PX)  # 没看到敌 → 转一档
                continue
            dx = bar.cx - _PRE_AIM[0]
            if abs(dx) > _SEEK_ALIGN_TOL:
                self._rotate_camera(int(dx * 0.1))  # 血条偏右/左 → 转过去（比例实机调）
            return bar
        return None

    def _rotate_camera(self, px: int) -> None:
        """水平转视角（MoveMouseBy），异常吞掉。"""
        try:
            self.ctx.ic.moveMouseBy(int(px), 0)
        except Exception:
            pass
        utils.sleep(0.1)

    # ── Q 就绪 ──

    def is_q_ready(self) -> bool:
        """q_classify 分类 Q 图标 ROI：类名含 'energy 1 cd 0' 且非 'cd 1' 且置信度≥0.7。"""
        name, score = self._classify_crop(self._q_classifier(), _Q_ROI)
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
            self._avatar_classifier(), _AVATAR_SIDE_ROIS[idx]
        )
        if score < _AVATAR_CONF_THRESH:
            return None
        base = name.split("Costume")[0].strip()  # BGI ClassifyAvatarCnName 去 Costume
        return base or None

    def _active_slot_index(self) -> int | None:
        """AvatarIndexRectList 4 个编号块里第一个“非白”= 出战槽；全白返回 None。"""
        frame = self.ctx.capture()
        if frame is None:
            return None
        bgr = _buffer_to_bgr(frame)
        if bgr is None:
            return None
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        lo, hi = _INDEX_WHITE_GRAY
        for i, (x, y, w, h) in enumerate(_AVATAR_INDEX_ROIS):
            block = gray[y : y + h, x : x + w]
            # 白块占比 = 灰度落在 [lo, hi] 的像素比例（BGI CountGrayMatColor(251,255)）
            white_ratio = float(((block >= lo) & (block <= hi)).sum()) / block.size
            if white_ratio < _INDEX_WHITE_RATIO:
                return i
        return None

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
        deadline = time.monotonic() + duration_s
        try:
            while time.monotonic() < deadline:
                if not self.has_enemy():
                    break  # 敌人没了，提前退出
                for step in rotation:
                    if time.monotonic() >= deadline:
                        break
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
        """检测死亡/复活弹窗；死亡→传送到七天神像复活→抛 ``Retry``。返回 False=无需处理。

        对齐 BGI ``Avatar.ThrowWhenDefeated`` → ``TpForRecover``：不点复活按钮，
        直接传七天神像（传送即复活）。战斗循环每轮开头调。⚠ 传送点名为骨架值
        （"七天神像-风"），实机按所在国换最近神像。
        """
        from abilities.game_state import has_resurrection_icon
        from framework.errors import Retry

        frame = self.ctx.capture()
        if frame is None or not has_resurrection_icon(self.ctx, frame):
            return False
        try:
            self.g.teleport_to("七天神像-风")
        except Exception:
            pass  # 传送失败也重试（可能已在神像旁）
        raise Retry(reason="角色死亡，传送七天神像复活后重试")

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
        """
        from avc._core import KeyCode

        if not self.is_q_ready():
            return
        for _ in range(_BURST_RETRY):
            self._tap(KeyCode.q, _SWITCH_SLOT_HOLD)
            utils.sleep(_BURST_SLEEP)
            if not self.is_q_ready():  # 进入 CD → 释放成功
                utils.sleep(_BURST_ANIM_SLEEP)
                return

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
        self, clf: GenshinDetector, roi: tuple[int, int, int, int]
    ) -> tuple[str, float]:
        """对 frame 的 roi 区域做分类，返回 (类名, 分数)。

        ⚠️ 不用 ``clf.classify``（其 ``_preprocess`` 用 letterbox，会给分类图加黑边，
        与 BGI YoloSharp ``Classify`` 的「精确 resize」训练分布不一致，置信度可能偏低）。
        这里直接 resize 到 imgsz×imgsz 再推理（/255 + NCHW + argmax）。
        """
        frame = self.ctx.capture()
        if frame is None:
            return "", 0.0
        bgr = _buffer_to_bgr(frame)
        if bgr is None:
            return "", 0.0
        x, y, w, h = roi
        crop = bgr[y : y + h, x : x + w]
        rgb = crop[:, :, ::-1]
        resized = cv2.resize(rgb, (clf.imgsz, clf.imgsz), interpolation=cv2.INTER_LINEAR)
        tensor = np.ascontiguousarray(
            resized.astype(np.float32).transpose(2, 0, 1)[None] / 255.0
        )
        out = clf.session.run([clf.out_name], {clf.in_name: tensor})[0].flatten()
        top = int(out.argmax())
        return clf.name(top), float(out[top])


# ── 模块级纯函数（供 game_state.py 单向 import，避免类循环依赖）──


def detect_blood_bars(frame: "IImageBuffer") -> list[Rect]:
    """frame → 血条框列表（截图缓冲坐标系）。

    优先走 avc ``IColorDetector``（C++，下沉到 avc_opencv；运行时 frame 为 avc
    ``IImageBuffer``）；无 avc / 插件未装 / frame 非 avc buffer（如单测 FakeBuffer）时
    回退纯 cv2。两侧逻辑等价：``inRange``(血条色) + 连通域 + 过滤(面积/左侧 UI)。
    ⚠ avc 侧 8-连通、cv2 侧 4-连通；对血条这种孤立横条几乎无差别。
    """
    cd = _get_blood_detector()
    if cd is not None and hasattr(frame, "_native"):
        out: list[Rect] = []
        for i in range(cd.detect(frame)):
            r = cd.getRegion(i)
            if r is None:
                continue
            if r.x <= _BLOOD_EXCLUDE_X:  # 排除左侧 UI（队伍头像红边）
                continue
            out.append(Rect(r.x, r.y, r.w, r.h))
        return out
    return _detect_blood_bars_cv2(frame)


_BLOOD_CD = None  # 懒建的 avc IColorDetector（无 avc / 插件未装则保持 None）


def _get_blood_detector():
    """懒建并缓存 avc ``IColorDetector``（配好血条色/ROI/minArea）。

    无 avc 或 avc_opencv 插件未加载时返回 None（调用方回退到纯 cv2）。
    血条 RGB(255,90,90) → BGR(90,90,255)，精确匹配（BGI 单标量阈值 low==high）。
    """
    global _BLOOD_CD
    if _BLOOD_CD is not None:
        return _BLOOD_CD
    try:
        from avc import Vision
        from avc._core import ColorSpace

        cd = Vision.createColorDetector()
        if cd is None:  # avc_opencv 插件未加载 → 降级
            return None
        cd.setColorSpace(ColorSpace.bgr)
        cd.setRange(90, 90, 255, 90, 90, 255)  # BGR; 精确匹配
        cd.setRoi(*_BLOOD_ROI)
        cd.setMinArea(_BLOOD_MIN_AREA)
        _BLOOD_CD = cd
        return cd
    except Exception:
        return None


def _detect_blood_bars_cv2(frame: "IImageBuffer") -> list[Rect]:
    """纯 Python cv2 回退（无 avc 时；逻辑同 avc 侧，4-连通）。

    BGI ``AvatarRecognition.FindBloodBars`` 的 Python 等价：
    BGRA→BGR→RGB → ``inRange(RGB(255,90,90), RGB(255,90,90))`` 精确匹配
    → ``connectedComponentsWithStats`` → 过滤（面积/左侧 UI）。
    """
    bgr = _buffer_to_bgr(frame)
    if bgr is None:
        return []
    x0, y0, rw, rh = _BLOOD_ROI
    roi = bgr[y0 : y0 + rh, x0 : x0 + rw]
    rgb = roi[:, :, ::-1]
    mask = cv2.inRange(
        rgb, np.array(_BLOOD_RGB, dtype=np.uint8), np.array(_BLOOD_RGB, dtype=np.uint8)
    )
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(
        mask, connectivity=4, ltype=cv2.CV_32S
    )
    out: list[Rect] = []
    for i in range(1, n):  # 0 是背景
        x, y, w, h, area = stats[i]
        if area < _BLOOD_MIN_AREA:
            continue
        gx, gy = int(x) + x0, int(y) + y0  # 还原到全图坐标
        if gx <= _BLOOD_EXCLUDE_X:  # 排除左侧 UI（队伍头像红边）
            continue
        out.append(Rect(gx, gy, int(w), int(h)))
    return out


def has_enemy_in_frame(frame: "IImageBuffer") -> bool:
    """帧内是否有血条（供 SceneEstimator / game_state 判 COMBAT 场景用）。"""
    return bool(detect_blood_bars(frame))


def _buffer_to_bgr(frame) -> np.ndarray | None:
    """avc IImageBuffer（默认 BGRA8）→ HxWx3 BGR ndarray；失败 None。

    avc 截图默认 BGRA8（CLAUDE §5）；取前 3 通道即 BGR。不借 detector._to_rgb_np
    以保持解耦（后者按 imageType 分派，这里按默认格式简化）。
    """
    try:
        raw = bytes(frame.to_bytes())
        h, w = frame.height, frame.width
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, -1)
        return arr[:, :, :3]
    except Exception:
        return None
