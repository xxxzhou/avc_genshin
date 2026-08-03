# AVC Genshin — 实现方案文档

> 基于 avc SDK Python 封装，实现类 BetterGI 的原神自动化功能

## 1. 项目定位

| 维度 | BetterGI | 本项目 |
|------|----------|--------|
| 语言 | C# / .NET 8 / WPF | Python 3.10+ / 无 UI |
| 截图 | Fischless.GameCapture (BitBlt/DwmSharedSurface) | avc IScreenCapture (~5ms) |
| 操作 | Fischless.WindowsInput (PostMessage/SendInput) | avc IInputController (SendInput + 拟人化) |
| 模板匹配 | OpenCvSharp | avc ITemplateMatcher (OpenCV + NMS) |
| OCR | PaddleOCR ONNX 版 | avc ITextRecognizer (PP-OCRv6) |
| YOLO | Compunet.YoloSharp + OnnxRuntime | avc IONNXSession / onnxruntime Python |
| 任务调度 | TaskTriggerDispatcher (定时器轮询) | AI 生成脚本直接执行 |
| 战斗 | JSON 脚本引擎 + 角色识别 + CD 管理 | 简化版：固定连招 + 技能CD检测 |
| 路径 | AutoPathing (JSON + 执行器) | 复用 BetterGI JSON 路径 + Python 执行器 |

**核心复用**：BetterGI 的 ONNX 模型文件、JSON 路径数据、模板图片。不重新训练。

---

## 2. avc Python API 速查

### 2.1 模块导入

```python
from avc import Input, Vision, Image
from avc.Input import IInputController, IScreenCapture
from avc.Input import createInputController, createScreenCapture
from avc.Vision import ITemplateMatcher, ITextRecognizer
from avc.Vision import createTemplateMatcher, createTextRecognizer
from avc.Vision import findWindowByName, getActiveWindow, getWindowName
from avc.Image import IImageBuffer, loadImage, crop, resize, toBytes
from avc._core import KeyCode, MouseButton, ImageType
from avc._core import TemplateMatchMethod, MatchOrderBy
```

### 2.2 IScreenCapture — 截图

```python
sc = createScreenCapture()

# 绑定窗口
sc.setWindow("原神")          # 子串匹配窗口标题
sc.activateWindow("原神")     # 激活并前置窗口

# 截图
sc.refresh()                  # 重新截取
buf = sc.getBuffer()          # IImageBuffer (借用, 生命周期归 sc)

# 坐标转换
pos = sc.toScreen(bufX, bufY) # 截图坐标 → 屏幕坐标, 返回 vec2i (.x/.y)

# 裁剪
cropped = sc.crop(x, y, w, h) # 返回新 IScreenCapture

# 保存
sc.save("debug.png")

# 属性
w = sc.width()
h = sc.height()
```

### 2.3 IInputController — 操作

```python
ic = createInputController()

# 鼠标
ic.moveTo(x, y)               # 移动到屏幕坐标
ic.moveBy(dx, dy)             # 相对移动
ic.click(x, y)                # 单击 (屏幕坐标)
ic.click(x, y, MouseButton.right)  # 右键
ic.doubleClick(x, y)          # 双击
ic.drag(x1, y1, x2, y2)      # 拖拽
ic.scroll(dx, dy)             # 滚轮 (正=向下/向右)
pos = ic.getCursorPos()       # 返回 (x, y)

# 键盘
ic.keyDown(KeyCode.w)         # 按下
ic.keyUp(KeyCode.w)           # 释放
ic.press(KeyCode.e)           # 按下并释放
ic.press(KeyCode.e, holdMs=500)  # 按住500ms
ic.hotkey(KeyCode.ctrl, KeyCode.c)  # 组合键
ic.typeText("你好")           # Unicode 文本输入

# 配置
ic.setMoveDurationMs(200)     # 移动动画时长 (0=瞬移)
ic.setMoveSteps(20)           # 插值步数
ic.setKeyDelayMs(50)          # 按键间隔

# 常用 KeyCode
# KeyCode.w / .a / .s / .d — 移动
# KeyCode.e / .q — 技能
# KeyCode.f — 交互
# KeyCode.space — 跳跃
# KeyCode.esc — 菜单
# KeyCode.tab — 队伍
# KeyCode.m — 地图
# KeyCode.j — 任务
# KeyCode.num1~.num4 — 角色切换
# KeyCode.shift — 冲刺
```

### 2.4 ITemplateMatcher — 模板匹配

```python
tm = createTemplateMatcher()

# 配置
tm.setMethod(TemplateMatchMethod.ccoeffNormed)  # 默认, 光照鲁棒
tm.setGreenMask(True)            # 纯绿(0,255,0)区域当透明
tm.setNmsIoU(0.2)                # NMS 去重阈值
tm.setOrderBy(MatchOrderBy.horizontal)  # 结果排序
tm.setRoi(x, y, w, h)           # 感兴趣区域

# 添加模板
idx = tm.addTemplatePath("templates/teleport_btn.png", 0.8)  # 从文件, 阈值0.8
idx = tm.addTemplate(imageBuffer, 0.7)                        # 从 IImageBuffer
tm.clearTemplates()

# 执行
count = tm.match(sceneBuffer)    # 在场景中匹配, 返回命中数

# 取结果
n = tm.getMatchCount()
result = tm.getMatch(0)          # 返回 MatchResult (.x/.y/.w/.h/.score/.templateIndex)
center = tm.matchCenter(result)  # 返回 vec2i (.x/.y) — 命中框中心
ms = tm.getMatchTimeMs()
err = tm.getLastError()
```

### 2.5 ITextRecognizer — OCR

```python
ocr = createTextRecognizer()

# 配置
ocr.setThreshold(0.3)            # 检测阈值 (DBNet)
ocr.setRoi(x, y, w, h)          # 感兴趣区域
ocr.setUseGpu(True)              # GPU 推理

# 执行
count = ocr.recognize(sceneBuffer)  # det+rec, 返回文字框数

# 取结果
n = ocr.getMatchCount()
text, result = ocr.getMatch(0)   # 返回 (str, OcrResult(.x/.y/.w/.h/.score))
center = ocr.ocrCenter(result)   # 返回 vec2i (.x/.y) — 文字框中心
ms = ocr.getMatchTimeMs()
```

### 2.6 IImageBuffer — 图像操作

```python
# 创建/加载
buf = IImageBuffer()
buf = loadImage("templates/btn.png")  # 从文件加载

# 格式
buf.width / buf.height / buf.imageType

# 数据
raw = buf.to_bytes()             # → Python bytes (BGRA8)
buf.from_bytes(data)             # bytes → buf

# 操作
cropped = crop(buf, x, y, w, h)  # 裁剪, 返回新 IImageBuffer
resized = resize(buf, 640, 640)  # 缩放, 返回新 IImageBuffer
buf.save("out.png")              # 保存
b64 = buf.to_base64()            # Base64
```

---

## 3. 功能模块设计

### 3.1 BetterGI 功能映射

| BetterGI 功能 | 模块 | 优先级 | 实现方式 |
|--------------|------|--------|---------|
| 自动拾取 | AutoPick | P0 | YOLO检测交互物 + 按F |
| 自动剧情 | AutoSkip | P0 | OCR识别对话 + 模板匹配按钮 + 点击 |
| 快速传送 | QuickTeleport | P0 | 模板匹配传送按钮 + 点击 |
| 自动秘境 | AutoDomain | P1 | 路径导航 + 战斗 + 领奖循环 |
| 自动伐木 | AutoWood | P1 | 按Z + 上下线刷新 |
| 自动钓鱼 | AutoFishing | P1 | YOLO检测鱼 + 抛竿/收杆 |
| 自动地脉花 | AutoLeyLine | P1 | 路径导航 + 战斗 + 领奖 |
| 自动采集/挖矿 | AutoPathing | P2 | 小地图定位 + 路径执行 |
| 自动战斗 | AutoFight | P2 | 角色识别 + 连招 + CD检测 |
| 七圣召唤 | AutoGeniusInvokation | P3 | 暂不实现 |
| 自动烹饪 | AutoCook | P3 | 模板匹配进度条 + 点击 |
| 自动音游 | AutoMusicGame | P3 | 模板匹配音符 + 点击 |

### 3.2 核心基础设施 (P0 — 必须先完成)

#### 3.2.1 游戏连接 — GameContext

```python
class GameContext:
    """游戏上下文: 持有所有 avc 实例, 是所有操作的基础"""

    def __init__(self, window_title="原神"):
        # avc 核心实例
        self.sc = createScreenCapture()
        self.ic = createInputController()
        self.tm = createTemplateMatcher()
        self.ocr = createTextRecognizer()

        # 绑定游戏窗口
        self.sc.setWindow(window_title)
        self.sc.activateWindow(window_title)

        # 配置拟人化
        self.ic.setMoveDurationMs(200)
        self.ic.setMoveSteps(20)
        self.ic.setKeyDelayMs(50)

        # 窗口信息
        self.window_title = window_title
        self._refresh_size()

    def _refresh_size(self):
        self.sc.refresh()
        self.width = self.sc.width()
        self.height = self.sc.height()

    def capture(self):
        """截图并返回 IImageBuffer"""
        self.sc.refresh()
        return self.sc.getBuffer()

    def click_at(self, buf_x, buf_y):
        """截图坐标点击 (自动转屏幕坐标)"""
        pos = self.sc.toScreen(buf_x, buf_y)
        self.ic.click(pos.x, pos.y)

    def click_center(self, result):
        """点击 MatchResult/OcrResult 中心"""
        if hasattr(result, 'x'):  # MatchResult
            cx, cy = result.x + result.w // 2, result.y + result.h // 2
        else:  # OcrResult
            cx, cy = result.x + result.w // 2, result.y + result.h // 2
        self.click_at(cx, cy)

    def press(self, key, holdMs=0):
        """按键"""
        self.ic.press(key, holdMs)

    def is_1920x1080(self):
        return self.width == 1920 and self.height == 1080
```

#### 3.2.2 YOLO 检测器 — GenshinDetector

两种方案可选：

**方案A: avc IONNXSession (推荐, 统一在 avc 生态内)**

```python
# avc 内置 ONNX 推理, 但目前 Python SWIG 未暴露 IONNXSession
# 需要补充 SWIG 绑定后可用
# 优势: 不依赖 onnxruntime Python 包, GPU 推理走 avc_onnx 插件
```

**方案B: onnxruntime Python (立即可用)**

```python
import onnxruntime as ort
import numpy as np

class GenshinDetector:
    """YOLO 目标检测 — 复用 BetterGI ONNX 模型"""

    def __init__(self, model_dir="models", use_gpu=True):
        providers = []
        if use_gpu:
            providers.append('CUDAExecutionProvider')
        providers.append('CPUExecutionProvider')

        self.models = {}
        self.labels = self._load_labels(f"{model_dir}/label.json")

        # 加载所有模型
        model_files = {
            'world': 'bgi_world.onnx',       # 大世界: 敌人/血条/采集物
            'fish': 'bgi_fish.onnx',          # 钓鱼
            'mine': 'bgi_mine.onnx',          # 矿物
            'tree': 'bgi_tree.onnx',          # 秘境古树
            'avatar_side': 'avatar_side_classify_sim.onnx',  # 角色侧脸
            'q_classify': 'q_classify_sim.onnx',            # Q技能CD
            'avatar': 'avatar.onnx',          # 队伍头像
        }
        for name, filename in model_files.items():
            path = f"{model_dir}/{filename}"
            if os.path.exists(path):
                self.models[name] = ort.InferenceSession(path, providers=providers)

    def detect(self, raw_bytes, width, height, model_name='world'):
        """检测目标, 输入 BGRA8 bytes, 返回检测结果"""
        session = self.models.get(model_name)
        if session is None:
            return {}

        # BGRA8 → RGB + resize → NCHW float32
        img = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(height, width, 4)
        img = img[:, :, :3][:, :, ::-1]  # BGRA → BGR → RGB

        input_info = session.get_inputs()[0]
        _, _, model_h, model_w = input_info.shape
        # 动态 shape 时用默认 640
        if isinstance(model_h, str) or isinstance(model_w, str):
            model_h, model_w = 640, 640

        img_resized = cv2.resize(img, (model_w, model_h))
        blob = img_resized.astype(np.float32) / 255.0
        blob = blob.transpose(2, 0, 1)[np.newaxis, ...]

        # 推理
        input_name = input_info.name
        outputs = session.run(None, {input_name: blob})

        # 解码 — BetterGI YoloSharp 输出格式
        return self._decode_yolo(outputs, width, height, model_w, model_h)

    def _decode_yolo(self, outputs, orig_w, orig_h, model_w, model_h):
        """解码 YOLO 输出 (BetterGI YoloSharp 格式)
        参考: BetterGI Core/Recognition/ONNX/YOLO/Predictor.cs
        """
        # TODO: YoloSharp 输出格式与标准 YOLOv8 不同
        # 需要参考 BetterGI 源码确定具体解码逻辑
        # 预期输出: [1, N, 6] (x1,y1,x2,y2,conf,cls) 或类似
        scale_x, scale_y = orig_w / model_w, orig_h / model_h
        results = {}
        # ... 解码逻辑待实现
        return results

    def _load_labels(self, path):
        """加载类别标签"""
        if not os.path.exists(path):
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
```

#### 3.2.3 识别工具箱 — VisionUtils

```python
class VisionUtils:
    """视觉识别工具集 — 封装常用识别模式"""

    def __init__(self, ctx: GameContext, detector: GenshinDetector):
        self.ctx = ctx
        self.detector = detector

    # ── 模板匹配 ──

    def find_template(self, template_path, threshold=0.8, roi=None):
        """查找模板, 返回 MatchResult 或 None"""
        tm = self.ctx.tm
        tm.clearTemplates()
        if roi:
            tm.setRoi(*roi)
        else:
            tm.clearRoi()
        tm.addTemplatePath(template_path, threshold)
        buf = self.ctx.capture()
        count = tm.match(buf)
        if count > 0:
            return tm.getMatch(0)
        return None

    def find_all_templates(self, template_path, threshold=0.8, roi=None):
        """查找所有匹配, 返回 [MatchResult]"""
        tm = self.ctx.tm
        tm.clearTemplates()
        if roi:
            tm.setRoi(*roi)
        else:
            tm.clearRoi()
        tm.addTemplatePath(template_path, threshold)
        buf = self.ctx.capture()
        count = tm.match(buf)
        return [tm.getMatch(i) for i in range(count)]

    def wait_for_template(self, template_path, timeout=10, threshold=0.8, interval=0.5):
        """等待模板出现, 返回 MatchResult 或 None"""
        start = time.time()
        while time.time() - start < timeout:
            result = self.find_template(template_path, threshold)
            if result:
                return result
            time.sleep(interval)
        return None

    # ── OCR ──

    def find_text(self, keyword, roi=None):
        """查找包含关键词的文字, 返回 [(text, OcrResult)]"""
        ocr = self.ctx.ocr
        if roi:
            ocr.setRoi(*roi)
        else:
            ocr.clearRoi()
        buf = self.ctx.capture()
        count = ocr.recognize(buf)
        results = []
        for i in range(count):
            text, result = ocr.getMatch(i)
            if keyword in text:
                results.append((text, result))
        return results

    def wait_for_text(self, keyword, timeout=10, roi=None, interval=0.5):
        """等待文字出现"""
        start = time.time()
        while time.time() - start < timeout:
            results = self.find_text(keyword, roi)
            if results:
                return results
            time.sleep(interval)
        return []

    # ── YOLO ──

    def detect_objects(self, model_name='world', roi=None):
        """YOLO 检测, 返回 {cls: [{x1,y1,x2,y2,conf}]}"""
        buf = self.ctx.capture()
        if roi:
            buf = crop(buf, *roi)
        raw = buf.to_bytes()
        w, h = buf.width, buf.height
        return self.detector.detect(raw, w, h, model_name)

    def has_enemy(self):
        """是否有敌人"""
        dets = self.detect_objects('world')
        return 'enemy_identify' in dets or 'health_bar' in dets

    # ── 游戏状态判断 ──

    def is_loading(self):
        """是否在加载中"""
        return self.find_template("templates/loading.png", 0.7) is not None

    def is_in_dialog(self):
        """是否在对话中"""
        # 检测对话选项区域
        return len(self.find_text("委托", roi=(800, 600, 400, 400))) > 0

    def is_main_ui(self):
        """是否在主界面 (大世界)"""
        # 检测右上角小地图
        return self.find_template("templates/minimap_frame.png", 0.8,
                                  roi=(0, 0, 300, 300)) is not None
```

---

## 4. 功能模块详细设计

### 4.1 自动拾取 (AutoPick) — P0

BetterGI 参考: `GameTask/AutoPick/`

**原理**: YOLO 检测可交互物 → 按F

```python
class AutoPick:
    """自动拾取 — 检测交互物并按F"""

    # 白名单: 这些出现时按F
    PICK_WHITELIST = {'collect', 'interact', 'item'}
    # 黑名单: 这些出现时不按F
    PICK_BLACKLIST = {'enemy_identify'}

    def __init__(self, ctx: GameContext, vision: VisionUtils):
        self.ctx = ctx
        self.vision = vision
        self.enabled = False

    def tick(self):
        """每帧调用, 检测并拾取"""
        if not self.enabled:
            return

        dets = self.vision.detect_objects('world')
        for cls_name, items in dets.items():
            if cls_name in self.PICK_BLACKLIST:
                continue
            if cls_name in self.PICK_WHITELIST:
                self.ctx.press(KeyCode.f)
                time.sleep(0.5)
                return  # 每帧只拾取一个
```

### 4.2 自动剧情 (AutoSkip) — P0

BetterGI 参考: `GameTask/AutoSkip/`

**原理**: 检测对话UI → 点击跳过/选择选项

```python
class AutoSkip:
    """自动剧情 — 跳过对话/选择选项/领日常奖励"""

    def __init__(self, ctx: GameContext, vision: VisionUtils):
        self.ctx = ctx
        self.vision = vision
        self.enabled = False
        self.auto_daily_reward = True  # 自动领日常奖励
        self.auto_dispatch = True      # 自动重新派遣

    def tick(self):
        if not self.enabled:
            return

        buf = self.ctx.capture()

        # 1. 检测并点击"跳过"按钮
        skip = self.vision.find_template("templates/skip_btn.png", 0.8)
        if skip:
            self.ctx.click_center(skip)
            time.sleep(0.3)
            return

        # 2. 检测对话选项 — 优先橙色选项(日常奖励)
        ocr = self.ctx.ocr
        ocr.setRoi(800, 600, 400, 400)  # 对话选项区域
        count = ocr.recognize(buf)
        for i in range(count):
            text, result = ocr.getMatch(i)
            # 橙色选项 = 日常奖励/派遣
            if self.auto_daily_reward and ('委托' in text or '奖励' in text):
                self.ctx.click_center(result)
                time.sleep(0.5)
                return
            # 普通选项 — 点击第一个
            if '选项' in text or any(kw in text for kw in ['好的', '是', '确定']):
                self.ctx.click_center(result)
                time.sleep(0.5)
                return

        # 3. 检测弹出书页 — 关闭
        book = self.vision.find_template("templates/close_btn.png", 0.8)
        if book:
            self.ctx.click_center(book)
            time.sleep(0.3)

        # 4. 通用: 点击屏幕中央继续对话
        self.ctx.click_at(960, 540)
        time.sleep(0.3)
```

### 4.3 快速传送 (QuickTeleport) — P0

BetterGI 参考: `GameTask/QuickTeleport/`

**原理**: 检测地图上的传送点 → 点击 → 确认传送

```python
class QuickTeleport:
    """快速传送 — 在地图上自动点击传送点"""

    def __init__(self, ctx: GameContext, vision: VisionUtils):
        self.ctx = ctx
        self.vision = vision

    def teleport_to_nearest(self):
        """传送至最近的传送点"""
        # 1. 打开地图
        self.ctx.press(KeyCode.m)
        time.sleep(1.5)

        # 2. 查找传送按钮
        tp = self.vision.wait_for_template("templates/teleport_btn.png", timeout=5)
        if tp:
            self.ctx.click_center(tp)
            time.sleep(1.0)

            # 3. 确认传送
            confirm = self.vision.wait_for_template("templates/confirm_teleport.png", timeout=3)
            if confirm:
                self.ctx.click_center(confirm)
                # 等待加载完成
                self._wait_for_load()
                return True

        # 4. 失败, 关闭地图
        self.ctx.press(KeyCode.esc)
        return False

    def _wait_for_load(self, timeout=30):
        """等待传送加载完成"""
        start = time.time()
        while time.time() - start < timeout:
            if not self.vision.is_loading():
                if self.vision.is_main_ui():
                    return True
            time.sleep(0.5)
        return False
```

### 4.4 自动秘境 (AutoDomain) — P1

BetterGI 参考: `GameTask/AutoDomain/AutoDomainTask.cs`

**原理**: 循环: 进入秘境 → 开钥匙 → 战斗 → 走到古树 → 领奖 → 退出

```python
class AutoDomain:
    """自动秘境 — 全自动刷体力"""

    def __init__(self, ctx: GameContext, vision: VisionUtils, fighter):
        self.ctx = ctx
        self.vision = vision
        self.fighter = fighter
        self.run_count = 0
        self.max_runs = 5

    def run(self, max_runs=5):
        """执行秘境循环"""
        self.max_runs = max_runs
        while self.run_count < self.max_runs:
            print(f"[秘境] 第 {self.run_count + 1}/{self.max_runs} 次")
            if not self._run_once():
                print("[秘境] 执行失败, 退出")
                break
            self.run_count += 1
        print(f"[秘境] 完成, 共 {self.run_count} 次")

    def _run_once(self):
        """单次秘境流程"""
        # 1. 开启秘境 (消耗树脂)
        if not self._activate_domain():
            return False

        # 2. 战斗
        if not self.fighter.fight_until_clear(timeout=180):
            return False

        # 3. 走到古树
        if not self._walk_to_tree():
            return False

        # 4. 领取奖励
        if not self._claim_reward():
            return False

        # 5. 退出秘境
        self._exit_domain()
        return True

    def _activate_domain(self):
        """开启秘境"""
        # 检测并点击"开启"按钮
        btn = self.vision.wait_for_template("templates/domain_start.png", timeout=10)
        if btn:
            self.ctx.click_center(btn)
            time.sleep(2.0)
            return True
        return False

    def _walk_to_tree(self):
        """走到古树 (YOLO 检测古树位置)"""
        for _ in range(30):
            dets = self.vision.detect_objects('tree')
            if 'tree' in dets:
                # 走向古树
                self.ctx.press(KeyCode.f)
                time.sleep(1.0)
                return True
            # 向前走
            self.ctx.press(KeyCode.w, holdMs=500)
            time.sleep(0.5)
        return False

    def _claim_reward(self):
        """领取奖励"""
        btn = self.vision.wait_for_template("templates/claim_reward.png", timeout=5)
        if btn:
            self.ctx.click_center(btn)
            time.sleep(1.0)
            return True
        return False

    def _exit_domain(self):
        """退出秘境"""
        self.ctx.press(KeyCode.esc)
        time.sleep(1.0)
        btn = self.vision.wait_for_template("templates/confirm_exit.png", timeout=3)
        if btn:
            self.ctx.click_center(btn)
            self._wait_for_load()

    def _wait_for_load(self, timeout=30):
        start = time.time()
        while time.time() - start < timeout:
            if self.vision.is_main_ui():
                return True
            time.sleep(0.5)
        return False
```

### 4.5 自动伐木 (AutoWood) — P1

BetterGI 参考: `GameTask/AutoWood/`

**原理**: 按Z使用王树瑞佑 → 等待 → 上下线刷新 → 重复

```python
class AutoWood:
    """自动伐木 — 使用王树瑞佑刷木材"""

    def __init__(self, ctx: GameContext, vision: VisionUtils):
        self.ctx = ctx
        self.vision = vision

    def run(self, count=50):
        """伐木 count 次"""
        for i in range(count):
            print(f"[伐木] 第 {i+1}/{count} 次")
            # 1. 按Z使用王树瑞佑
            self.ctx.press(KeyCode.z)
            time.sleep(5.0)  # 等待动画

            # 2. 上下线刷新木材
            self._reconnect()
            time.sleep(3.0)

    def _reconnect(self):
        """断线重连刷新木材"""
        # 打开菜单 → 退出游戏 → 重新进入
        self.ctx.press(KeyCode.esc)
        time.sleep(1.0)
        # 点击"退出游戏"
        self.ctx.click_at(960, 800)
        time.sleep(2.0)
        # 确认退出
        self.ctx.click_at(1100, 650)
        time.sleep(5.0)
        # 重新进入
        self.ctx.click_at(960, 800)
        time.sleep(10.0)
```

### 4.6 自动钓鱼 (AutoFishing) — P1

BetterGI 参考: `GameTask/AutoFishing/`

**原理**: YOLO检测鱼 → 抛竿 → 等待上钩 → 收杆 → 完成钓鱼进度

```python
class AutoFishing:
    """自动钓鱼 — YOLO检测 + 自动抛竿/收杆"""

    def __init__(self, ctx: GameContext, vision: VisionUtils):
        self.ctx = ctx
        self.vision = vision

    def run(self, count=10):
        """钓 count 条鱼"""
        for i in range(count):
            print(f"[钓鱼] 第 {i+1}/{count} 条")
            if not self._fish_one():
                print("[钓鱼] 失败, 退出")
                break

    def _fish_one(self):
        """钓一条鱼"""
        # 1. 检测鱼群
        dets = self.vision.detect_objects('fish')
        if not dets:
            return False

        # 2. 抛竿
        self.ctx.press(KeyCode.f)
        time.sleep(1.0)

        # 3. 等待上钩
        hooked = self.vision.wait_for_template("templates/fish_hooked.png", timeout=15)
        if not hooked:
            return False

        # 4. 收杆
        self.ctx.press(KeyCode.f)
        time.sleep(0.5)

        # 5. 完成钓鱼进度 (拉杆小游戏)
        return self._play_fishing_minigame()

    def _play_fishing_minigame(self, timeout=20):
        """钓鱼进度条小游戏"""
        start = time.time()
        while time.time() - start < timeout:
            # 检测进度条位置, 按住/释放鼠标
            bar = self.vision.find_template("templates/fish_bar.png", 0.7)
            if bar is None:
                # 没有进度条 = 钓鱼完成
                return True
            # 根据进度条位置决定按住/释放
            # 简化: 按住左键
            self.ctx.ic.mouseDown(MouseButton.left)
            time.sleep(0.1)
            self.ctx.ic.mouseUp(MouseButton.left)
            time.sleep(0.05)
        return False
```

### 4.7 路径导航 (AutoPathing) — P2

BetterGI 参考: `GameTask/AutoPathing/`

**原理**: 小地图定位 + 路径数据 + 相机旋转 + WASD移动

```python
class PathExecutor:
    """路径执行器 — 复用 BetterGI JSON 路径数据"""

    def __init__(self, ctx: GameContext, vision: VisionUtils):
        self.ctx = ctx
        self.vision = vision

    def execute(self, path_data):
        """执行路径 JSON"""
        for wp in path_data['waypoints']:
            action = wp.get('action', 'move')
            pos = wp.get('position', {})

            if action == 'teleport':
                self._teleport(pos)
            elif action in ('move', 'path'):
                self._navigate_to(pos)
            elif action == 'combat':
                self._navigate_to(pos)
                # 战斗由外部 fighter 处理
            elif action == 'collect':
                self._navigate_to(pos)
                self.ctx.press(KeyCode.f)
                time.sleep(1.5)
            elif action == 'mine':
                self._navigate_to(pos)
                self.ctx.press(KeyCode.f)
                time.sleep(2.0)

    def _navigate_to(self, target_pos):
        """导航到目标位置"""
        for _ in range(200):  # 最多200步
            current = self._get_player_position()
            if current is None:
                time.sleep(0.5)
                continue

            dx = target_pos['x'] - current[0]
            dy = target_pos['y'] - current[1]
            dist = (dx**2 + dy**2) ** 0.5

            if dist < 5.0:
                return True

            # 旋转视角朝向目标
            self._rotate_camera_to(dx, dy)
            # 向前走
            self.ctx.press(KeyCode.w, holdMs=500)
            time.sleep(0.3)

        return False

    def _get_player_position(self):
        """通过小地图获取玩家位置"""
        buf = self.ctx.capture()
        minimap = crop(buf, 20, 20, 200, 200)
        # 模板匹配全地图
        # TODO: 需要全地图特征数据
        return None

    def _rotate_camera_to(self, dx, dy):
        """旋转相机朝向目标方向"""
        target_angle = math.atan2(dx, -dy)  # 游戏坐标系
        # 通过鼠标中键拖拽旋转视角
        # 简化: moveBy 旋转
        self.ctx.ic.moveBy(int(dx * 0.5), 0)

    def _teleport(self, pos):
        """传送到指定位置"""
        self.ctx.press(KeyCode.m)
        time.sleep(1.5)
        # 点击地图上的传送点
        # TODO: 需要地图坐标到屏幕坐标的映射
        self.ctx.press(KeyCode.esc)

    def load_path(self, name):
        """加载 BetterGI 格式路径文件"""
        path = os.path.join("paths", f"{name}.json")
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
```

### 4.8 简化战斗 (SimpleFighter) — P2

BetterGI 参考: `GameTask/AutoFight/`

```python
class SimpleFighter:
    """简化战斗 — 固定连招 + 技能CD检测"""

    def __init__(self, ctx: GameContext, vision: VisionUtils):
        self.ctx = ctx
        self.vision = vision
        # 默认连招: E → Q → 普攻5次
        self.combo = [
            (KeyCode.e, 500),
            (KeyCode.q, 1000),
            ('attack', 200), ('attack', 200),
            ('attack', 200), ('attack', 200),
            ('attack', 200),
        ]

    def fight_until_clear(self, timeout=120):
        """战斗直到清敌, 返回是否成功"""
        start = time.time()
        while time.time() - start < timeout:
            if not self.vision.has_enemy():
                return True
            self._do_combo()
            time.sleep(0.3)
        return False

    def _do_combo(self):
        """执行连招"""
        for action, delay in self.combo:
            if action == 'attack':
                self.ctx.click_at(960, 540)  # 攻击方向
            else:
                self.ctx.press(action)
            time.sleep(delay / 1000.0)

    def switch_character(self, slot):
        """切换角色 (1-4)"""
        key = [KeyCode.num1, KeyCode.num2, KeyCode.num3, KeyCode.num4][slot - 1]
        self.ctx.press(key)
        time.sleep(0.5)
```

---

## 5. AI 任务编排

### 5.1 核心思路

不用框架、不用状态机。AI (LLM) 根据用户意图直接生成 Python 脚本，脚本调用 avc API 执行。

```
用户意图 → LLM 生成 Python 脚本 → 代码校验 → 沙箱执行
```

### 5.2 暴露给 AI 的 API 清单

```python
# === 截图 ===
ctx.capture()                          # 截图, 返回 IImageBuffer
ctx.click_at(buf_x, buf_y)             # 截图坐标点击
ctx.click_center(result)               # 点击识别结果中心
ctx.press(key, holdMs=0)               # 按键

# === 识别 ===
vision.find_template(path, threshold)  # 模板匹配 → MatchResult | None
vision.find_text(keyword, roi)          # OCR查找 → [(text, OcrResult)]
vision.detect_objects(model)            # YOLO检测 → {cls: [{x1,y1,x2,y2,conf}]}
vision.has_enemy()                      # 是否有敌人
vision.is_main_ui()                     # 是否主界面
vision.is_loading()                     # 是否加载中
vision.wait_for_template(path, timeout) # 等待模板出现
vision.wait_for_text(keyword, timeout)  # 等待文字出现

# === 路径 ===
path_executor.execute(path_data)        # 执行路径
path_executor.load_path(name)           # 加载路径文件

# === 战斗 ===
fighter.fight_until_clear(timeout)      # 战斗直到清敌
fighter.switch_character(slot)          # 切换角色

# === 工具 ===
sleep(seconds)                          # 等待 (自动加随机抖动)
wait_until(predicate, timeout)          # 等待条件满足
```

### 5.3 ScriptRunner 实现

```python
class ScriptRunner:
    """脚本执行器 — AI 生成代码 + 安全校验 + 沙箱执行"""

    def __init__(self, ctx, vision, detector, fighter, path_executor):
        self.ctx = ctx
        self.vision = vision
        self.detector = detector
        self.fighter = fighter
        self.path_executor = path_executor
        self.planner = AITaskPlanner()
        self.validator = CodeValidator()
        self.cache = TaskCodeCache()

    def run_intent(self, user_intent: str):
        """从用户意图到执行"""
        # 1. 查缓存
        code = self.cache.get(user_intent)
        if not code:
            # 2. AI 生成
            game_state = self._get_game_state()
            code = self.planner.plan(user_intent, game_state)
            # 3. 校验
            ok, err = self.validator.validate(code)
            if not ok:
                print(f"[安全] 校验失败: {err}")
                return False
            # 4. 缓存
            self.cache.put(user_intent, code)

        # 5. 执行
        return self._exec(code)

    def _exec(self, code: str, timeout=600):
        """沙箱执行"""
        safe_globals = {
            'ctx': self.ctx,
            'vision': self.vision,
            'detector': self.detector,
            'fighter': self.fighter,
            'path_executor': self.path_executor,
            'sleep': lambda s: time.sleep(s * random.uniform(0.8, 1.2)),
            'wait_until': self._wait_until,
            'print': print,
            'len': len, 'range': range,
            'int': int, 'float': float, 'str': str,
            'abs': abs, 'min': min, 'max': max,
            'time': time, 'random': random, 'math': math,
            'KeyCode': KeyCode,
        }
        try:
            exec(code, safe_globals)
            return True
        except Exception as e:
            print(f"[执行] 错误: {e}")
            return False
```

---

## 6. 项目结构

```
D:\Work\github\avc_genshin\
  main.py                    # 入口: 初始化 + 接收用户意图
  config.py                  # 配置 (窗口名、分辨率、模型路径、LLM API key)
  context.py                 # GameContext (avc 实例管理)
  detector.py                # GenshinDetector (YOLO 检测)
  vision_utils.py            # VisionUtils (识别工具箱)
  auto_pick.py               # 自动拾取
  auto_skip.py               # 自动剧情
  quick_teleport.py          # 快速传送
  auto_domain.py             # 自动秘境
  auto_wood.py               # 自动伐木
  auto_fishing.py            # 自动钓鱼
  auto_leyline.py            # 自动地脉花
  path_executor.py           # 路径导航
  fighter.py                 # 简化战斗
  ai_planner.py              # AI 任务编排
  script_runner.py           # 脚本执行器
  code_validator.py          # 代码校验
  task_cache.py              # 脚本缓存
  models/                    # ONNX 模型 (从 BetterGI Release 提取)
    bgi_world.onnx
    bgi_fish.onnx
    bgi_mine.onnx
    bgi_tree.onnx
    avatar_side_classify_sim.onnx
    q_classify_sim.onnx
    avatar.onnx
    label.json
  templates/                 # 模板匹配图片
    skip_btn.png
    teleport_btn.png
    confirm_teleport.png
    domain_start.png
    claim_reward.png
    fish_hooked.png
    fish_bar.png
    loading.png
    minimap_frame.png
    close_btn.png
    confirm_exit.png
  paths/                     # BetterGI 格式路径 JSON
    collection/
    mining/
    domain/
  map/                       # 地图数据
    teyvat_full_map.png
  cache/                     # AI 生成脚本缓存
    tasks/
  logs/                      # 运行日志
  debug/                     # 调试截图
```

---

## 7. 开发阶段

| 阶段 | 内容 | 优先级 | 依赖 |
|------|------|--------|------|
| **一** | 基础链路: GameContext + detector.py + 截图→推理原型 | P0 | avc Python SDK |
| **二** | 操作闭环: 坐标转换 + 拟人化 + 模板匹配验证 | P0 | 阶段一 |
| **三** | P0功能: AutoPick + AutoSkip + QuickTeleport | P0 | 阶段二 |
| **四** | AI编排: ai_planner + code_validator + script_runner | P0 | 阶段三 |
| **五** | P1功能: AutoDomain + AutoWood + AutoFishing | P1 | 阶段三 |
| **六** | 导航: 小地图定位 + PathExecutor + BetterGI路径 | P2 | 阶段五 |
| **七** | 战斗: SimpleFighter + 角色识别 + CD检测 | P2 | 阶段五 |
| **八** | P2功能: 自动采集/挖矿 + 地脉花 | P2 | 阶段六+七 |
| **九** | 稳定性: 长时运行 + 边界case + 拟人化调参 | P3 | 全部 |

### 里程碑

| 里程碑 | 阶段 | 可用功能 |
|--------|------|---------|
| **M1: 闭环** | 一+二 | 截图→检测→操作 |
| **M2: 基础功能** | 三 | 自动拾取/剧情/传送 |
| **M3: AI编排** | 四 | AI生成脚本+执行 |
| **M4: 进阶功能** | 五 | 秘境/伐木/钓鱼 |
| **M5: 导航+战斗** | 六+七 | 路径导航+战斗 |
| **M6: 完整版** | 八+九 | 采集/挖矿+稳定 |

---

## 8. 需要补充的 avc SWIG 绑定

| 优先级 | API | 说明 | C++ 源文件 |
|--------|-----|------|-----------|
| P0 | `IInputController.setHumanize(bool)` | 启用拟人化 (贝塞尔轨迹+自然点击) | `BaseInputController.hpp` |
| P0 | `IInputController.setJitterSeed(uint32)` | 固定随机种子 | 同上 |
| P0 | `IInputController.setClickHoldMs(int32)` | 点击按住时长 | 同上 |
| P0 | `IScreenCapture.activateWindow(name)` | 已有, 确认可用 | `AvcInput.h` |
| P1 | `IONNXSession` 全套 | ONNX 推理 (避免依赖 onnxruntime Python) | `vision/IONNXSession.hpp` |
| P2 | `IInputController.hold(key, duration_ms)` | 按住键指定时长 | 已有 press(key, holdMs) |
| P2 | `findWindowByName(name)` | 已有, 确认可用 | `AvcInput.h` |

---

## 9. 风险与注意事项

### 9.1 反检测 (必须)
- 启用拟人化: 贝塞尔轨迹 + 随机延迟 + 自然点击
- 操作间隔加抖动: 0.8-1.2x 随机
- 原神管理员权限运行, 操作程序也必须管理员权限

### 9.2 YOLO 模型兼容性
- BetterGI ONNX 模型用 YoloSharp 导出, 输出格式与标准 YOLOv8 不同
- 类别标签在 `label.json` 中
- **解码 YOLO 输出最容易踩坑**, 参考 BetterGI `Core/Recognition/ONNX/YOLO/Predictor.cs`

### 9.3 分辨率依赖
- 模型和坐标基于 1920x1080, 原神必须 1920x1080 窗口模式
- 启动时检查分辨率, 不匹配则报错

### 9.4 游戏更新
- 版本更新可能改变 UI 布局, 模板匹配失效需更新
- BetterGI 社区会跟进, 可同步更新模型和路径数据

### 9.5 法律与合规
- 仅供技术研究和学习使用
- 使用自动化工具可能违反游戏服务条款

---

## 10. BetterGI 源码参考索引

| 功能 | BetterGI 源码路径 | 说明 |
|------|-------------------|------|
| YOLO 推理 | `Core/Recognition/ONNX/BgiYoloPredictor.cs` | YOLO 检测封装 |
| YOLO 输出解码 | `Core/Recognition/ONNX/YOLO/Predictor.cs` | **必读** — 输出格式解析 |
| 模型注册 | `Core/Recognition/ONNX/BgiOnnxModel.cs` | 所有模型路径 |
| OCR | `Core/Recognition/OCR/` | PaddleOCR ONNX 版 |
| 模板匹配 | `Core/Recognition/OpenCv/` | OpenCvSharp |
| 截图 | `Fischless.GameCapture/` | BitBlt/DwmSharedSurface |
| 操作 | `Fischless.WindowsInput/` | PostMessage/SendInput |
| 路径执行 | `GameTask/AutoPathing/PathExecutor.cs` | 路径导航核心 |
| 相机旋转 | `GameTask/AutoPathing/CameraRotateTask.cs` | 视角旋转 |
| 陷阱脱出 | `GameTask/AutoPathing/TrapEscaper.cs` | 卡住检测与脱出 |
| 小地图定位 | `GameTask/Common/Map/MiniMap/` | SIFT 特征匹配 |
| 自动战斗 | `GameTask/AutoFight/` | 完整战斗系统 |
| 自动秘境 | `GameTask/AutoDomain/AutoDomainTask.cs` | 秘境自动化 |
| 自动钓鱼 | `GameTask/AutoFishing/` | 钓鱼检测与操作 |
| 自动拾取 | `GameTask/AutoPick/` | 拾取检测与按F |
| 自动剧情 | `GameTask/AutoSkip/` | 剧情跳过 |
| 一条龙 | `GameTask/Common/Job/` | 组合任务编排 |
