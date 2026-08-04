"""示例持久任务：任务组合（ctx.run 调子任务）。验证 04 §6 组合机制。"""

from framework import task


@task(
    name="compose_ping",
    desc="组合示例：调用 ping 子任务并透传其返回值。验证 ctx.run 组合。",
    tags=["test"],
)
def main(ctx, g):
    r = ctx.run("ping", echo="from_parent")
    g.set_flag("composed", True)
    return {"nested": r}
