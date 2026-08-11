"""claim_mail —— 领取邮件奖励（Phase D）。

对照 BGI ClaimMailRewardsTask：ESC 打开派蒙菜单 → 点邮件图标 → 点全部领取 → ESC 关闭。
无邮件时优雅跳过。
"""

from __future__ import annotations

from framework import task


@task(
    name="claim_mail",
    desc="领取所有邮件奖励：打开派蒙菜单→点邮件图标→全部领取→关闭。无邮件时跳过。",
    daemons=["frame", "scene_estimator", "auto_skip"],
    tags=["p1", "daily"],
)
def main(ctx, g) -> dict:
    """邮件领取主流程。返回 ``{claimed}``。"""
    from abilities.mail import claim_all_mail

    ok = claim_all_mail(ctx, g)
    ctx.observe.event("mail.claim", ability="claim_mail", phase="act",
                      step="claim", ok=ok, claimed=ok)
    return {"claimed": ok}
