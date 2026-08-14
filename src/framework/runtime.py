"""Runtime —— 执行引擎（docs/design/01 §4）。

同步外壳、异步内核：
  - asyncio loop 跑在专用线程：守护（框架驱动循环）+ 桥接目标协程。
  - 主脚本（同步 ``fn(ctx, g)``）跑在 to_thread 线程池；g.* 经同步桥回到 loop。
  - 单线程 loop → 所有 avc 调用序列化，天然解决 avc 并发安全（01 §3 决策 2）。

线程拓扑：调用线程(run_callable 阻塞) │ loop 线程(守护/协程) │ pool 线程(主脚本 fn)。
fn 不在 loop 线程 → g.* 桥接不会死锁；fn 期间 loop 空闲跑守护 → 边走边拾取成立。

守护采用**框架驱动循环**：daemon 只写 step，框架统一保证 取消/场景门控/输入权属/频率
（02 §2.3）。ctx 可注入（测试用 MockCtx，无需游戏/avc）。
"""

from __future__ import annotations

import asyncio
import sys
from threading import Thread
from typing import TYPE_CHECKING, Any, Callable

from framework.authority import InputAuthority
from framework.cancellation import CancellationToken, RunContext
from framework.config import Config
from framework.notify import notify
from framework.errors import (
    CancelledError,
    InputConflict,
    NormalEnd,
    Retry,
    TaskError,
)
from framework.logging import JsonlLogger, new_run_id
from framework.observe import Observe
from framework.report import live_line, summarize, summary_text
from framework.policy import Policy
from framework.shared import SharedState

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.daemons.base import DaemonCtx
    from framework.high_level_api import HighLevelApi
    from framework.registry import TaskRegistry
    from framework.task import TaskDescriptor


class Runtime:
    """AI 脚本运行时。单例（``Runtime.instance()``）。"""

    _instance: "Runtime | None" = None

    def __init__(
        self,
        ctx: "GameContext | Any | None" = None,
        *,
        window: str = "原神",
        cfg: Config | None = None,
        policy: Policy | None = None,
        registry: "TaskRegistry | None" = None,
    ):
        if ctx is None:
            from framework.context import GameContext

            ctx = GameContext(window_title=window, cfg=cfg or Config.load())
        self.ctx = ctx

        # 注册真实场景分类器（Phase A：替代默认返回 UNKNOWN 的占位分类器）
        try:
            from abilities.game_state import make_classifier
            from framework.scene import set_classifier

            set_classifier(make_classifier(self.ctx))
        except Exception:
            pass  # 无 avc 环境时保持默认分类器（测试/无游戏）

        try:  # 绑定反向引用，供 ctx.mount/unmount/run 委托
            self.ctx.runtime = self  # type: ignore[attr-defined]
        except Exception:
            pass

        self.cfg = ctx.cfg if (cfg is None and hasattr(ctx, "cfg")) else (cfg or Config.load())
        self.policy = policy or Policy.default()
        self.shared = SharedState()
        self.authority = InputAuthority()

        self.loop = asyncio.new_event_loop()
        self._loop_thread = Thread(target=self._run_loop_forever, daemon=True, name="avcgs-loop")
        self._loop_thread.start()
        self._resume_event = asyncio.Event()
        self._resume_event.set()  # 初始非挂起
        self._suspended = False

        self._daemon_tasks: dict[str, asyncio.Task] = {}
        # 每次 run 的状态（run_callable 内设置）
        self._token: CancellationToken | None = None
        self._observe: Observe | None = None
        self._dctx: "DaemonCtx | None" = None
        self._g: "HighLevelApi | None" = None
        self._guard: "GuardRail | None" = None  # 健康度护栏（每 run 重建）
        self._nest_depth = 0  # ctx.run 嵌套深度（04 §6.2，上限 8）

        self._hotkey_listener: "HotkeyListener | None" = None

        from framework.registry import TaskRegistry

        self.registry = registry or TaskRegistry()
        self.registry.discover()

        Runtime._instance = self

    @classmethod
    def instance(cls) -> "Runtime":
        if cls._instance is None:
            raise RuntimeError("Runtime 尚未创建")
        return cls._instance

    # ── loop 线程 ──

    def _run_loop_forever(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def shutdown(self) -> None:
        self._stop_hotkey()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._loop_thread.join(timeout=2)

    def _bridge(self, coro, timeout: float | None = 5) -> Any:
        """从非 loop 线程提交协程到 loop 并阻塞等结果。"""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout) if timeout else fut.result()

    # ── 主脚本执行 ──

    def run_callable(
        self,
        fn: Callable[..., Any],
        *,
        task_name: str = "script",
        timeout: float | None = 600,
        daemons: tuple[str, ...] = (),
    ) -> Any:
        """跑同步 ``fn(ctx, g)``。daemons: 起始自动挂载的守护名（@task.daemons）。

        基础守护 ``frame``+``scene_estimator`` 始终挂载：场景估计依赖 frame，而
        ``g.scene``/``wait_scene``/``teleport_to`` 依赖 shared.scene。若不挂，场景恒为
        None，地图导航会被跳过、传送落点错误（2026-08-08 实机定位）。
        """
        from framework.daemons.base import DaemonCtx
        from framework.high_level_api import HighLevelApi

        token = CancellationToken()
        run_id = new_run_id()
        RunContext(token=token, run_id=run_id, task=task_name)
        self._token = token
        logger = JsonlLogger(run_id, logs_dir=self.cfg.logs_dir)
        observe = Observe(logger, self.shared, debug_dir=self.cfg.debug_dir)
        self._observe = observe
        self._dctx = DaemonCtx(
            ctx=self.ctx, shared=self.shared, authority=self.authority,
            observe=observe, token=token, cfg=self.cfg,
        )
        self._g = HighLevelApi(self.ctx, runtime=self)
        observe.event("run_start", task=task_name)
        # 实时打印订阅者：每条事件一行进 stderr（不用 StatusLine——它是单行 5s 清屏，不适合事件流）
        observe.subscribe(lambda e: print(live_line(e), file=sys.stderr))
        # 健康度护栏订阅者：触发阈值 → token.cancel + save_evidence + emit auto_kill
        # 解决"任务出问题停不下来"——不用等 F9/超时，能力死循环即自动止损（见 guardrail.py）
        from framework.guardrail import GuardRail

        self._guard = GuardRail(token, observe, self.ctx)
        observe.subscribe(self._guard.on_event)
        # 取消令牌注入 ctx：ability 同步长循环调 ctx.check_cancel() 响应取消
        # （GuardRail auto_kill / F9 / 超时；2026-08-14 实机 pos.match 死循环教训）
        self.ctx.token = token
        self._notify_task = task_name
        notify("task_start", task=task_name)

        for d in ("frame", "scene_estimator", *daemons):
            try:
                self.mount(d)
            except Exception as e:
                observe.event("mount_error", level="warn", daemon=d, error=repr(e))

        # F9 全局热键：按 F9 取消当前任务
        self._start_hotkey(token)

        try:
            fut = asyncio.run_coroutine_threadsafe(self._worker(fn, timeout), self.loop)
            result = fut.result(timeout=timeout + 5) if timeout else fut.result()
            return result
        except KeyboardInterrupt:
            token.cancel("用户中断")
            raise
        finally:
            self._teardown()

    async def _worker(self, fn: Callable, timeout: float | None) -> Any:
        """loop 协程：把 fn 丢到线程池跑，处理异常/超时/取消/重试（04 §3.2）。"""
        observe = self._observe
        task = getattr(self, "_notify_task", "?")
        attempts = 0
        while True:
            try:
                if timeout:
                    result = await asyncio.wait_for(asyncio.to_thread(fn, self.ctx, self._g), timeout)
                else:
                    result = await asyncio.to_thread(fn, self.ctx, self._g)
                observe.event("task_return", return_value=result)
                notify("task_end", task=task)
                return result
            except asyncio.TimeoutError:
                observe.failure("Timeout", reason=f"超时 {timeout}s")
                notify("task_error", task=task, error=f"超时 {timeout}s")
                self._token.cancel("timeout")
                raise
            except CancelledError as e:
                observe.failure("Timeout", reason=f"取消：{e}")
                notify("task_error", task=task, error=f"取消：{e}")
                raise
            except NormalEnd as e:
                observe.event("task_return", normal_end=True, reason=e.reason)
                notify("task_end", task=task, normal_end=True, reason=e.reason)
                return None
            except Retry as e:
                attempts += 1
                lim = e.attempts or 3
                if attempts > lim:
                    observe.failure("TaskError", reason=f"重试 {lim} 次仍失败：{e.reason}")
                    notify("task_error", task=task, error=f"重试 {lim} 次仍失败")
                    raise TaskError(reason=f"重试 {lim} 次仍失败：{e.reason}")
                observe.event("retry", reason=e.reason, attempt=attempts)
                continue  # 重跑 fn
            except TaskError as e:
                observe.failure(e.failure_type, reason=e.reason)
                notify("task_error", task=task, error=e.failure_type)
                raise
            except Exception as e:
                observe.failure("TaskError", reason=repr(e))
                notify("task_error", task=task, error=repr(e))
                observe.save_evidence(self.ctx, "crash")
                raise

    def _teardown(self) -> None:
        """结束：卸载所有守护 + 释放按键 + 关日志（01 §8.4）。"""
        self._stop_hotkey()
        self._stop_all_daemons()
        try:
            self.ctx.release_all_keys()
        except Exception:
            pass
        self.ctx.token = None  # 取消令牌随 run 结束失效
        try:
            self.ctx.close()
        except Exception:
            pass
        if self._observe is not None:
            # run_summary：按 ability 分组摘要（替代塞 task_return——后者早于 teardown 发，时序对不上）。
            # AI 读 jsonl → 找 run_summary → 配合用户提示点名坏在哪个 ability 的哪个 stage/reason。
            summary = summarize(self._observe.timeline())
            self._observe.event("run_summary", summary=summary)
            print(summary_text(summary), file=sys.stderr)
            self._observe.event("run_end")
            self._observe.logger.close()
        self._token = None
        self._observe = None
        self._dctx = None
        self._g = None

    # ── 任务执行（run_task 顶层 / _run_inline = ctx.run 组合，04 §6）──

    def run_task(self, name: str, timeout: float | None = 600, **params) -> Any:
        """顶层运行已注册任务（CLI --task 走这里）。daemons 来自 @task.daemons。"""
        desc = self.registry.get(name)
        params = self._validate_params(desc, params)
        daemons = tuple(desc.daemons)

        def fn(ctx, g):
            return desc.main(ctx, g, **params)

        return self.run_callable(fn, task_name=name, timeout=timeout, daemons=daemons)

    def _run_inline(self, name: str, **params) -> Any:
        """ctx.run 组合：在当前 run 内同线程跑子任务 main（共享 token/observe/ctx/g）。

        NormalEnd→返回 None；Retry→重试（默认 3）；TaskError/其它→向父传播（04 §6.1）。
        """
        desc = self.registry.get(name)
        params = self._validate_params(desc, params)
        self._nest_depth += 1
        if self._nest_depth > 8:
            self._nest_depth -= 1
            raise TaskError(reason="ctx.run 嵌套过深（>8）")
        obs = self._observe
        if obs:
            obs.event("task_start", task=name, params=params)
        mounted: list[str] = []
        try:
            for d in desc.daemons:
                self.mount(d)
                mounted.append(d)
            attempts = 0
            while True:
                try:
                    result = desc.main(self.ctx, self._g, **params)
                except NormalEnd as e:
                    if obs:
                        obs.event("task_return", task=name, normal_end=True, reason=e.reason)
                    return None
                except Retry as e:
                    attempts += 1
                    lim = e.attempts or 3
                    if attempts > lim:
                        raise TaskError(reason=f"重试 {lim} 次仍失败：{e.reason}")
                    if obs:
                        obs.event("retry", task=name, reason=e.reason, attempt=attempts)
                    continue
                else:
                    if obs:
                        obs.event("task_return", task=name, return_value=result)
                    return result
        finally:
            for d in reversed(mounted):
                try:
                    self.unmount(d)
                except Exception:
                    pass
            self._nest_depth -= 1

    def _validate_params(self, desc: "TaskDescriptor", params: dict) -> dict:
        """按 @task.params schema 注入默认值 + 类型校验（04 §2.3）。"""
        out = dict(params)
        for k, spec in (desc.params or {}).items():
            if k not in out:
                if "default" in spec:
                    out[k] = spec["default"]
                elif spec.get("required"):
                    raise TaskError(reason=f"缺少必填参数：{k}")
            elif "type" in spec:
                out[k] = self._coerce(out[k], spec["type"], k)
        return out

    @staticmethod
    def _coerce(val: Any, typ: str, key: str) -> Any:
        _MAP = {"int": int, "float": float, "str": str, "bool": bool, "list": list, "dict": dict}
        try:
            return _MAP[typ](val)
        except (ValueError, TypeError) as e:
            raise TaskError(reason=f"参数 {key} 无法转为 {typ}：{e!r}")

    # ── 守护 mount/unmount/suspend（从工作线程调用 → 桥接到 loop）──

    def mount(self, name: str, **opts) -> None:
        from framework.daemons.base import get_daemon_class

        cls = get_daemon_class(name)
        if cls is None:
            raise ValueError(f"未知守护：{name}")
        if self._dctx is None:
            raise RuntimeError("无活动 run（mount 须在任务执行期调用）")
        self._bridge(self._mount_async(cls, name))

    async def _mount_async(self, cls, name: str) -> None:
        if name in self._daemon_tasks:
            return
        task = self.loop.create_task(self._daemon_loop(cls, self._dctx))
        self._daemon_tasks[name] = task
        if self._observe:
            self._observe.event("mount", daemon=name)

    def unmount(self, name: str) -> None:
        self._bridge(self._unmount_async(name))

    async def _unmount_async(self, name: str) -> None:
        task = self._daemon_tasks.pop(name, None)
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=1)
        except Exception:
            pass
        if self._observe:
            self._observe.event("unmount", daemon=name)

    def suspend_all(self) -> None:
        self._bridge(self._set_suspend(True))

    def resume_all(self) -> None:
        self._bridge(self._set_suspend(False))

    async def _set_suspend(self, s: bool) -> None:
        self._suspended = s
        if s:
            self._resume_event.clear()
        else:
            self._resume_event.set()

    def _stop_all_daemons(self) -> None:
        if not self._daemon_tasks:
            return
        try:
            self._bridge(self._stop_all_async(), timeout=5)
        except Exception:
            pass

    async def _stop_all_async(self) -> None:
        for task in list(self._daemon_tasks.values()):
            task.cancel()
        for task in list(self._daemon_tasks.values()):
            try:
                await asyncio.wait_for(task, timeout=1)
            except Exception:
                pass
        self._daemon_tasks.clear()

    # ── 守护框架驱动循环（取消 / 场景门控 / 权属 / 频率）──

    async def _daemon_loop(self, cls, dctx: "DaemonCtx") -> None:
        inst = cls()
        name = inst.name
        lease = None
        try:
            while not dctx.token.cancelled:
                if self._suspended:  # suspend_all：释放权属，等恢复
                    if lease:
                        lease.release()
                        lease = None
                    await self._resume_event.wait()
                    continue
                try:
                    dctx.token.check()
                except CancelledError:
                    break
                # 场景门控（02 §2.3）
                if inst.scenes:
                    cur = dctx.shared.scene.scene if dctx.shared.scene else None
                    if cur not in inst.scenes:
                        if lease:
                            lease.release()
                            lease = None
                        await self._wait_scene(dctx, inst.scenes)
                        continue
                # 输入权属（02 §2.2）：确保持有 owns_keys
                if inst.owns_keys:
                    if lease is None or not lease.active:
                        try:
                            lease = dctx.authority.acquire(inst.owns_keys, name, inst.priority)
                        except InputConflict:
                            await asyncio.sleep(0.2)  # 抢不到，退让
                            continue
                # 一步
                try:
                    await inst.step(dctx)
                except CancelledError:
                    break
                except Exception as e:
                    if dctx.observe:
                        dctx.observe.event("daemon_error", level="warn", daemon=name, error=repr(e))
                await asyncio.sleep(inst.interval)
        except asyncio.CancelledError:
            pass
        finally:
            if lease:
                try:
                    lease.release()
                except Exception:
                    pass

    async def _wait_scene(self, dctx: "DaemonCtx", scenes) -> None:
        """挂起到场景恢复或取消（场景门控的等待支）。"""
        while not dctx.token.cancelled and not self._suspended:
            cur = dctx.shared.scene.scene if dctx.shared.scene else None
            if cur in scenes:
                return
            try:
                dctx.token.check()
            except CancelledError:
                return
            await asyncio.sleep(0.1)

    # ── 全局热键（F9 取消）──

    def _start_hotkey(self, token: CancellationToken) -> None:
        """注册 F9 全局热键，按 F9 触发 token.cancel()。"""
        if sys.platform != "win32":
            return
        try:
            from framework.hotkey import HotkeyListener, VK_F9

            listener = HotkeyListener()
            listener.register(VK_F9, callback=lambda: token.cancel("F9"))
            listener.start()
            self._hotkey_listener = listener
        except Exception:
            pass  # 热键注册失败不影响运行

    def _stop_hotkey(self) -> None:
        """停止热键监听。"""
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
            self._hotkey_listener = None
