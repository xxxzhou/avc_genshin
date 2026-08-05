"""avc_genshin 领域能力（L2 abilities/）。

被 ``g.*`` 和守护**内部调用**的原神领域能力。任务作者一般用 ``g.*`` 即可，
仅在降级（``vision.*``）或写新守护/领域能力时才 ``from abilities import ...``。

模块（docs/design/07）：
    vision_utils   find_template/find_text/crop（封装 avc 模板/OCR）
    detector       GenshinDetector（YOLO + YoloSharp 解码）
    navigation     PathExecutor + 小地图定位（阶段七）
    fighter        SimpleFighter（阶段 C：站桩战斗 + 血条索敌 + Q 检测）
    game_state     UI/场景特征检测（阶段三 SceneEstimator 用）
    reward         树脂奖励领取（auto_boss/auto_ley_line/auto_domain 共用，OCR 判文案）
"""
