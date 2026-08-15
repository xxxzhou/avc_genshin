"""模板尺寸守卫测试（2026-08-15 实机 P0：avc OpenCV cv::crossCorr 崩溃）。

背景：avc 模板匹配 `matchTemplate` 在模板 ≥ 搜索区域时触发 OpenCV 断言崩溃
`corr.rows <= img.rows + templ.rows - 1`，走 terminate 绕过 Python 异常路径
（无 failure 事件/无存证）。守卫：`_template_fits` 在 match 前验模板 < 搜索区域。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _res_path(name: str) -> str:
    from framework.resources import res

    return str(res.template_ui(name))


class TestTemplateFits:
    def test_template_smaller_than_frame_ok(self):
        from abilities.vision_utils import _template_fits

        # paimon_menu.png 38×40 < 1920×1080 → fits
        frame = MagicMock(width=1920, height=1080)
        assert _template_fits(_res_path("paimon_menu.png"), frame, None) is True

    def test_template_larger_than_roi_skipped(self):
        from abilities.vision_utils import _template_fits

        # 用真模板 paimon_menu（38×40），ROI 宽仅 10 → 模板 > 搜索区域 → False（跳过）
        frame = MagicMock(width=1920, height=1080)
        roi = (0, 0, 10, 20)
        assert _template_fits(_res_path("paimon_menu.png"), frame, roi) is False

    def test_template_equal_to_roi_skipped(self):
        from abilities.vision_utils import _template_fits

        frame = MagicMock(width=1920, height=1080)
        roi = (0, 0, 38, 40)  # 恰好等于模板尺寸 → 需严格小于 → False
        assert _template_fits(_res_path("paimon_menu.png"), frame, roi) is False

    def test_missing_template_path_conservative_ok(self):
        from abilities.vision_utils import _template_fits

        frame = MagicMock(width=1920, height=1080)
        # 读不到尺寸 → 保守放行（不拦截正常路径）
        assert _template_fits("/no/such/template.png", frame, None) is True

    def test_tpl_size_cache(self):
        from abilities.vision_utils import _tpl_size

        assert _tpl_size(_res_path("paimon_menu.png")) == (38, 40)


class TestFindTemplateGuard:
    def test_find_template_skips_large_template(self):
        """find_template 遇模板>搜索区域返回 None（不发 match），reason=template_larger_than_region。"""
        from abilities.vision_utils import find_template

        ctx = MagicMock()
        ctx.observe = MagicMock()
        tm = MagicMock()
        tm.addTemplatePath.return_value = 0
        ctx.tm = tm
        # 假 frame：宽高足够，但 ROI 极小 → 模板 > ROI
        buf = MagicMock(width=1920, height=1080)
        find_template(ctx, _res_path("paimon_menu.png"), roi=(0, 0, 10, 20), frame=buf)
        # 不应调用 tm.match（会触发 avc 崩溃）
        assert not tm.match.called
        evs = [c for c in ctx.observe.event.call_args_list]
        assert evs and evs[0].kwargs.get("reason") == "template_larger_than_region"
