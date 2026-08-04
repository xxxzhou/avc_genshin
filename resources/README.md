# resources/ —— 游戏资源（数据，非代码）

| 子目录 | 内容 | git | 来源 |
|---|---|---|---|
| `templates/` | 模板图（1080p 基准，按功能分子目录） | ✅ | 自截 / BetterGI |
| `paths/` | BetterGI 路径 JSON | ✅ | BGI 社区 |
| `models/` | ONNX 模型（world/avatar/...） | ❌ 大文件 | BGI Release |
| `ocr/` | OCR 模型/字典 | ❌ | BGI / avc 内置 |
| `map/` | 全地图数据（300M+） | ❌ | BGI Release |

访问统一走 `framework.resources.res`（**禁止硬编码相对路径**）：

```python
from framework.resources import res
res.template("daily/katherine.png")   # resources/templates/...
res.model("bgi_world.onnx")           # resources/models/...，缺失则回退 BGI_ROOT
```

> 大文件（models/map/ocr）已在 `.gitignore`；设 `BGI_ROOT` 环境变量指向本地
> BetterGI 仓库即可零拷贝复用其模型/全地图（`docs/design/03 §5.2`）。
