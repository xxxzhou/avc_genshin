"""FrameDaemon —— 单一截图+推理，广播给订阅者（docs/design/02 §3）。

避免多个守护各自截图+YOLO 把单线程 loop 吃满、拖慢主任务（02 §3.1）。
按固定频率（自适应待实现）截图，写入 SharedState.frame；若注入了 detector，
顺带推理写 SharedState.detections。SceneEstimator/auto_pick 等都读共享结果。

性能纪律（02 §3.2）：重活（截图、YOLO/OCR）下沉 avc/onnxruntime（释放 GIL）。
阶段三骨架：截图+发布帧；detector 推理为可注入钩子（to_thread 化待优化）。
"""

from __future__ import annotations

from framework.daemons.base import Daemon, DaemonCtx, daemon


@daemon(name="frame", interval=0.1)  # ~10Hz；owns_keys 空、所有场景活跃
class FrameDaemon(Daemon):
    """唯一截图者。detector 可由 Runtime/外部用 ``FrameDaemon.detector = ...`` 注入。"""

    detector = None  # 可选：abilities.detector.GenshinDetector 实例

    async def step(self, dctx: DaemonCtx) -> None:
        buf = dctx.ctx.capture()
        if buf is None:
            return
        dctx.shared.frame = buf  # 整体替换（swap，读取方拿一致快照）
        if self.detector is not None:
            try:
                dctx.shared.detections = self.detector.detect(buf)
            except Exception as e:  # 推理失败不应拖垮守护链
                dctx.observe.event("frame_infer_error", level="debug", error=repr(e))
