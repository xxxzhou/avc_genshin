"""场景特征检测原语（docs/design/02 §1、Phase A）。

可组合的特征函数，供 ``make_classifier`` 组装为场景分类器。每个函数检测一个 UI 特征
（模板匹配 / 像素颜色 / OCR），返回 bool。组合判场景 → 注册为 ``set_classifier()`` 的
真实分类器（替代永远返回 UNKNOWN 的默认分类器）。

对照 BetterGI ``BvStatus.cs``：
- ``IsInMainUi`` → ``has_paimon_menu``
- ``IsInTalkUi`` → ``has_disabled_ui_btn``
- ``IsInBigMapUi`` → ``has_map_scale_btn``
- ``IsInDomain`` → ``has_in_domain``
- ``CurrentAvatarIsLowHp`` → ``is_low_hp``
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from framework.resources import res
from framework.scene import Scene, SceneClassifier, SceneState

if TYPE_CHECKING:
    from avc.image import IImageBuffer

    from framework.context import GameContext


# ── 模板路径常量（经 res.template_* 解析，支持 BGI 回退）──

_TPL = {
    # UI
    "paimon_menu": ("template_ui", "paimon_menu.png"),
    "in_domain": ("template_ui", "in_domain.png"),
    "key_space": ("template_ui", "key_space.png"),
    "key_x": ("template_ui", "key_x.png"),
    "btn_black_confirm": ("template_ui", "btn_black_confirm.png"),
    "btn_white_confirm": ("template_ui", "btn_white_confirm.png"),
    "page_close_white": ("template_ui", "page_close_white.png"),
    "prompt_dialog_star": ("template_ui", "prompt_dialog_left_bottom_star.png"),
    "party_btn_choose_view": ("template_ui", "party_btn_choose_view.png"),
    # Dialog
    "disabled_ui": ("template_dialog", "disabled_ui.png"),
    "icon_option": ("template_dialog", "icon_option.png"),
    "icon_exclamation": ("template_dialog", "icon_exclamation.png"),
    "page_close": ("template_dialog", "page_close.png"),
    "page_close_main": ("template_dialog", "page_close_main.png"),
    "hangout_skip": ("template_dialog", "hangout_skip.png"),
    "primogem": ("template_dialog", "primogem.png"),
    # Pick
    "pick_f": ("template_pick", "F.png"),
    "pick_e": ("template_pick", "E.png"),
    "pick_g": ("template_pick", "G.png"),
    # Eat
    "recovery": ("template_eat", "Recovery.png"),
    "resurrection": ("template_eat", "Resurrection.png"),
    # Chest
    "chest_f": ("template_chest", "chest_F_icon.png"),
    "flower_f": ("template_chest", "flower_F_icon.png"),
    "chest_icon": ("template_chest", "chest.png"),
    # Teleport
    "map_scale_btn": ("template_teleport", "MapScaleButton.png"),
    "map_close_btn": ("template_teleport", "MapCloseButton.png"),
    "map_settings_btn": ("template_teleport", "MapSettingsButton.png"),
    "go_teleport": ("template_teleport", "GoTeleport.png"),
    "teleport_waypoint": ("template_teleport", "TeleportWaypoint.png"),
    "domain_teleport": ("template_teleport", "Domain.png"),
    "map_choose": ("template_teleport", "MapChoose.png"),
    # Loading
    "enter_game": ("template_loading", "enter_game.png"),
    "girl_moon": ("template_loading", "girl_moon.png"),
}


def _tpl_path(key: str) -> str:
    """解析模板路径（缓存避免重复查找）。"""
    shortcut, name = _TPL[key]
    resolver = getattr(res, shortcut)
    return str(resolver(name))


# ── 模板匹配辅助 ──


def _find_template(
    ctx: "GameContext",
    tpl_key: str,
    frame: "IImageBuffer | None" = None,
    threshold: float = 0.8,
    roi: tuple[int, int, int, int] | None = None,
) -> bool:
    """单模板匹配：存在返回 True。"""
    if ctx.tm is None:
        return False
    buf = frame if frame is not None else ctx.capture()
    if buf is None:
        return False
    ctx.tm.clearTemplates()
    if roi:
        ctx.tm.setRoi(*roi)
    else:
        ctx.tm.clearRoi()
    path = _tpl_path(tpl_key)
    if ctx.tm.addTemplatePath(path, threshold) < 0:
        return False
    return ctx.tm.match(buf) > 0


# ── 像素检测辅助 ──


def _pixel_bgra(frame: "IImageBuffer", x: int, y: int) -> tuple[int, int, int, int]:
    """取帧 (x, y) 处 BGRA8 像素。返回 (B, G, R, A)。"""
    raw = frame.to_bytes()
    w = frame.width
    offset = (y * w + x) * 4
    if offset + 4 > len(raw):
        return (0, 0, 0, 0)
    b, g, r, a = struct.unpack_from("BBBB", raw, offset)
    return (b, g, r, a)


def _near_black_ratio(frame: "IImageBuffer", y_start: int = 0, y_end: int | None = None) -> float:
    """计算帧中近黑像素（BGR 均 < 10）占比。用于加载界面检测。"""
    raw = frame.to_bytes()
    w, h = frame.width, frame.height
    if y_end is None:
        y_end = h
    total = 0
    black = 0
    for y in range(y_start, min(y_end, h)):
        for x in range(0, w, 4):  # 采样：每 4 像素取 1
            offset = (y * w + x) * 4
            if offset + 3 >= len(raw):
                continue
            b, g, r = struct.unpack_from("BBB", raw, offset)
            total += 1
            if b < 10 and g < 10 and r < 10:
                black += 1
    return black / max(total, 1)


# ── 场景特征原语 ──


def has_paimon_menu(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """主界面：左上角派蒙菜单图标（BGI ``IsInMainUi``）。"""
    return _find_template(ctx, "paimon_menu", frame, roi=(0, 0, 200, 150))


def has_disabled_ui_btn(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """对话中：左上角"自动"禁用按钮（BGI ``IsInTalkUi``）。"""
    return _find_template(ctx, "disabled_ui", frame, roi=(0, 0, 200, 150))


def has_map_scale_btn(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """大地图：缩放按钮（BGI ``IsInBigMapUi``）。"""
    return _find_template(ctx, "map_scale_btn", frame)


def has_map_settings_btn(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """大地图：设置按钮（BGI ``IsInBigMapUi`` 备选）。"""
    return _find_template(ctx, "map_settings_btn", frame)


def has_in_domain(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """秘境内：右上角秘境图标（BGI ``IsInDomain``，需排除全白）。"""
    if not _find_template(ctx, "in_domain", frame, roi=(1700, 0, 220, 100)):
        return False
    # BGI 逻辑：若匹配区域全白则视为不在秘境
    buf = frame if frame is not None else ctx.capture()
    if buf is None:
        return False
    # 采样匹配区域中心几个点
    for dx, dy in [(0.5, 0.5), (0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)]:
        px = 1700 + int(220 * dx)
        py = int(100 * dy)
        b, g, r, _ = _pixel_bgra(buf, min(px, buf.width - 1), min(py, buf.height - 1))
        if not (b >= 240 and g >= 240 and r >= 240):
            return True  # 有非白像素 → 确实在秘境
    return False  # 全白 → 不在秘境


def is_loading_screen(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """加载界面：近黑帧占比 > 90% 或检测到加载 UI 元素。"""
    buf = frame if frame is not None else ctx.capture()
    if buf is None:
        return False
    # 方法一：检测加载 UI 模板
    if _find_template(ctx, "enter_game", buf):
        return True
    if _find_template(ctx, "girl_moon", buf):
        return True
    # 方法二：帧中间 1/3 近黑占比 > 90%
    h = buf.height
    ratio = _near_black_ratio(buf, h // 3, h * 2 // 3)
    return ratio > 0.90


def is_low_hp(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """当前角色红血：像素 (808, 1010) 为红色 (B=90, G=90, R=255)（BGI ``CurrentAvatarIsLowHp``）。"""
    buf = frame if frame is not None else ctx.capture()
    if buf is None:
        return False
    if buf.width < 810 or buf.height < 1012:
        return False
    b, g, r, _ = _pixel_bgra(buf, 808, 1010)
    return r >= 240 and r <= 255 and g >= 80 and g <= 100 and b >= 80 and b <= 100


def has_recovery_icon(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """便携营养袋可用（Recovery 图标存在，BGI ``CheckRecovery``）。"""
    return _find_template(ctx, "recovery", frame)


def has_resurrection_icon(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """复活提示（Resurrection 图标，BGI ``CheckResurrection``）。"""
    return _find_template(ctx, "resurrection", frame)


def has_chest_f_icon(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """宝箱 F 交互图标（BGI ``AutoOpenChest``）。"""
    return _find_template(ctx, "chest_f", frame)


def has_flower_f_icon(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """地脉花 F 交互图标（BGI ``AutoOpenChest`` 备选）。"""
    return _find_template(ctx, "flower_f", frame)


def has_chest_icon(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """宝箱图标（远距离，BGI ``ChestIcon``）。"""
    return _find_template(ctx, "chest_icon", frame)


def has_go_teleport(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """传送按钮（地图界面，BGI ``GoTeleport``）。"""
    return _find_template(ctx, "go_teleport", frame)


def has_page_close(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """关闭页面按钮（对话/菜单弹出页，BGI ``PageClose``）。"""
    return _find_template(ctx, "page_close", frame)


def has_icon_option(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """对话选项气泡图标（BGI ``OptionIcon``）。"""
    return _find_template(ctx, "icon_option", frame)


def has_icon_exclamation(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """感叹号选项图标（BGI ``ExclamationIcon``）。"""
    return _find_template(ctx, "icon_exclamation", frame)


def has_pick_f(ctx: "GameContext", frame: "IImageBuffer | None" = None) -> bool:
    """拾取 F 键图标（BGI ``AutoPick``）。"""
    return _find_template(ctx, "pick_f", frame)


# ── 橙色选项检测（BGI ``IsOrangeOption``）──


def is_orange_option(frame: "IImageBuffer", x: int, y: int, w: int, h: int) -> bool:
    """判断矩形区域内是否为橙色文字（重要选项）。

    BGI 逻辑：BGR 阈值 (48,195,243)-(55,205,255)，白色占比 > 6%。
    注意：BGI 用 BGR，我们用 BGRA8 bytes，所以 B=48..55, G=195..205, R=243..255。
    """
    raw = frame.to_bytes()
    fw = frame.width
    total = 0
    orange = 0
    for py in range(y, min(y + h, frame.height)):
        for px in range(x, min(x + w, fw)):
            offset = (py * fw + px) * 4
            if offset + 3 >= len(raw):
                continue
            b, g, r, _ = struct.unpack_from("BBBB", raw, offset)
            total += 1
            if 48 <= b <= 55 and 195 <= g <= 205 and 243 <= r <= 255:
                orange += 1
    if total == 0:
        return False
    return (orange / total) > 0.06


# ── 场景分类器工厂 ──


def make_classifier(ctx: "GameContext") -> SceneClassifier:
    """创建真实场景分类器（捕获 ctx 用于模板匹配/像素检测）。

    用法：``set_classifier(make_classifier(ctx))``
    分类优先级：DIALOG > MAP > LOADING > DOMAIN > COMBAT > MAIN_UI > UNKNOWN
    """

    def classify(frame: "IImageBuffer") -> SceneState:
        # 对话（最高优先：对话中不应误判为主界面）
        if has_disabled_ui_btn(ctx, frame):
            return SceneState(scene=Scene.DIALOG, confidence=0.95)

        # 大地图
        if has_map_scale_btn(ctx, frame) or has_map_settings_btn(ctx, frame):
            return SceneState(scene=Scene.MAP, confidence=0.95)

        # 加载
        if is_loading_screen(ctx, frame):
            return SceneState(scene=Scene.LOADING, confidence=0.9)

        # 秘境
        if has_in_domain(ctx, frame):
            return SceneState(scene=Scene.DOMAIN, confidence=0.9)

        # 战斗：有血条+技能 UI 但不在对话/地图/秘境
        # 简化判定：主界面 + 检测到敌人 → 战斗
        # （精确战斗检测待 Phase C 的 fighter 能力）
        if has_paimon_menu(ctx, frame):
            # 检测到敌人 → 战斗
            # TODO: Phase C 接入 fighter.has_enemy()
            return SceneState(scene=Scene.MAIN_UI, confidence=0.9)

        # 菜单/其他：有关闭按钮但不在上述场景
        if has_page_close(ctx, frame):
            return SceneState(scene=Scene.MENU, confidence=0.7)

        return SceneState(scene=Scene.UNKNOWN, confidence=0.0)

    return classify
