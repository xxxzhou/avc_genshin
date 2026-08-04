# 05 · 高层 API 与守护库 —— AI 写任务的工具箱

> 本文档钉死 AI 写任务时**能调用的全部接口**:`g.*` 高层 API、`ctx.*` 运行时控制、降级层 `vision.*`、守护库清单、以及如何写新守护。
> 与 `04-任务契约与注册.md` 配套——**AI 写任务只需读这两篇**(契约 + 工具箱)。
> 前置:`01`/`02`/`03`/`04`。

---

## 0. 一句话

`g.*` 是 AI 的**主要工具**:一行调用完成"传送/走/对话/等待/检测/操作",框架在内部自动处理并发权属、场景读取、日志记录、护栏校验。绝大多数任务**只用 `g.*` 就够了**;`ctx.*` 管运行时(挂守护/调子任务);`vision.*` 是降级逃生口。

---

## 1. API 三层分层

| 层 | 入口 | 用途 | AI 何时用 |
|---|---|---|---|
| **第一层 高层语义** | `g.*` | 操作语义(teleport/go_to/talk/wait_*/detect/click) | **默认全用这层** |
| **第二层 运行时控制** | `ctx.*` | 挂卸守护、调子任务、直接截图/按键(降级) | 需要**并发守护**或**任务组合**时 |
| **第三层 视觉原语** | `vision.*` | find_template/find_text/detect_objects(返回原始结果) | `g.*` 不够用时降级 |

> 设计原则(01 §6):让 AI 写高层语义、少接触 avc 原始 API。三层由高到低,能高则高。

---

## 2. `g.*` 全量 API

> 所有坐标基于 **1080p**(`00 §8`);所有 `g.*` 调用**同步阻塞**、内部检查 `CancellationToken`、自动写日志(03 §7)、自动经 `InputAuthority`/`Policy`(02)。

### 2.1 移动 / 导航

| 方法 | 签名 | 语义 |
|---|---|---|
| `g.teleport_to` | `(name: str) -> None` | 传送到地点/七天神像/秘境入口(地图选点→确认,内部走 `quick_teleport` 逻辑) |
| `g.go_to` | `(pos: tuple[float,float]) -> None` | 走到小地图坐标(PathExecutor 闭环纠偏;执行期持 `MOVE`/`MOUSE_MOVE` 租约) |
| `g.go_along` | `(path: list) -> None` | 沿 BGI JSON 路径走(07 §2) |
| `g.face` | `(pos_or_dir) -> None` | 转向某点/方向 |
| `g.jump` | `() -> None` | 跳跃(空格) |

### 2.2 对话

| 方法 | 签名 | 语义 |
|---|---|---|
| `g.talk` | `(option: str) -> None` | 在对话中找匹配 `option` 的选项并点击(模糊匹配) |
| `g.talk_skip` | `() -> None` | 跳过当前对话到下一句/结束 |
| `g.talk_open` | `() -> None` | 推进对话到出现选项 |
| `g.visible_options` | `() -> list[str]` | 当前对话可见的选项文本(供 `g.decide` 决策,04 §10) |

### 2.3 等待(阻塞到条件满足 / 超时)

| 方法 | 签名 | 语义 |
|---|---|---|
| `g.wait_text` | `(kw: str, timeout=10) -> bool` | 等 OCR 出现含 `kw` 的文字 |
| `g.wait_template` | `(path: str, timeout=10) -> bool` | 等模板出现(`path` 经 `res.template()` 解析) |
| `g.wait_main_ui` | `(timeout=30) -> bool` | 等 `scene==MAIN_UI` 且稳定(读 SharedState,02 §1) |
| `g.wait_scene` | `(scene: str, timeout=30) -> bool` | 等进入指定场景 |
| `g.wait_loading` | `(timeout=60) -> bool` | 等加载完成 |
| `g.wait_until` | `(pred: Callable, timeout=30) -> bool` | 等谓词为真(通用) |

> 超时返回 `False` 并**自动存证**(截图 + 期望,记 `failure` 候选;02 §4)。

### 2.4 检测(即时查询,返回高层结果)

| 方法 | 签名 | 语义 |
|---|---|---|
| `g.scene` | `() -> SceneState` | 当前场景(直接读 SharedState,不重判) |
| `g.has_enemy` | `() -> bool` | 屏幕上有敌人 |
| `g.find_nearest_enemy` | `() -> tuple[float,float] \| None` | 最近敌人小地图坐标 |
| `g.is_loading` | `() -> bool` | 是否加载中 |
| `g.find_template` | `(path: str) -> Rect \| None` | 即时查模板(不等待) |
| `g.find_text` | `(kw: str) -> Rect \| None` | 即时查文字 |
| `g.detect_objects` | `(cls: str) -> list[Detection]` | YOLO 检测某类物体(world/avatar/...) |

### 2.5 操作(1080p 坐标)

| 方法 | 签名 | 语义 |
|---|---|---|
| `g.click` | `(x: float, y: float) -> None` | 单击(经拟人化) |
| `g.press` | `(key: KeyCode, hold=0.0) -> None` | 按键(hold 秒) |
| `g.hotkey` | `(*keys: KeyCode) -> None` | 组合键 |
| `g.type_text` | `(s: str) -> None` | 输入文字 |
| `g.scroll` | `(dx: int, dy: int) -> None` | 滚轮 |
| `g.move_to` | `(x: float, y: float) -> None` | 移动鼠标 |

### 2.6 世界模型(跨任务持久状态)

| 方法 | 签名 | 语义 |
|---|---|---|
| `g.set_flag` | `(key: str, val) -> None` | 写持久标志(如 `daily_claimed=True`) |
| `g.has_flag` | `(key: str) -> bool` | 读标志(供任务跳过已完成项,04 §10 示例) |

> 世界模型持久化于本地(今日领过/树脂数/已采集点),让框架不重复劳动、随使用积累对"你的号"的认知。详见 `06`。

### 2.7 决策(预留,后置)

| 方法 | 签名 | 语义 |
|---|---|---|
| `g.decide` | `(question, schema, *, context="auto", timeout=30) -> Decision` | 运行时决策层(LLM/VLM/人在环),**当前 `NoOpDecider`**(01 §11) |

---

## 3. `ctx.*` 运行时控制

```python
# 守护(并发响应)
ctx.mount(name: str, **opts) -> None       # 挂守护(如 auto_pick)
ctx.unmount(name: str) -> None             # 卸守护
ctx.suspend_all() -> None                  # 挂起全部(进不可打断段,如战斗连招/设置菜单)
ctx.resume_all() -> None                   # 恢复

# 任务组合
ctx.run(name: str, **params) -> dict | None   # 调子任务(04 §6)

# 降级(直接 avc,不经高层封装;谨慎用)
ctx.capture() -> IImageBuffer              # 截图
ctx.click_at(x, y) -> None                 # 点(截图坐标→屏幕坐标)
ctx.press(key, holdMs=0) -> None           # 按键
```

> `ctx.mount` 的守护**自带场景门控与输入权属**(02 §2),任务无需关心并发安全。

---

## 4. 降级层 `vision.*`(第三层)

```python
vision.find_template(path: str, threshold=0.8) -> Rect | None
vision.find_text(kw: str) -> Rect | None
vision.detect_objects(cls: str) -> dict[str, list[Detection]]   # YOLO 全类
vision.ocr_region(x, y, w, h) -> list[str]                      # 指定区域 OCR
```

- 仅当 `g.*` 表达不了时用(如自定义 ROI 检测、特殊阈值)。
- 返回**原始结果**(Rect/Detection),任务自己解读。

---

## 5. 守护库清单(`framework/daemons/`)

每个守护对应 BetterGI 的一个实时触发器,改造为自治 async 循环(01 §4.2)。

| 守护 | 对应 BGI | `owns_keys` | `scenes` | 行为 |
|---|---|---|---|---|
| `auto_pick` | `AutoPickTrigger` | `{INTERACT}` | `MAIN_UI, DOMAIN` | YOLO 检测交互物 → 按 F |
| `auto_skip` | `AutoSkipTrigger` | `{INTERACT,SPACE}` | `DIALOG` | 检测对话 → 跳过/选橙选项/关书页 |
| `loading_wait` | `GameLoadingTrigger` | `{}` | `LOADING` | 检测加载,阻塞主流程直到完成 |
| `auto_eat` | `AutoEatTrigger` | `{}`(或道具键) | `MAIN_UI, COMBAT` | 红血自动吃营养袋/自动复活 |
| `quick_teleport` | `QuickTeleportTrigger` | `{MOUSE_CLICK}` | `MAP` | 地图选点后自动确认传送 |

> 每个守护的 `scenes` 决定它**只在哪些场景活跃**(02 §2 场景门控)——`auto_pick` 在对话/菜单/战斗里自动静默,不会误按 F。

---

## 6. 如何写一个新守护

守护是自治 async 循环,声明输入通道与活跃场景,框架保证并发安全:

```python
# src/framework/daemons/auto_fish.py
from framework import daemon, InputChannel, Scene

@daemon(
    name="auto_fish",
    owns_keys={InputChannel.MOUSE_CLICK},      # 我只碰鼠标点击
    scenes=[Scene.MAIN_UI],                    # 只在大世界钓鱼时活跃
    priority=0,
)
class AutoFishDaemon:
    async def run(self, ctx, token):
        g = HighLevelApi(ctx)
        while not token.cancelled:
            if await self._detect_bite(g):     # 检测鱼咬钩
                g.click(*bite_pos)             # 起杆(经 InputAuthority 校验通道)
                await asyncio.sleep(1.0)
            await asyncio.sleep(0.2)           # 自管频率(~5Hz)
```

要点:
- **`@daemon(owns_keys=..., scenes=...)`**:声明通道与场景,框架据此仲裁(02 §2)。同通道冲突 → 框架拒绝;非活跃场景 → 自动 suspend。
- **`async def run(ctx, token)`**:自治 `while + sleep` 循环,频率自管,**控制流在自己手里**(01 §4.2,非回调)。
- **检测走共享帧**(`FrameDaemon`,02 §3),不要自己截图+推理。
- 注册后,任务可 `ctx.mount("auto_fish")` 使用。

---

## 7. `g.*` 内部如何串起可靠性地基(02)

每次 `g.*` 调用,框架在内部自动做(任务作者无感):

```
g.go_to(pos)
  → token.check()                          取消点(01 §4.3)
  → InputAuthority.acquire({MOVE,MOUSE_MOVE}, holder="go_to")   并发权属(02 §2)
  → while not arrived:
       读 SharedState.scene / frame        共享事实(02 §1 §3)
       Observe.event("action", ...)        日志(03 §7)
       Policy.check(...)                   护栏(02 §5)
       ic.press(W, ...)                    经拟人化的 avc 输入
  → InputAuthority.release(...)
```

**任务作者只写 `g.go_to(pos)`,并发安全/场景感知/日志/护栏全自动。**

---

## 8. 端到端:用工具箱写一个任务

```python
# src/tasks/fish_all.py
from framework import task

@task(
    name="fish_all",
    desc="在当前水域自动钓鱼,直到鱼篓满或 N 次。",
    daemons=["auto_skip"],
    params={"rounds": {"type": "int", "default": 10, "desc": "最多钓几轮"}},
    tags=["collect", "fishing"],
)
def main(ctx, g, rounds=10):
    g.teleport_to("当前水域锚点")
    ctx.mount("auto_fish")                       # 挂自定义守护(§6)
    for i in range(rounds):
        if not g.wait_text("上钩", timeout=120):  # 等待(§2.3)
            break
        if g.has_flag("fish_full"):               # 世界模型(§2.6)
            break
    ctx.unmount("auto_fish")
    return {"rounds_done": i + 1}
```

展示了:`@task` 契约(04)、`g.*` 高层、`ctx.mount` 守护、`wait_text`/`has_flag` 组合。

---

## 9. 开放问题

1. **`g.*` 坐标系**:统一 1080p 截图坐标,还是区分"截图坐标/屏幕坐标"?起步统一 1080p 截图坐标,`g.click` 内部转屏幕坐标。
2. **`vision.*` 是否暴露给任务**:降级层放开会增加 AI 写错风险;起步放开(透明优于限制),靠 Observe/Policy 兜底。
3. **守护参数化**:`ctx.mount("auto_pick", whitelist=[...])` 的参数注入约定(BGI 的 `AutoPickConfig`)。
4. **世界模型持久化格式**:JSON/SQLite?跨账号隔离?
5. **`g.decide` 的 `context="auto"` 打包细节**:自动打包截图+OCR+scene+时间线的具体字段(01 §11 后置时定)。

---

*本文是 AI 写任务的工具箱参考。配合 `04-任务契约与注册`,AI 即可生成任何符合规范的任务脚本。领域能力(导航/战斗/检测,被 `g.*` 内部调用)详见 `07-领域能力.md`。*
