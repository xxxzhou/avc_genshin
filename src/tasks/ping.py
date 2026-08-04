"""示例持久任务：连通性探针。验证任务体系（@task → discover → run_task）。

只用已验证的高层原语（g.set_flag / 返回 dict），不依赖未实现的移动/对话。
"""

from framework import task


@task(
    name="ping",
    desc="连通性探针：设 flag ping=ok 并返回 {pong:True}。用于验证任务体系端到端。",
    tags=["test"],
    params={"echo": {"type": "str", "default": "ok", "desc": "回显值"}},
)
def main(ctx, g, echo="ok"):
    g.set_flag("ping", echo)
    return {"pong": True, "echo": echo}
