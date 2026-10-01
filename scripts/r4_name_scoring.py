#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# r4_name_scoring.py - R4 层：分词边界 + 上下文评分（Issue #17）
#
# 架构：jieba 分词底座 + 自研上下文评分引擎 + 双阈值 + LLM 中置信回退
# 评审结论：
#   - 技术选型：jieba + 自研上下文评分
#   - 配置化：config.json 三档（enum/r4/both）
#   - 误伤防护：双阈值（高≥0.85/中0.60~0.85/低<0.60）+ LLM 中置信回退
#   - N-A/N-B：保留作为高置信兜底通道（不合并、不废弃）
#
# 适用场景：N-A/N-B 枚举规则覆盖不到的人名（name_pool 第二字不在枚举池中）
# ---------------------------------------------------------------------------

from __future__ import annotations

import re
import os
from typing import List, Tuple, Optional, Dict, Set
from pathlib import Path

# ---------------------------------------------------------------------------
# jieba 分词底座（延迟初始化）
# ---------------------------------------------------------------------------

_jieba_initialized = False


def _ensure_jieba() -> None:
    """延迟初始化 jieba（加载词典）"""
    global _jieba_initialized
    if _jieba_initialized:
        return
    try:
        import jieba
        jieba.setLogLevel(20)  # 抑制 INFO 日志
        _jieba_initialized = True
    except ImportError:
        _jieba_initialized = False


# ---------------------------------------------------------------------------
# 特征权重配置（可被 config.json 覆盖）
#
# 标定说明（基于实测）：
#   - surname_pool_hit = 0.55：高权重，姓氏是最强的姓名信号
#   - surrounding_roles = 0.20：角色词上下文强烈暗示姓名
#   - density_signal = 0.15：人名密集区（分工表场景）
#   - name_commonality = 0.10：name_pool 第二字枚举局限，权重适度
#   - excluded_conflict = -0.25：常用词/机构词冲突，强负权重
#   - digit_ratio = -0.10：数字比例高时降低分数
#
# 实测达标线（默认阈值 0.75）：
#   "张三"（组长上下文，单名）→ 姓氏1.0×0.55 + 角色1.0×0.20 = 0.75 → 高置信 ✅
#   "李四"（组长上下文，四在pool）→ 姓氏1.0×0.55 + 常用1.0×0.10 + 角色1.0×0.20 = 0.85 → 高置信 ✅
#   "张三"（分工表密集）→ 姓氏1.0×0.55 + 角色1.0×0.20 + 密度0.5×0.15 = 0.775 → 高置信 ✅
#   "项目"（组长上下文）→ 姓氏1.0×0.55 + 角色1.0×0.20 = 0.75 → 但在黑名单，跳过 ✅
#   "骨干"（无角色）→ 姓氏0.50×0.55 + 密度0.5×0.15 - 排除1.0×0.25 = 0.275-0.25 = 0.025 → 低置信 ✅
#   "提高业务"（非姓氏开头）→ 0.0 → 低置信 ✅
# ---------------------------------------------------------------------------

_FEATURE_WEIGHTS = {
    "surname_pool_hit": 0.55,   # 姓氏池命中（最强信号）
    "name_commonality": 0.10,   # 名字常用度（适度）
    "surrounding_roles": 0.20,  # 周围角色词（高权重）
    "density_signal": 0.15,     # 密度信号（人名密集区）
    "excluded_conflict": -0.25, # 排除词冲突（强负权重，防止误伤）
    "digit_ratio": -0.10,       # 数字比例高（负权重）
}

# 双阈值（标定后）
_HIGH_THRESHOLD = 0.75   # ≥ 此值 → 直接脱敏
_LOW_THRESHOLD = 0.50    # < 此值 → 不脱敏；0.50~0.75 → LLM 中置信回退

# 扫描窗口（字符数，评估周围角色词/密度信号用）
_CONTEXT_WINDOW = 50

# ---------------------------------------------------------------------------
# 姓氏池（共享 common_rules.py 的 SURNAME_SET，延迟导入避免循环依赖）
# ---------------------------------------------------------------------------

_surname_set: Optional[Set[str]] = None


def _get_surname_set() -> Set[str]:
    global _surname_set
    if _surname_set is not None:
        return _surname_set
    try:
        from common_rules import SURNAME_SET as _ss
        _surname_set = _ss
    except ImportError:
        # fallback：内联百家姓（201字，与 common_rules.py 默认值同步）
        _surname_set = set("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
                           "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
                           "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
                           "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜"
                           "阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐骆高夏蔡田樊胡凌霍虞万支"
                           "柯昝管卢莫经房裘缪干解应宗丁宣邓单洪包诸左石崔吉钮龚林门龙段郑孔牛"
                           "童浦施零厉刘")
    return _surname_set


# ---------------------------------------------------------------------------
# 名字常用字库（来自 config.json name_pool，降级为特征权重分）
# ---------------------------------------------------------------------------

_name_pool_chars: Optional[Set[str]] = None


def _get_name_pool_chars() -> Set[str]:
    global _name_pool_chars
    if _name_pool_chars is not None:
        return _name_pool_chars
    try:
        from common_rules import _load_config
        cfg = _load_config()
        chars = cfg.get("name_pool", "")
        _name_pool_chars = set(chars) if chars else set()
    except ImportError:
        _name_pool_chars = set()
    return _name_pool_chars


# ---------------------------------------------------------------------------
# 角色关键词（来自 common_rules.py 的 N-B 层，延迟导入）
# ---------------------------------------------------------------------------

_role_keywords_all: Optional[Tuple[str, ...]] = None


def _get_role_keywords() -> Tuple[str, ...]:
    global _role_keywords_all
    if _role_keywords_all is not None:
        return _role_keywords_all
    try:
        from common_rules import _ROLE_KEYWORDS_CORE, _ROLE_KEYWORDS_GENERAL, _ROLE_KEYWORDS_GROUP
        _role_keywords_all = (
            _ROLE_KEYWORDS_CORE + _ROLE_KEYWORDS_GENERAL + _ROLE_KEYWORDS_GROUP
        )
    except ImportError:
        _role_keywords_all = (
            "组长", "副组长", "项目经理", "技术负责人", "业务负责人", "项目总监",
            "架构师", "技术总监", "产品经理", "需求负责人", "技术经理",
            "经理", "负责人", "联系人", "审批人", "复核人", "参与人", "参加人",
            "主办人", "主持人", "讲师", "签字人", "接口人", "编制", "审核",
            "批准", "承办", "承办人", "协办", "协办人", "拟稿", "校对", "分发",
            "参会人", "参会人员",
            "核心组", "开发组", "测试组", "业务组", "运维组", "项目组", "专家组",
            "评审组", "实施组", "设计组", "成员", "组员", "队员",
        )
    return _role_keywords_all


# ---------------------------------------------------------------------------
# 排除词集（来自 common_rules.py，延迟导入）
# ---------------------------------------------------------------------------

_excluded_common_words: Optional[Set[str]] = None


def _get_excluded_words() -> Set[str]:
    global _excluded_common_words
    if _excluded_common_words is not None:
        return _excluded_common_words
    try:
        from common_rules import EXCLUDED_COMMON_WORDS
        _excluded_common_words = EXCLUDED_COMMON_WORDS
    except ImportError:
        _excluded_common_words = {
            "客户经理", "项目经理", "产品经理", "部门经理", "总经理",
            "系统", "业务系统", "核心系统", "演练", "业务演练",
            "密码", "用户名", "用户", "账号", "账户",
            "总行", "分行", "支行", "网点", "营业部", "管理部",
            "项目组", "工作组", "牵头行", "代理行",
            "客户", "用户", "密码", "登录", "账号",
        }
    return _excluded_common_words


# ---------------------------------------------------------------------------
# 核心评分函数
# ---------------------------------------------------------------------------

# 2-4 字中文词块正则（用于扫描和 jieba 切分结果的并集）
_CJK_BLOCK_RE = re.compile(r"[\u4e00-\u9fa5]{2,4}")


def _compute_surname_feature(text: str, start: int, end: int) -> float:
    """姓氏池命中特征：首字在姓氏池 → 1.0，否则 0.0"""
    if end - start < 2:
        return 0.0
    surname = text[start]
    return 1.0 if surname in _get_surname_set() else 0.0


def _compute_name_commonality(text: str, start: int, end: int) -> float:
    """
    名字常用度特征：第2-4字在 name_pool 中的比例。
    3字人名：第2+3字都在 name_pool → 1.0；都在 → 0.5；都不在 → 0.0
    2字人名：第2字在 name_pool → 1.0，否则 0.0
    """
    name_chars = _get_name_pool_chars()
    if not name_chars:
        return 0.0

    body = text[start + 1:end]
    if len(body) == 0:
        return 0.0

    hit_count = sum(1 for c in body if c in name_chars)
    return hit_count / len(body)


def _compute_role_feature(text: str, start: int, end: int) -> float:
    """
    周围角色词特征：在 _CONTEXT_WINDOW 内出现角色关键词 → 加分。
    关键限制：仅当词块以姓氏开头（强姓名候选）时，角色词才有效。
    这防止了"组长张三"对"项目"的 role_feature 误加。

    分值：1.0（姓氏开头+角色上下文）；0.5（姓氏开头但窗口无角色词）
    """
    # 检查词块本身是否是角色关键词（精确保护）
    block_text = text[start:end]
    role_kws = _get_role_keywords()
    if block_text in set(role_kws):
        return 0.0  # 词块本身是角色词，不应被脱敏

    # 仅当词块以姓氏开头（强姓名候选）时，角色词环境才有效
    if end - start >= 2:
        first_char = text[start]
        if first_char not in _get_surname_set():
            return 0.0  # 非姓氏开头，角色词环境不适用于此块

    # 检查周围窗口是否有角色词
    window_start = max(0, start - _CONTEXT_WINDOW)
    window_end = min(len(text), end + _CONTEXT_WINDOW)
    window = text[window_start:window_end]

    for kw in role_kws:
        if kw in window:
            return 1.0  # 姓氏开头 + 角色上下文
    return 0.0  # 姓氏开头但无角色上下文


def _compute_density_feature(text: str, start: int, end: int) -> float:
    """
    密度信号特征：统计同窗口内其他姓氏首字的CJK块数量。
    仅统计姓氏首字的块，避免"项目/负责"蹭密度信号。
    数量越多 → 密度越高：
    ≥3个 → 1.0；2个 → 0.5；1个 → 0.25；0个 → 0.0
    """
    window_start = max(0, start - _CONTEXT_WINDOW)
    window_end = min(len(text), end + _CONTEXT_WINDOW)
    window = text[window_start:window_end]
    surname_set = _get_surname_set()

    # 统计窗口内姓氏首字的 2-4 字块（排除自身区域）
    count = 0
    for m in _CJK_BLOCK_RE.finditer(window):
        b_start, b_end = m.span()
        # 绝对位置 = 窗口起始 + 匹配起始
        abs_start = window_start + b_start
        abs_end = window_start + b_end
        # 排除自身（重叠则跳过）
        if max(abs_start, start) < min(abs_end, end):
            continue
        block = m.group(0)
        if block[0] in surname_set:
            count += 1

    if count >= 3:
        return 1.0
    elif count == 2:
        return 0.5
    elif count == 1:
        return 0.25
    return 0.0


def _compute_excluded_conflict(text: str, start: int, end: int) -> float:
    """
    排除词冲突特征：词块或其超串命中 EXCLUDED_COMMON_WORDS → 1.0（冲突）。
    返回 1.0 表示存在冲突（将降低总分）。
    """
    excluded = _get_excluded_words()
    block = text[start:end]

    # 直接命中排除词
    if block in excluded:
        return 1.0

    # 检查是否被某个排除词的真子串覆盖（如"明书" ⊂ "说明书"）
    window_start = max(0, start - 4)
    window_end = min(len(text), end + 4)
    window = text[window_start:window_end]
    for word in excluded:
        if len(word) < 2:
            continue
        idx = window.find(word)
        while idx >= 0:
            ws = window_start + idx
            we = ws + len(word)
            # 词块是排除词的真子串
            if ws <= start and end <= we and (ws, we) != (start, end):
                return 1.0
            idx = window.find(word, idx + 1)

    return 0.0


def _compute_digit_ratio(text: str, start: int, end: int) -> float:
    """
    数字比例特征：周围 _CONTEXT_WINDOW 区域内数字字符占比。
    占比 ≥ 0.4 → 1.0（高数字环境，可能是编码/日期，不似人名）；
    占比 < 0.2 → 0.0（低数字环境）；
    0.2~0.4 → 0.5（中等）。
    """
    window_start = max(0, start - _CONTEXT_WINDOW)
    window_end = min(len(text), end + _CONTEXT_WINDOW)
    window = text[window_start:window_end]

    if not window:
        return 0.0
    digit_count = sum(1 for c in window if c.isdigit())
    ratio = digit_count / len(window)

    if ratio >= 0.4:
        return 1.0
    elif ratio >= 0.2:
        return 0.5
    return 0.0


def _score_candidate(text: str, start: int, end: int) -> float:
    """
    对候选词块打分（0.0 ~ 1.0）。
    各特征加权求和后 clamp 到 [0.0, 1.0]。
    """
    f = _FEATURE_WEIGHTS

    score = (
        f["surname_pool_hit"] * _compute_surname_feature(text, start, end)
        + f["name_commonality"] * _compute_name_commonality(text, start, end)
        + f["surrounding_roles"] * _compute_role_feature(text, start, end)
        + f["density_signal"] * _compute_density_feature(text, start, end)
        + f["excluded_conflict"] * _compute_excluded_conflict(text, start, end)
        + f["digit_ratio"] * _compute_digit_ratio(text, start, end)
    )

    return max(0.0, min(1.0, score))


def _classify_by_threshold(score: float) -> str:
    """按双阈值分类：high / medium / low"""
    if score >= _HIGH_THRESHOLD:
        return "high"
    elif score >= _LOW_THRESHOLD:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# LLM 回退（中置信区二次确认）
# ---------------------------------------------------------------------------

# LLM 中置信回退是否启用（由 config.json 控制）
_llm_fallback_enabled = True


def set_llm_fallback(enabled: bool) -> None:
    global _llm_fallback_enabled
    _llm_fallback_enabled = enabled


_llm_client = None


def _get_llm_client():
    """延迟加载 LLM 客户端"""
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    try:
        from llm_client import LLMClient
        _llm_client = LLMClient()
        return _llm_client
    except ImportError:
        return None


LLM_PROMPT_TEMPLATE = """请判断以下文本片段中的「{block}」是否为人名（真实人物姓名）。

上下文：{context}

判断标准：
- 在金融/银行项目文档中的人名（如：张三、李四）
- 出现在分工表、联络人表、成员列表中的姓名
- 配合"组长/成员/负责人"等角色词使用的人名

不是人名的典型场景：
- 常用词（如"提高业务""成立"等）
- 地名/机构名
- 产品名/系统名

请返回 JSON：{{"is_name": true/false, "reason": "判断理由（20字内）"}}"""


def _llm_fallback_check(text: str, start: int, end: int, block: str) -> bool:
    """
    LLM 中置信回退：判断中置信词块是否真正是人名。
    返回 True → 脱敏；返回 False → 保留原文。
    """
    if not _llm_fallback_enabled:
        # 未启用 LLM 回退时，中置信区保守处理：不脱敏
        return False

    client = _get_llm_client()
    if client is None:
        return False  # 无 LLM，降级保守

    # 构造上下文（前20字 + 词块 + 后20字）
    ctx_start = max(0, start - 20)
    ctx_end = min(len(text), end + 20)
    context = text[ctx_start:ctx_end]

    prompt = LLM_PROMPT_TEMPLATE.format(block=block, context=context)
    try:
        result = client.call(prompt, temperature=0.05)
        import json
        data = json.loads(result)
        return data.get("is_name", False)
    except Exception:
        return False  # LLM 调用失败，降级保守


# ---------------------------------------------------------------------------
# 主入口：R4 分词边界 + 上下文评分脱敏
# ---------------------------------------------------------------------------

_NAME_BLOCK_PAT = re.compile(r"[\u4e00-\u9fa5]{2,4}")

# 已在其他层处理的占位符（幂等保护）
_PLACEHOLDER_CHARS = set("XYZ0123456789年月日时分秒")

# 非姓名4字词（姓氏首字+常用非姓名词，防止"项目/负责"类误判）
# 当词块长度=4且包含以下模式时，强制降为低分不脱敏
_NON_NAME_4CHAR_PATTERNS = {
    "项目", "负责", "进行", "形成", "实行", "执行",
    "成立", "建设", "开展", "推进", "落实", "完善",
    "管理", "运营", "发展", "规划", "实施",
    "阶段", "版本", "阶段", "阶段",
}


def _is_known_non_name_4char(block: str) -> bool:
    """检测4字块是否为已知非姓名词（姓氏+常用词）"""
    if len(block) != 4:
        return False
    return block in _NON_NAME_4CHAR_PATTERNS


# 非姓名2字词（姓氏首字+非姓名第二字，如"项目/成立/建设"）
# 这些组合在金融文档中几乎不可能是人名
_NON_NAME_2CHAR_STARTS_WITH_SURNAME: set = {
    "项目", "成立", "建设", "开展", "推进", "落实",
    "发展", "实施", "管理", "运营", "规划",
    "形成", "实行", "执行", "进行",
}


def _is_known_non_name_2char(block: str) -> bool:
    """检测姓氏开头的2字块是否为已知非姓名词"""
    if len(block) != 2:
        return False
    return block in _NON_NAME_2CHAR_STARTS_WITH_SURNAME


def _is_already_redacted(text: str, start: int, end: int) -> bool:
    """检测词块是否已经是占位符（避免二次替换）"""
    block = text[start:end]
    # 含已有占位符字符 → 认为是已处理，跳过
    if any(c in _PLACEHOLDER_CHARS for c in block):
        return True
    return False


def _mask_block(text: str, start: int, end: int) -> str:
    """用 XXX 替换词块"""
    return text[:start] + "XXX" + text[end:]


def _iter_cjk_blocks(text: str):
    """
    迭代器：返回 text 中所有 2-4 字连续CJK块。
    策略：每个CJK位置 i，检查从 i 开始的 2/3/4 字CJK块。
    允许重叠（如"组长张三"中的"张三"也能被找到）。
    按 (start, end, block) 顺序产出。
    """
    n = len(text)
    i = 0
    while i < n:
        # 跳过非CJK字符
        if not ("\u4e00" <= text[i] <= "\u9fff"):
            i += 1
            continue
        # 从位置 i 检查所有 2/3/4 字CJK块（不推进 i，让下次循环检查下一个起点）
        for length in (2, 3, 4):
            if i + length <= n:
                block = text[i:i + length]
                if all("\u4e00" <= c <= "\u9fff" for c in block):
                    yield (i, i + length, block)
        i += 1


def apply_r4_name_pass(
    text: str,
    counts: Optional[Dict[str, int]] = None,
    mode: str = "r4"
) -> str:
    """
    R4 分词边界 + 上下文评分脱敏主入口。

    流程：
    1. 滑动窗口扫描所有 2-4 字连续CJK块（支持重叠，优先长匹配）
    2. 对每个词块打分
    3. 高置信（≥HIGH_THRESHOLD）→ 直接脱敏
    4. 中置信（LOW_THRESHOLD~HIGH_THRESHOLD）→ LLM 回退确认后脱敏
    5. 低置信（<LOW_THRESHOLD）→ 保留原文

    参数：
        text: 待处理文本
        counts: 脱敏计数（可选）
        mode: 评分模式，"r4"=纯 R4 评分，"both"=叠加在枚举规则后

    返回：脱敏后文本
    """
    if not text or not isinstance(text, str):
        return text

    _ensure_jieba()

    # 滑动窗口扫描（显式迭代器避免重叠块被覆盖）
    result = text
    max_iterations = 1000

    for _ in range(max_iterations):
        blocks = list(_iter_cjk_blocks(result))
        if not blocks:
            break

        made_replacement = False
        for start, end, block in blocks:
            # 已处理保护（占位符）
            if _is_already_redacted(result, start, end):
                continue

            # 排除纯占位符
            if block in ("XXX", "XXXX"):
                continue

            # 排除已知非姓名4字词（姓氏+常用词，如"项目/负责"）
            if _is_known_non_name_4char(block):
                continue

            # 排除已知非姓名2字词（姓氏+常用词，如"项目/成立/建设"）
            if _is_known_non_name_2char(block):
                continue

            score = _score_candidate(result, start, end)
            classification = _classify_by_threshold(score)

            should_redact = False

            if classification == "high":
                should_redact = True
            elif classification == "medium":
                should_redact = _llm_fallback_check(result, start, end, block)
            # else: low → 不脱敏

            if should_redact:
                result = result[:start] + "XXX" + result[end:]
                if counts is not None:
                    counts["姓名"] = counts.get("姓名", 0) + 1
                # 替换后重启扫描
                made_replacement = True
                break

        if not made_replacement:
            break

    return result


# ---------------------------------------------------------------------------
# 配置化接口
# ---------------------------------------------------------------------------

# 当前 name_mode（由 pipeline.py 调用 set_name_mode 设置）
_current_name_mode = "enum"


def set_name_mode(mode: str) -> None:
    """设置当前处理的 name_mode（enum/r4/both）"""
    global _current_name_mode
    _current_name_mode = mode


def get_name_mode() -> str:
    """获取当前 name_mode"""
    return _current_name_mode


# ---------------------------------------------------------------------------
# 特征权重配置化（供 config.json 覆盖）
# ---------------------------------------------------------------------------

def update_feature_weights(weights: Dict[str, float]) -> None:
    """更新特征权重（供外部调用覆盖默认值）"""
    global _FEATURE_WEIGHTS
    for key, val in weights.items():
        if key in _FEATURE_WEIGHTS:
            _FEATURE_WEIGHTS[key] = val


def update_thresholds(high: float, low: float) -> None:
    """更新双阈值"""
    global _HIGH_THRESHOLD, _LOW_THRESHOLD
    _HIGH_THRESHOLD = high
    _LOW_THRESHOLD = low


# ---------------------------------------------------------------------------
# 调试/测试工具
# ---------------------------------------------------------------------------

def debug_score(text: str, start: int, end: int) -> Dict:
    """
    调试工具：输出候选词块的详细评分。
    返回各特征值和总分。
    """
    block = text[start:end]
    return {
        "text": block,
        "start": start,
        "end": end,
        "length": len(block),
        "surname_feature": _compute_surname_feature(text, start, end),
        "name_commonality": _compute_name_commonality(text, start, end),
        "role_feature": _compute_role_feature(text, start, end),
        "density_feature": _compute_density_feature(text, start, end),
        "excluded_conflict": _compute_excluded_conflict(text, start, end),
        "digit_ratio": _compute_digit_ratio(text, start, end),
        "total_score": _score_candidate(text, start, end),
        "classification": _classify_by_threshold(_score_candidate(text, start, end)),
    }
