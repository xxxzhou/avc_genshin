# 08 BGI 对齐核查（B 系列差异报告）

> 目的：对 Phase D 新增的 4 个领域能力（领邮件/合成树脂/尘歌壶/自动秘境）+ 秘境坐标，与 BGI 源码逐行对照。
> 原则见「三层归属准则」与「对齐 BGI 优先」：**已修复的标记 ✅→，v1 可接受的差距标记 ⚠️（待实机验证后决定）**。
> 核查日期：2026-08-07。

---

## B1 秘境坐标来源

**问题**：`domain.py _DOMAIN_COORDS` 硬编码 4 个坐标，3 个错误、1 个无效：

| 旧坐标 | BGI tp.json 真值 | 差异 | 结论 |
|---|---|---|---|
| 绝缘之境 (6408,1624) | 无此名（真名 **椛染之庭** (-3775,-2368)） | — | 别名未映射 |
| 华池岩岫 (1845,1455) | (1291,1430) | 554 | 错误 |
| 芬德尼尔之顶 (1015,2515) | (1039,-826) | 3341 | Y 符号反 |
| 太山府 (1545,210) | (659,1168) | 1305 | 错误 |

**BGI 做法**：`MapLazyAssets.DomainPositionMap` 从 `tp.json` **动态构建**（`Type in ("BlessDomain","ForgeryDomain","MasteryDomain")`，35 个可刷秘境），不硬编码。

**✅→修复**：`get_domain_coords` 改为查 tp.json（`TpDatabase.find_by_name`，懒加载单例），加 `_DOMAIN_ALIASES` 别名表（绝缘之境→椛染之庭）。坐标与 `teleport_to` 同系（`position[0]`=X、`position[2]`=Y）。

**坑**：tp.json 字段是小写 `type`（BGI 靠 Newtonsoft 大小写不敏感映射到 C# `Type`），勿找大写 `Type`。

---

## B2 mail.py vs BGI `ClaimMailRewardsTask`

| 项 | BGI | 我们 | 结论 |
|---|---|---|---|
| ESC 前确保主界面 | 开头 `ReturnMainUiTask.Start` | 直接 ESC | ⚠️→ 修 |
| 派蒙菜单展开 | Delay 1300 | sleep 1.3 | ✅ |
| 邮件图标 ROI | `@mailReward`=(0,540,192,540) | `_MAIL_ICON_ROI` | ✅ |
| 邮件图标点击后 | Delay 1000 | sleep 1.0 | ✅ |
| collect ROI | `@collect`=(0,720,480,360) | `_COLLECT_ROI` | ✅ |
| collect 点击后 | Delay 200 + ESC | sleep 0.3 + ESC | ✅ |
| 无邮件 | log + 尾部 ReturnMainUiTask | `_close_menu` 返回 True | ✅ |
| 模板阈值 | RecognitionObject 默认 0.8 | 0.7 | ⚠️→ 修 |
| 尾部收尾 | ReturnMainUiTask（含 BtnExitDoor 特判） | `_close_menu`（纯 ESC×3） | ⚠️ v1 接受 |

**修复**：加 `_ensure_main_ui`（场景守护已知主界面则跳过，否则 ESC 回主界面）；阈值 0.7→0.8。

---

## B3 craft.py vs BGI `GoToCraftingBenchTask.GoCraftResin`

| 项 | BGI | 我们 | 结论 |
|---|---|---|---|
| 合成台路径 | `合成台_{country}.json` | 同 | ✅ |
| F 进入合成 | `FindFAndPress("合成")` + IsInTalkUi + MoveBackward 重试 | `_press_f_to_enter`（DIALOG 场景 + 3 重试） | ⚠️ v1 接受 |
| 选对话选项 | `SelectLastOptionUntilEnd` 停判 BtnWhiteConfirm | `_select_last_option` 停判无选项 | ⚠️ v1 接受 |
| 树脂图标 ROI | `@craftCondensedResin`=(960,0,960,720) | `_CRAFT_RESIN_ROI` | ✅ |
| 无可合成 | log + ESC | ESC + 返回 | ✅ |
| 确认 | BtnWhiteConfirm + BtnBlackConfirm（全屏） | 同 | ✅ |
| 数量控制 | MinResinToKeep + 增减按钮 + OCR 计数 | 直接最大量（v1 简化） | ⚠️ v1 接受 |
| 模板阈值 | 0.8 | 0.7 | ⚠️→ 修 |

**修复**：阈值 0.7→0.8（树脂模板 + 白/黑确认）。

---

## B4 pot.py vs BGI `GoToSereniteaPotTask`

| 项 | BGI | 我们 | 结论 |
|---|---|---|---|
| 进入尘歌壶 | OpenMap→SwitchArea→缩放→住宅图标→传送按钮 | `teleport_to("尘歌壶")` 委托传送链 | ⚠️ v1 接受 |
| 找阿圆 | FindAYuan（中键回正+旋转视角+前进靠近） | OCR 阿圆 + 按 F（等可见） | ⚠️ v1 接受 |
| 信任对话 | `SingleSelectText("信任")` | `g.talk(_TRUST)` | ✅ |
| 好感 ROI | `@potLove`=(1680,540,240,270) | **全屏** | ⚠️→ 修 |
| 好感数 0 跳过 | OCR `0/8` 判断 | 无 | ⚠️ v1 接受 |
| 无法领取提示 | OCR + 点击关闭 | 无 | ⚠️ v1 接受 |
| 宝钱 ROI | `@potMoney`=(960,810,480,270) | **全屏** | ⚠️→ 修 |
| 页面关闭 ROI | `@potPageClose`=(960,216,480,135) | **全屏** | ⚠️→ 修 |
| 收尾关闭 ROI | `@pageCloseWhite`=(1680,0,240,135) | **全屏** | ⚠️→ 修 |
| 退出 | Finished：点 PageCloseWhite → 选再见 → Tp(4508,3630) | ESC → talk 再见 → teleport(4508.97,3630.56) | ⚠️→ 修 |
| 洞天商店购买 | BuyMaxNumber（按配置） | 无 | ⚠️ v1 接受 |
| 模板阈值 | 0.8 | 0.7 | ⚠️→ 修 |

**修复**：4 个 pot 模板补 BGI ROI（`_POT_ROI`，顺带解决 avc 全屏 ~13s 慢匹配）；阈值 0.8；退出改「先点 PageCloseWhite 再 talk 再见」（去掉 ESC）。

---

## B5 domain.py vs BGI `AutoDomainTask`

| 项 | BGI | 我们 | 结论 |
|---|---|---|---|
| 秘境坐标 | DomainPositionMap（tp.json） | tp.json + 别名（B1 已修） | ✅ |
| 传送后移动 | 各秘境特定移动（芬德尼尔之顶后退等） | `_walk_and_press_f` 等 F 图标 | ⚠️ v1 接受 |
| 单人挑战确认 | **AutoFight `Confirm`=confirm.png（右半）** | btn_white_confirm.png（全屏） | ⚠️→ 修（模板错） |
| 队伍选择 ROI | `@partyChooseView`=(0,960,274,120) | 全屏 | ⚠️→ 修 |
| 开始挑战确认 | Confirm（右半） | btn_white_confirm.png | ⚠️→ 修 |
| 关秘境提示 | CloseDomainTip（地脉异常+点击任意处关闭） | 无 | ⚠️ v1 接受 |
| 战斗 | 战斗脚本 + 结束检测（挑战达成/自动退出 OCR） | `g.fight_until_clear` | ⚠️ v1 接受 |
| 找石化古树 | YOLO 树检测 + 摄像机锁东 + 左右移动 | 无（v1 OCR 简化） | ⚠️ v1 接受 |
| 领奖交互 | WalkToPressF（按 F）→ OCR 石化古树**点击** | OCR 石化古树 → **按 F** | ⚠️ v1 接受 |
| 树脂不足 | `数量不足/补充原粹树脂` → ExitDomain | `补充原粹树脂` → 返回 False → NormalEnd | ✅ |
| 退出 | ESC×2 + ClickBlackConfirmButton（全屏） | ESC×2 + btn_black_confirm（全屏） | ✅ |
| 循环 | TpDomain 一次 + loop 轮次 | auto_domain 每次 re-enter | ✅ |
| 复活重试/树脂20/40/圣遗物分解 | 有 | 无 | ⚠️ v1 接受 |
| 模板阈值 | 0.8 | 0.7 | ⚠️→ 修 |

**修复**：确认按钮换 `confirm.png` + `_CONFIRM_ROI`(960,540,960,540)；`party_btn_choose_view` + `_PARTY_VIEW_ROI`(0,960,274,120)；阈值 0.8。

---

## B6 路径资源核对

| 资源 | 现状 |
|---|---|
| `paths/craft/合成台_{蒙德/璃月/稻妻/枫丹}.json` | ✅ 4 个齐全，`load_path_task` 正常加载（8/3/6/2 航点） |
| `paths/boss/*` | ✅ 40+（半永恒统辖矩阵等） |
| `paths/guild/*` | ✅ 6 国冒险家协会 |
| `paths/ley_line/*` | ✅ |
| 秘境路径 | 无需补——坐标已并入 tp.json（与 BGI DomainPositionMap 同源），不另存路径文件 |

---

## 汇总：统一原则

1. **模板阈值统一 0.8**（对齐 BGI `RecognitionObject` 默认）。旧代码多处 0.7 且有 E1 离线筛查见错误场景最高 0.71 的证据——0.7 会误收。
2. **BGI 有 ROI 的模板必须带 ROI**：既对齐又规避 avc 朴素模板匹配全屏 ~13s 慢匹配（当前问题 #5）。
3. 其余差距均为 **v1 明确简化项**（见各行「v1 接受」），实机跑通后按需补：MinResinToKeep 数量控制、FindAYuan 旋转、CloseDomainTip、YOLO 树检测、石化古树点击交互、复活重试、树脂 20/40 切换、圣遗物分解。
