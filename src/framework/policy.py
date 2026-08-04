"""Policy —— 护栏层（docs/design/02 §5）。

让 **AI 生成的代码可被信任**：BGI 的任务是人写的（人不会"把原石全抽了"），但 AI 会。
声明式 Policy + 沙箱强制：AI 任务的每次 g.* 调用经 Policy 校验，违规则抛 PolicyViolation
（进 Observe 失败分类，AI 据此修正）。高风险动作按 confirm_threshold 暂停待人类确认。

这是"AI 全程处理"模式**独有**的风险层（BGI 不需要），是"人敢放手"的前提（02 §5.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from framework.errors import PolicyViolation


@dataclass
class Policy:
    never_spend: list[str] = field(default_factory=list)  # ["primogem","stardust"] 禁止消耗的货币
    no_coop: bool = False  # 不进联机
    regions: list[str] | None = None  # 只在这些区域活动（None=不限）
    time_budget_s: int | None = None  # 单次运行上限（秒）
    confirm_threshold: str = "medium"  # high|medium|low：高风险动作执行前的人类确认门槛

    # ── 校验（g.* / Runtime 在相关操作前调用）──

    def check_spend(self, currency: str) -> None:
        if currency in self.never_spend:
            raise PolicyViolation(rule=f"spend:{currency}")

    def check_coop(self) -> None:
        if self.no_coop:
            raise PolicyViolation(rule="coop")

    def check_region(self, region: str) -> None:
        if self.regions is not None and region not in self.regions:
            raise PolicyViolation(rule=f"region:{region}")

    def check_time(self, elapsed_s: float) -> None:
        if self.time_budget_s is not None and elapsed_s > self.time_budget_s:
            raise PolicyViolation(rule=f"time_budget:{self.time_budget_s}s")

    def needs_confirm(self, action_risk: str) -> bool:
        """该风险等级的动作是否需人类确认（接 Runtime 确认通道，阶段三起步返回布尔）。"""
        order = {"low": 0, "medium": 1, "high": 2}
        return order.get(action_risk, 1) >= order.get(self.confirm_threshold, 1)

    @classmethod
    def default(cls) -> "Policy":
        """宽松默认（起步）：不花原石/星光，不限区，需确认 high 级动作。"""
        return cls(never_spend=["primogem", "stardust"], confirm_threshold="high")
