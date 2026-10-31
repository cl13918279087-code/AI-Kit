#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# name_region_detector.py - N-C 层：姓名集中区检测（Issue #50）
#
# 战略转向（Issue #50 核心主张）：
#   旧：全局字符池粗筛 —— 凡是「姓 + 名字用字」都当人名
#   新：区域优先 —— 先定位「人名集中区」，再在区域内逐项判定
#
# 人做脱敏时从不逐个核对名单，实际顺序是：
#   1. 先扫版面，找出哪些区域是「人名集中区」
#   2. 再在这些区域内判断哪些条目是人名
#   3. 区域之外，默认不动
#
# 本模块是**格式无关的语义判定层**：
#   - 输入：一段纯文本 / 一组表格单元格
#   - 输出：「姓名集中区」的坐标集合
# 各格式适配器（Word/Excel/PPT/PDF）调用之。
#
# **模块划分说明（Issue #50 §4.1）**：
#   - table_role_detector.py 是 OOXML 格式适配层（解析 XML、按坐标回写）
#   - name_region_detector.py 是语义判定层（判定「哪些区段是姓名集中区」）
#   T1 表格列判定的**共用函数**放在本模块，供两者共同复用，避免实现漂移。
#
# 三类识别器（Issue #50 §4.2）：
#   T1 表格姓名列   —— 表头角色词 + 值列形态一致性    置信：高
#   T2 正文名单段   —— 锚点词引导 + 候选同构性        置信：高
#   T3 签名/落款区  —— 位置特征 + 角色词              置信：中
#
# 区域判定的三条准则（Issue #50 §4.3）必须**同时**满足：
#   1. 结构性：区域由分隔符连接的同构项构成（长度一致、形态一致）
#   2. 上下文性：区域前方有角色/名单锚点词，或区域内至少一项命中 D16
#   3. 形态性：每项满足「姓 + 1~3 个名用字」，或命中 D16
# ---------------------------------------------------------------------------

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# 配置读取（锚点词表统一从 config.json 的 name_anchor_words 取，
# 见 Issue #50 §4.7「锚点词表收敛」——不再三处各维护一份）
# ---------------------------------------------------------------------------

_ANCHOR_WORDS: Optional[Dict[str, List[str]]] = None


def _load_config() -> dict:
    """读取 config.json（延迟导入避免循环依赖）。"""
    import os
    from common_rules import _load_config as _cr_load_config
    return _cr_load_config()


def get_anchor_words() -> Dict[str, List[str]]:
    """返回锚点词表：{'role': [...], 'list': [...], 'sign': [...]}。

    配置缺失时回退到内置最小集，保证模块可独立使用与测试。
    """
    global _ANCHOR_WORDS
    if _ANCHOR_WORDS is not None:
        return _ANCHOR_WORDS
    try:
        cfg = _load_config()
        raw = cfg.get("name_anchor_words") or {}
    except Exception:
        raw = {}
    result = {
        "role": list(raw.get("role") or []),
        "list": list(raw.get("list") or []),
        "sign": list(raw.get("sign") or []),
    }
    if not any(result.values()):
        result = {
            "role": ["组长", "项目经理", "负责人", "联系人", "成员", "组员", "名单", "姓名"],
            "list": ["成员", "人员", "名单", "如下", "包括"],
            "sign": ["签字", "签名", "盖章"],
        }
    _ANCHOR_WORDS = result
    return result


def reset_anchor_cache() -> None:
    """清空锚点词表缓存（配置变更或测试隔离时调用）。"""
    global _ANCHOR_WORDS
    _ANCHOR_WORDS = None


# ---------------------------------------------------------------------------
# 基础形态工具
# ---------------------------------------------------------------------------

# 列表分隔符：顿号、中文逗号、英文逗号、分号、斜杠
_LIST_SEPARATORS = "、，,;；/"

# 姓名块：2-4 个连续 CJK 字符
_NAME_ITEM_RE = re.compile(r"^[一-龥]{2,4}$")

# 区域内的姓名候选（不含分隔符的连续 CJK 块）
_CANDIDATE_RE = re.compile(r"[一-龥]{2,4}")

# 单元格内切分「姓名 token」用的分隔符集
#
# 与 `_LIST_SEPARATORS` 的差异：**额外包含冒号**（全角/半角）与空白。
# 原因：「负责部门/人」类表格的单元格形态是
#   「科技运营：赤帅超」 / 「品宣部门公关组/方宁」
# 即「组织前缀 + 分隔符 + 姓名」。若不按冒号切分，整格既不是姓名形态，
# 也提不出姓名 token → 表头虽已声明人角色，姓名仍会明文残留
# （实测 Issue #49 残留泄漏「方宁」）。
_CELL_TOKEN_SEPARATORS = "、，,;；/：: \t\r\n　"

# 单元格内的「角色标签」词：出现在人名区单元格中，用来切分粘连文本
#
# 背景（实测 slide7 / slide11 的「组员构成」列）：
#   「组长：毛杰副组长：疏荣、邓君组员：吴郧杰、李虎」
# 姓名与标签之间**没有分隔符**（「邓君组员」是粘连的），
# 若只按分隔符切分，会得到「姓名 + 标签」混合 token，
# 遮盖时会把标签一起吃掉（整格变成一个 XXX，标签信息尽失）。
#
# 因此在分隔符之外，再增加一层「角色标签」边界切分：标签本身单独成 token
# 且**不参与姓名判定**（否则「成员」这类标签可能被误当姓名）。
_NC_ROLE_LABELS = (
    "副组长", "组长", "组员", "负责人", "责任人", "联系人",
    "参与人", "执行人", "成员", "队员",
)
_NC_ROLE_LABEL_RE = re.compile("(" + "|".join(_NC_ROLE_LABELS) + ")")

# N-C 区域层专用的「池外姓」补充集合
#
# 背景：config.surname_pool 共 415 字，相对《百家姓》缺 91 字（含寇/赤/
# 宓/嵇/羿/汲/邴/逄/璩/逯/冼/仉/蒯/邝/竺/殳/隗/钭 等）。缺字导致
# 「寇建杰」「赤帅超」这类**真实人名**不被 `is_name_shaped` 识别，
# 进而在「进度负责人」这类人角色列里被判定为「非姓名列」→ 同列的
# 「夏凌」一并明文残留。
#
# 为什么只在 N-C 层补充、而不改 common_rules 的 surname_pool：
#   - N-C 层是**区域化**判定——只在已确认是人角色/名单的区域内才生效，
#     上下文本身提供了强证据，误伤面天然收敛；
#   - 规则层（common_rules）作用于**全文档无上下文**文本，扩池会直接
#     放大 Issue #49 的误伤指标（#49 的验收口径依赖规则层指标稳定）。
#
# 选字原则：只取「既在《百家姓》中、又极少作为常用词首字」的姓氏，
# 排除 家/印/空/红/秋/相/养/后/从/松/权/慎/广/步/富/山/幸/满/查/燕/
# 通/闻/都/宿/双/公 等高频词首字。
_NC_EXTRA_SURNAMES: Set[str] = set(
    "寇赤宓嵇羿汲邴逄璩逯冼仉蒯邝竺殳隗钭"
)


def _surname_set() -> Set[str]:
    from common_rules import SURNAME_SET
    return SURNAME_SET


def _compound_surnames() -> List[str]:
    """常见复姓表（Issue #49 P1 引入，N-C 复用）。"""
    from common_rules import _NAME_GATE_COMPOUND_SURNAMES
    return list(_NAME_GATE_COMPOUND_SURNAMES)


def _name_pool_chars() -> Set[str]:
    from common_rules import get_name_pool_chars
    return set(get_name_pool_chars())


def _full_name_set() -> Set[str]:
    from common_rules import get_full_name_set
    return get_full_name_set()


# D16 字频缓存（名字位字频 / 姓氏位字频）
_NC_FREQ_CACHE: Dict[str, Any] = {}


def _name_char_freq() -> Dict[str, int]:
    """D16 全名库自举的「名字位字频」（名字[1:] 的字频）。"""
    if "nf" not in _NC_FREQ_CACHE:
        try:
            from common_rules import _get_name_char_freq
            _NC_FREQ_CACHE["nf"] = _get_name_char_freq()
        except Exception:
            _NC_FREQ_CACHE["nf"] = {}
    return _NC_FREQ_CACHE["nf"]


def _surname_char_freq() -> Dict[str, int]:
    """D16 全名库自举的「姓氏位字频」（name[0] 的字频）。"""
    if "sf" not in _NC_FREQ_CACHE:
        try:
            from common_rules import _get_surname_char_freq
            _NC_FREQ_CACHE["sf"] = _get_surname_char_freq()
        except Exception:
            _NC_FREQ_CACHE["sf"] = {}
    return _NC_FREQ_CACHE["sf"]


def is_name_shaped(text: str, allow_extra_surnames: bool = False) -> bool:
    """形态性判定：该项是否满足「姓 + 1~3 个名用字」或命中 D16 精确全名库。

    这是区域判定的第三条准则。**不依赖任何上下文**，因此可以脱离
    规则A 的字频闸门独立判断——这正是「名单外人名能被捞出」的关键。

    支持复姓（「欧阳娜娜」「司马光」等）——复姓表见
    `common_rules._NAME_GATE_COMPOUND_SURNAMES`（Issue #49 P1 引入）。

    `allow_extra_surnames`
        是否允许 `_NC_EXTRA_SURNAMES`（池外姓）作为姓位。**默认关闭**，
        只在 N-C 区域层内部开启（`_cell_shape_ratio` / `redact_name_cell`
        等已确认「这是人角色列/名单区」的上下文里）。规则层
        （common_rules）与 D15 注入闸门（`table_role_detector.
        _is_name_shaped_word`）保持默认关闭，避免放大 Issue #49 的
        全文档误伤指标。
    """
    if not text:
        return False
    if text in _full_name_set():
        return True
    if not (2 <= len(text) <= 4):
        return False
    pool = _name_pool_chars()
    surnames = _surname_set()
    # 复姓优先匹配（名字位 1~2 字）
    for comp in _compound_surnames():
        if text.startswith(comp) and len(text) >= len(comp) + 1:
            return all(c in pool for c in text[len(comp):])
    if text[0] not in surnames:
        if not (allow_extra_surnames and text[0] in _NC_EXTRA_SURNAMES):
            return False
    return all(c in pool for c in text[1:])


# ---------------------------------------------------------------------------
# 单元格 token 切分
# ---------------------------------------------------------------------------

def _split_cell_tokens(text: str) -> List[Tuple[str, int, int]]:
    """把单元格文本切成「token 段」，返回 [(token, start, end)]。

    token = 相邻两个分隔符之间的文本（分隔符本身不含在 token 内，
    但 start/end 记录其在原串中的位置，便于按位置切片替换）。

    分隔符集见 `_CELL_TOKEN_SEPARATORS`（含冒号与空白）。
    """
    out: List[Tuple[str, int, int]] = []
    if not text:
        return out
    last = 0
    i = 0
    n = len(text)
    while i < n:
        if text[i] in _CELL_TOKEN_SEPARATORS:
            if i > last:
                out.append((text[last:i], last, i))
            last = i + 1
        i += 1
    if last < n:
        out.append((text[last:], last, n))
    return out


def _split_cell_name_tokens(text: str) -> List[Tuple[str, int, int]]:
    """把单元格文本切成「姓名候选 token」，返回 [(token, start, end)]。

    与 `_split_cell_tokens` 的差异：**额外按角色标签边界切分**。

    背景（slide7 / slide11「组员构成」列）：
        「组长：毛杰副组长：疏荣、邓君组员：吴郧杰、李虎」
    「邓君组员」这类「姓名 + 标签」粘连体若不切开，遮盖时会把标签
    一起吃掉；切开后每个 token 都是纯姓名或纯标签。
    """
    out: List[Tuple[str, int, int]] = []
    for tok, s, e in _split_cell_tokens(text):
        if not tok:
            continue
        pos = s
        for m in _NC_ROLE_LABEL_RE.finditer(tok):
            if m.start() > 0:
                out.append((tok[:m.start()], pos, pos + m.start()))
            out.append((m.group(0), pos + m.start(), pos + m.end()))
            pos = pos + m.end()
        if pos < e:
            out.append((tok[pos - s:], pos, e))
    return out


def _cell_has_name_evidence(value: str, allow_extra_surnames: bool = False,
                             relaxed: bool = False) -> bool:
    """单元格内是否存在「严格人名证据」（任一 token 满足严格姓名形态）。

    用于 T1 的「值列形态一致性」判定：像「科技运营：赤帅超」这类
    「组织前缀 + 分隔符 + 姓名」的单元格，整格不是姓名形态，但**内含**
    严格人名 token → 应计为姓名列证据。

    `allow_extra_surnames`
        是否放行 `_NC_EXTRA_SURNAMES`（池外姓）。

        **T1 形态一致性口径（默认 False）**：统计证据必须最严格，
        避免「这一列是人名列」的判定被池外姓放大。

        **N-C 补漏口径（True）**：表头已声明「人角色」之后，
        池外姓（寇/赤/宓/嵇…）的真实人名不应整列漏脱。
        实测（平安信用卡A+项目 PPT slide33/34）：「进度负责人」列
        值为「寇建杰」「夏凌」，寇为池外姓 → 严格形态下一致率 50%
        （<60% 门槛）→ 整列不被选中 → 同列「夏凌」明文残留。
        放开池外姓后一致率 100%，整列进入补漏通道。
    """
    stripped = value.strip()
    if not stripped:
        return False
    if _is_masked_name(stripped):
        return True
    if _nc_name_len(stripped, allow_extra_surnames=allow_extra_surnames,
                    relaxed=relaxed):
        return True
    for tok, _s, _e in _split_cell_name_tokens(stripped):
        t = tok.strip()
        if not t or t in _NC_ROLE_LABELS:
            continue
        # 「毛杰（38）」「夏凌（2）」这类「姓名 + 括注计数」：
        # 整 token 不是姓名形态，但**开头**是姓名 → 计入证据
        if _nc_name_len(t, allow_extra_surnames=allow_extra_surnames,
                        relaxed=relaxed):
            return True
    return False


# ---------------------------------------------------------------------------
# 共享「人角色表头」判定（T1 表头语义 + PPT 适配器共用）
# ---------------------------------------------------------------------------

# 「组员构成」「人员名单」这类**构造式**表头：角色词在前，后接固定后缀。
_NC_PERSON_HEADER_TAILS = ("构成", "名单", "列表", "情况", "信息", "构成情况")

# 「负责部门/人」这类复合表头里的「人」标记
_NC_PERSON_HEADER_MARKERS = ("人", "人员", "责任人", "姓名")

# 英文角色表头（Issue #52：实测 slide33/34 的「BOSS」列）
#
# 「BOSS」列的取值是「杨龙」「陈鲲」等人名，但表头是纯英文，
# 既不命中中文锚点词、也不满足「以角色词结尾」→ 整列不进 N-C →
# 同列的「陈鲲」明文残留。取**精确匹配**（不做子串匹配）以免误伤。
_NC_PERSON_HEADER_EN = frozenset({"BOSS", "OWNER", "PM"})

# 尾部括注（表头里的「（对接人）」「（3）」等）
_NC_HEADER_PAREN_RE = re.compile(r"[（(]([^（）()]*)[)）]\s*$")


def _split_header_parts(header: str) -> List[str]:
    """把复合表头按「/」「／」拆成分量。

    「负责部门/人」「品宣部门公关组/方宁」这类表头里，只有**某个分量**
    声明了「人」角色，整列才是人名列。
    """
    parts = re.split(r"[/／]", header or "")
    return [p.strip() for p in parts if p.strip()]


def is_person_header(header: str) -> bool:
    """表头是否声明了「人名角色」。

    三类口径（任一成立即为真）：

      1. **复合表头**：按 `/`、`／` 拆分后，任一分量是「人 / 人员 /
         责任人 / 姓名」标记，或以角色词结尾（负责部门/人）。
      2. **整体以角色词结尾**：对接负责人 / 进度负责人 / 开发负责人。
      3. **构造式表头**：角色词 + 固定后缀：组员构成 / 人员名单。

    为什么需要 1：「负责部门/人」既不以角色词结尾（结尾是「人」），
    也不整体等于锚点词，旧口径 `h in anchor_set or h.endswith(a)`
    完全不命中 → 该列不进入 N-C → 同列「方宁」明文残留（实测）。
    """
    text = (header or "").strip()
    if not text:
        return False
    # 英文角色表头（BOSS / Owner / PM）
    if text.upper() in _NC_PERSON_HEADER_EN:
        return True
    anchors = get_anchor_words()
    role_set: Set[str] = set(anchors.get("role", ())) | set(anchors.get("list", ()))

    # 1) 复合表头
    parts = _split_header_parts(text)
    if len(parts) > 1:
        for p in parts:
            if p in _NC_PERSON_HEADER_MARKERS:
                return True
            if p in role_set:
                return True
            if any(len(a) >= 2 and p.endswith(a) for a in role_set):
                return True

    # 2) 整体以角色词结尾 / 等于角色词
    if text in role_set:
        return True
    if any(len(a) >= 2 and text.endswith(a) for a in role_set):
        return True

    # 3) 构造式表头（组员构成 / 人员名单）
    for a in role_set:
        if len(a) >= 2 and text.startswith(a) and text[len(a):] in _NC_PERSON_HEADER_TAILS:
            return True

    # 4) 「X人」型表头（案例执行人 / 填报人 / 使用人）
    #
    # 为什么要单列一条：锚点词表里有「开发人」「测试人」「参与人」等，
    # 但**没有**「执行人」，因此「案例执行人」旧口径完全不命中 →
    # 该列 24 行人名（含「蔡海姗」）整列不进 N-C → 明文残留。
    # 中文表头以「人」结尾时几乎总是「人角色」，故按后缀判定。
    if len(text) >= 2 and text.endswith("人"):
        return True

    # 5) 括注型表头（数据提供方（对接人）/ 捞数需求方（对接人））
    #
    # 实测（真实 PPT slide40/47/48）：表头写成「XX（对接人）」时，
    # 括注里才是人角色，但整串以「）」结尾 → 上面 4 条口径全部不命中 →
    # 整列（李虎 / 戚亚亚 / 谈叙 / 邓君 …）不进 N-C → 明文残留。
    # 这里取**尾部括注**内容单独判定；括注非人角色（如「（3）」「（含）」）
    # 则不命中。
    m = _NC_HEADER_PAREN_RE.search(text)
    if m:
        inner = m.group(1).strip()
        if inner and inner not in role_set:
            if inner.endswith("人") or (len(inner) >= 2 and
                                        any(len(a) >= 2 and inner.endswith(a)
                                            for a in role_set)):
                return True
    return False


def _leading_name_span(token: str, allow_extra_surnames: bool = False) -> int:
    """返回 token **开头**连续严格姓名形态的最长前缀长度（0 = 无）。

    只认「开头」是刻意的安全约束：CJK 无词边界，若在 token 中任意
    位置扫描 2-4 字姓名形态，会把「品宣部门公关组」里的「宣部」
    （宣∈姓池、部∈名池）误判为姓名。限定在 token 开头可把这类
    组织名误判面压到最低——真实姓名通常紧跟分隔符出现。

    `allow_extra_surnames`
        是否放行池外姓（寇/赤/宓…）参与形态判定。N-C 区域层在
        「表头已声明人角色」的前提下开启。
    """
    # 先按「整 token 就是姓名」判定（长度 2~4）——保证「欧阳娜娜」取满 4 字，
    # 而不是被「欧阳娜」抢先匹配（旧的 4→3→2 顺序会截断复姓四字名）。
    if len(token) in (2, 3, 4) and is_name_shaped(token, allow_extra_surnames=allow_extra_surnames):
        return len(token)
    for L in (4, 3, 2):
        if L < len(token) and is_name_shaped(token[:L], allow_extra_surnames=allow_extra_surnames):
            return L
    return 0


def _relaxed_name_span(token: str, allow_extra_surnames: bool = False) -> int:
    """**宽松**姓名形态：姓 + 1~2 个任意 CJK 字（名字位不要求命中名字池）。

    存在的唯一理由是「名字位字不在名字池」的真实人名：
        「陈鲲」（鲲不在 name_pool）「吴县波」「陈沈麟」「洪云霓」「叶助茂」
    这类名字在规则层（字频闸门 + 名字池）**必然漏脱**，而在
    「表头已声明人角色 + 列形态一致」的上下文里，字形证据可以放宽。

    安全约束：
      - 只认 token **开头**的姓位（与 `_leading_name_span` 同口径）；
      - 姓位必须是姓氏字（池外姓仅在 N-C 区域层放行）；
      - 长度上限 3 字（避免把整句当姓名）；
      - 命中业务词保护表的一律不算；
      - **不做「前缀猜测」**：命中段之后若还接着中文（业务文本），
        则一律不算 —— 否则「渠道拓展」会被误判成姓名「渠道」，
        产出「XXX拓展」这种业务词半遮盖（业务词零误伤的硬要求）。

    该通道**只在列级上下文确认「这是人名列」时**才启用（见
    `redact_name_cell(..., relaxed=True)`），因此误伤面被列上下文收敛。
    """
    if not token or len(token) < 2:
        return 0
    surnames = _surname_set()
    for L in (3, 2):
        if L > len(token):
            continue
        head = token[:L]
        if not all("一" <= c <= "龥" for c in head):
            continue
        if head[0] not in surnames and not (
                allow_extra_surnames and head[0] in _NC_EXTRA_SURNAMES):
            continue
        if _is_biz_word(head):
            continue
        rest = token[L:]
        if rest and "一" <= rest[0] <= "龥":
            # 命中段之后仍是中文 → 这是「渠道拓展」型业务词，不是姓名
            continue
        return L
    return 0


# ---------------------------------------------------------------------------
# N-C 区域层的姓名判定分级（Issue #52 接线）
#
# 分级的依据是「**上下文已经确认这是人名列**」——此时字频闸门的
# 「连坐」代价可以放宽（与规则层 S3 强上下文通道同思路），但仍需
# 姓氏位闸门兜住「冷僻姓 + 常用词」这类结构性误伤。
#
#   A 级：姓氏闸门（D16 姓氏位字频 ≥5）+ 名字位字频 ≥1
#          —— 覆盖规则层漏掉的「陈冠男 / 陆检彪 / 陈观云 / 蔡海姗 /
#             夏凌 / 方宁」等（名字位字频不够「热」导致规则层放弃）。
#   B 级：姓 + 任意 CJK 名字位（**仅在 relaxed 列启用**）
#          —— 覆盖「陈鲲 / 吴县波 / 洪云霓 / 叶助茂」等名字位字
#             完全不在 D16 库中的真名。
#   C 级：高危姓 + 单字名（弱证据，仅独立 token）
#          —— 兜住「樊霖」这类「姓氏高危 + 名字位极冷」的真名。
#
# 误伤防护：单字名要求姓氏位字频 ≥20（「夏凌 / 方宁」保留，
# 而「单元 / 白天 / 华夏 / 万通」这类姓氏位字频 5~12 的常用词被挡住）。
# ---------------------------------------------------------------------------

_NC_SURNAME_FREQ_MIN = 5       # 姓氏位闸门（D16 自举，与规则层同口径）
_NC_SURNAME_FREQ_STRONG = 20   # 单字名所要求的姓氏位字频下限
_NC_NAME_CHAR_FREQ_MIN = 1     # 名字位字频下限（出现在 D16 名字位即可）


def _nc_tier_a(token: str, allow_extra_surnames: bool = False) -> bool:
    """A 级：姓氏闸门 + 名字位字频。"""
    if not token or not (2 <= len(token) <= 4):
        return False
    if not all("一" <= c <= "龥" for c in token):
        return False
    nf = _name_char_freq()
    # 复姓优先（复姓本身是强信号）
    for comp in _compound_surnames():
        if token.startswith(comp) and len(token) >= len(comp) + 1:
            return all(nf.get(c, 0) >= _NC_NAME_CHAR_FREQ_MIN
                       for c in token[len(comp):])
    ch = token[0]
    rest = token[1:]
    if ch in _NC_EXTRA_SURNAMES:
        # 池外姓（寇/赤/宓…）只在 N-C 区域层放行
        if not allow_extra_surnames:
            return False
        return all(nf.get(c, 0) >= _NC_NAME_CHAR_FREQ_MIN for c in rest)
    if ch not in _surname_set():
        return False
    sf = _surname_char_freq().get(ch, 0)
    if sf < _NC_SURNAME_FREQ_MIN:
        return False
    if not all(nf.get(c, 0) >= _NC_NAME_CHAR_FREQ_MIN for c in rest):
        return False
    # 单字名要求「高频姓」——挡住「单元 / 白天 / 华夏 / 万通」
    if len(rest) == 1 and sf < _NC_SURNAME_FREQ_STRONG:
        return False
    return True


def _nc_tier_b(token: str, allow_extra_surnames: bool = False) -> bool:
    """B 级：姓 + 任意 CJK 名字位（名字位字频不限）。

    **仅在 relaxed 列启用**（列内已有强证据但形态一致率不足 60%）。
    """
    if not token or not (2 <= len(token) <= 4):
        return False
    if not all("一" <= c <= "龥" for c in token):
        return False
    ch = token[0]
    if ch in _NC_EXTRA_SURNAMES:
        return allow_extra_surnames
    if ch not in _surname_set():
        return False
    return _surname_char_freq().get(ch, 0) >= 1


def _nc_tier_c(token: str) -> bool:
    """C 级：高危姓 + 单字名（弱证据）。"""
    if len(token) != 2:
        return False
    return token[0] in _high_risk_surname_set()


# ---------------------------------------------------------------------------
# D 级（Issue #52）：稀有字真名的补漏通道
# ---------------------------------------------------------------------------
#
# 问题（实测于真实 PPT「平安信用卡A+项目-投产整体方案v0.4」）：
#   A 级要求「名字位每个字都在 D16 名字位字频库中」，而真实人名里
#   存在大量**不在该库中的字**：
#     陈鲲（鲲 nf=0）、吴县波（县 nf=0）、陈沈麟（沈 nf=0）、
#     叶助茂（助 nf=0）、张瓞引（瓞/引 nf=0）、叶鎏（鎏 nf=0）……
#   这些列**已被正确判定为姓名列**（strict），但逐格遮盖时因 A 级
#   字频闸门而漏脱 → 真实人名明文残留（STD 版已遮、当前版未遮）。
#
# D 级的定位：**只在已确认的姓名列内部**做补漏，且需要「上下文证据」：
#   - D1 同格兄弟证据：同一单元格内已有 A/C 级确认的姓名；
#   - D2 同列兄弟证据：该列的 A/C 确认率 ≥ 50%（说明整列确实是人名列）。
#
# 形态约束（与 A/B/C 级共享）：
#   1. 2~3 字纯 CJK；
#   2. 首字为姓氏字（config.surname_pool + N-C 池外姓 + 「印」）；
#   3. 不命中角色标签、不命中业务词保护表；
#   4. **文档级低频**：该词在全文中的 CJK 2~3 gram 频次 < 8
#      ——业务词高频、姓名低频，用这一条把「客户体验」这类
#      「首字恰是姓氏」的常用词挡在外面。
#
# 判定结果：D 级**只在 A/C 级未命中**时补位，不会覆盖强证据。

# D 级专用的池外姓（比 A/B 级的池外姓集合多一个「印」）。
# 「印」在《百家姓》中确有姓氏，且「印辰婕」是实测到的真实人名；
# 但「印」也是高频常用词首字（印刷/印章），故只放宽到 D 级，
# 不进入 A/B 级的池外姓集合，避免放大既有判定面。
_NC_D_SURNAMES: Set[str] = set(_NC_EXTRA_SURNAMES) | {"印"}

# 文档级 gram 频次闸门上限（业务词 ≥ 该值即视为高频词，挡住）
_NC_DOC_FREQ_MAX = 8

# 文档级词频上下文（由 PPT/文档级适配层在处理前注入）
_NC_DOC_GRAM: Optional[Dict[str, int]] = None


def set_nc_doc_gram(gram: Optional[Dict[str, int]]) -> None:
    """注入文档级 CJK 2~3 gram 频次表（D 级闸门用）。

    未注入（None）时 **D 级整体不启用**（保守：没有文档级证据就
    不做低频判定，避免误伤业务词）。
    """
    global _NC_DOC_GRAM
    _NC_DOC_GRAM = gram


def clear_nc_doc_gram() -> None:
    """清空文档级词频上下文（避免跨文档污染）。"""
    global _NC_DOC_GRAM
    _NC_DOC_GRAM = None


def build_nc_doc_gram(texts: Iterable[str]) -> Dict[str, int]:
    """统计一批文本的 CJK 2~3 gram 频次。

    按 `[一-龥]+` 连续段切分后滑窗计数——与「业务词在全文中反复出现、
    姓名基本只出现一次」的经验分布一致。
    """
    gram: Dict[str, int] = {}
    for text in texts:
        if not text:
            continue
        for run in re.findall(r"[一-龥]+", text):
            n = len(run)
            for L in (2, 3):
                if n < L:
                    continue
                for i in range(n - L + 1):
                    g = run[i:i + L]
                    gram[g] = gram.get(g, 0) + 1
    return gram


def _nc_doc_freq(token: str) -> Optional[int]:
    """查询 token 在文档中的 gram 频次（None = 无文档级上下文）。"""
    if _NC_DOC_GRAM is None:
        return None
    return _NC_DOC_GRAM.get(token, 0)


def _nc_tier_d(token: str) -> bool:
    """D 级：姓首 + 低频 + 非业务词（不含任何字频要求）。"""
    t = (token or "").strip()
    if not (2 <= len(t) <= 3):
        return False
    if not all("一" <= c <= "龥" for c in t):
        return False
    if t in _NC_ROLE_LABELS or _is_biz_word(t):
        return False
    ch = t[0]
    if ch not in _surname_set() and ch not in _NC_D_SURNAMES:
        return False
    freq = _nc_doc_freq(t)
    if freq is None:
        # 没有文档级上下文 → 不启用 D 级
        return False
    return freq < _NC_DOC_FREQ_MAX


def _nc_name_len(token: str, allow_extra_surnames: bool = False,
                 relaxed: bool = False, weak: bool = False) -> int:
    """返回 token **开头**应视为姓名的长度（0 = 不是姓名）。

    候选长度按「整 token → 前缀」顺序尝试，保证
    「毛杰（38）」「夏凌（2）」「孙为民（追猎者）」这类
    「姓名 + 括注」结构只遮姓名部分、括注原样保留。

    两条安全约束（业务词零误伤）：
      1. **A 级前缀**若命中业务词保护表，则整个 token 不作姓名 ——
         「白名单用户」的最长前缀「白名单」是业务词，若继续回退到
         更短前缀就会误遮「白名」；
      2. **B 级（宽松）不做中文前缀猜测**：命中段之后若还接中文，
         则判为业务文本（业务词零误伤）；B 级只在「整体就是 2~3 字
         姓名」或「姓名 + 非中文括注」时启用。
         （否则「渠道拓展」会被误判成姓名「渠道」→ 产出「XXX拓展」）
    """
    t = (token or "").strip()
    if not t:
        return 0
    if _is_masked_name(t):
        return 0
    if _is_biz_word(t):
        return 0
    cands: List[int] = []
    if len(t) in (2, 3, 4):
        cands.append(len(t))
    for L in (3, 2):
        if L < len(t):
            cands.append(L)
    # A 级：最长前缀若是业务词 → 整个 token 不作姓名（不回退短前缀）
    longest = max(cands) if cands else 0
    if longest and longest < len(t) and _is_biz_word(t[:longest]):
        return 0
    for L in cands:
        if _nc_tier_a(t[:L], allow_extra_surnames=allow_extra_surnames):
            return L
    if relaxed:
        for L in cands:
            if not (2 <= L <= 3):
                continue
            if L < len(t):
                rest = t[L:]
                if rest and "一" <= rest[0] <= "龥":
                    continue
            if _nc_tier_b(t[:L], allow_extra_surnames=allow_extra_surnames):
                return L
    if weak and len(t) == 2 and _nc_tier_c(t):
        return 2
    return 0


def same_shape(a: str, b: str) -> bool:
    """同构性判定：两项是否形态一致（长度一致 + 姓氏/名用字构成一致）。"""
    if len(a) != len(b):
        return False
    if a in _full_name_set() and b in _full_name_set():
        return True
    if len(a) < 2:
        return False
    pool = _name_pool_chars()
    return _is_name_like(a) and _is_name_like(b)


def _is_name_like(text: str) -> bool:
    """宽松判定：首字为姓氏（或复姓起头）。"""
    if not (2 <= len(text) <= 4):
        return False
    if not all("一" <= c <= "龥" for c in text):
        return False
    for comp in _compound_surnames():
        if text.startswith(comp):
            return True
    return text[0] in _surname_set()



# ---------------------------------------------------------------------------
# 区域数据结构
# ---------------------------------------------------------------------------

class NameRegion:
    """一个「姓名集中区」。

    坐标以 (start, end) 字符区间表示（相对传入文本）；
    items 为区域内各项的 (start, end, text) 列表。
    """

    __slots__ = ("start", "end", "items", "kind", "confidence")

    def __init__(self, start: int, end: int, items: List[Tuple[int, int, str]],
                 kind: str = "T2", confidence: str = "high"):
        self.start = start
        self.end = end
        self.items = items
        self.kind = kind
        self.confidence = confidence

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"NameRegion({self.kind}, {self.start}:{self.end}, "
                f"{[t for _, _, t in self.items]})")

    @property
    def texts(self) -> List[str]:
        return [t for _, _, t in self.items]


# ---------------------------------------------------------------------------
# T2：正文名单段区域检测
# ---------------------------------------------------------------------------

def _split_by_separators(text: str) -> List[Tuple[str, int, int, str]]:
    """按列表分隔符切分，返回 [(片段, start, end, 分隔符)]。

    分隔符本身不计入片段的 start/end。
    """
    parts: List[Tuple[str, int, int, str]] = []
    last = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _LIST_SEPARATORS:
            if i > last:
                parts.append((text[last:i], last, i, ""))
            # 记录分隔符（连续分隔符只记第一个）
            if not parts or parts[-1][3]:
                parts.append((text[last:i], last, i, ch))
            else:
                parts[-1] = (parts[-1][0], parts[-1][1], parts[-1][2], ch)
            last = i + 1
        i += 1
    if last < n:
        parts.append((text[last:], last, n, ""))
    return parts


def _anchor_words_set() -> Set[str]:
    """全部锚点词（角色类 + 名单类）的并集。"""
    a = get_anchor_words()
    return set(a.get("role", ())) | set(a.get("list", ()))


def _is_anchor_phrase(text: str) -> bool:
    """判定一个词块本身是不是锚点短语（而非人名）。

    「项目组」「成员如下」「参会人员」这类词块虽然可能符合姓名形态，
    但它们是**名单的信号词**，不是名单内容，必须排除。
    """
    if text in _full_name_set():
        return False
    anchors = _anchor_words_set()
    if text in anchors:
        return True
    # 词块内含锚点词且长度 ≤4 时视为锚点短语
    # （"成员如下""参会人员""项目组成员"）
    return len(text) <= 4 and any(a in text for a in anchors)


def _extract_candidates(seg: str, seg_start: int) -> List[Tuple[int, int, str]]:
    """从一个片段中提取姓名形态候选，返回 [(start, end, text)]。

    提取规则：
      - 候选块的**左边界**必须是片段起点或非中文字符（即不被其他中文粘连），
        避免从中文词中间切出伪姓名；
      - 取该起点下**最长**的姓名形态前缀（4→3→2 逐级尝试）；
      - 锚点短语（如"项目组""参会人员"）被排除。
    """
    out: List[Tuple[int, int, str]] = []
    n = len(seg)
    i = 0
    while i < n:
        ch = seg[i]
        if not ("一" <= ch <= "龥"):
            i += 1
            continue
        # 左边界检查：片段起点或前一字符非中文
        if i > 0 and ("一" <= seg[i - 1] <= "龥"):
            i += 1
            continue
        # 最长优先尝试
        matched = None
        for ln in (4, 3, 2):
            if i + ln > n:
                continue
            frag = seg[i:i + ln]
            if not all("一" <= c <= "龥" for c in frag):
                continue
            if is_name_shaped(frag) and not _is_anchor_phrase(frag):
                matched = frag
                break
        if matched:
            out.append((seg_start + i, seg_start + i + len(matched), matched))
            i += len(matched)
            continue
        i += 1
    return out


def has_anchor_before(text: str, pos: int, window: int = 20) -> Optional[str]:
    """上下文性判定：区域起点前方 window 字内是否有锚点词。

    返回命中的锚点词，未命中返回 None。
    """
    if pos <= 0:
        return None
    pre = text[max(0, pos - window):pos]
    anchors = get_anchor_words()
    # 名单类要求配合同构性（由调用方保证），此处只做命中判定
    for kind in ("role", "list"):
        for w in anchors.get(kind, ()):
            if w in pre:
                return w
    return None


def detect_text_regions(text: str, min_items: int = 2) -> List[NameRegion]:
    """T2：检测文本中的姓名集中区。

    判定流程（Issue #50 §4.3 三条准则）：
      1. 结构性：候选链由分隔符连接，且各项长度一致（同构）
      2. 上下文性：链首前方有锚点词，或链内至少一项命中 D16
      3. 形态性：链内每项都满足姓名形态

    返回姓名集中区列表（按出现顺序）。
    """
    if not text:
        return []

    regions: List[NameRegion] = []
    for line_start, line in _iter_lines(text):
        regions.extend(_detect_line_regions(text, line_start, line, min_items))
    return regions


def _iter_lines(text: str):
    """产出 (行起始偏移, 行内容)。"""
    pos = 0
    for line in text.split("\n"):
        yield pos, line
        pos += len(line) + 1


def _detect_line_regions(text: str, line_start: int, line: str,
                         min_items: int) -> List[NameRegion]:
    """在单行内检测姓名集中区。"""
    # 1) 整行扫描，提取姓名形态候选
    candidates = _extract_candidates(line, 0)
    if len(candidates) < min_items:
        return []

    # 2) 按「仅由列表分隔符/空白间隔」把候选聚成链
    chains: List[List[Tuple[int, int, str]]] = []
    cur: List[Tuple[int, int, str]] = []
    for cand in candidates:
        if cur:
            gap = line[cur[-1][1]:cand[0]]
            if not _is_separator_gap(gap):
                if len(cur) >= min_items:
                    chains.append(cur)
                cur = []
        cur.append(cand)
    if len(cur) >= min_items:
        chains.append(cur)

    regions: List[NameRegion] = []
    for chain in chains:
        items = chain
        # 准则 3：形态性 —— 每项都已是 name_shaped（提取时保证）
        # 准则 1：结构性 —— 同构（长度一致）
        lengths = {len(t) for _, _, t in items}
        d16_seed = any(t in _full_name_set() for _, _, t in items)
        if len(lengths) != 1:
            # 长度不一致时，仅在「含 D16 精确命中」时放行
            # （D16 命中本身就是最强的名单证据，可覆盖长度不齐的混合名单）
            if not d16_seed:
                continue
        else:
            base = items[0][2]
            if not all(same_shape(base, t) for _, _, t in items):
                if not d16_seed:
                    continue

        # 准则 2：上下文性 —— 锚点词 或 D16 种子
        chain_abs_start = line_start + items[0][0]
        anchor = has_anchor_before(text, chain_abs_start)
        if not (d16_seed or anchor):
            continue

        abs_items = [(line_start + s, line_start + e, t) for s, e, t in items]
        regions.append(NameRegion(abs_items[0][0], abs_items[-1][1],
                                  abs_items, kind="T2"))
    return regions


def _is_separator_gap(gap: str) -> bool:
    """间隔是否仅由列表分隔符与空白构成（即两项在同一个名单链上）。"""
    for ch in gap:
        if ch not in _LIST_SEPARATORS and not ch.isspace():
            return False
    return True



# ---------------------------------------------------------------------------
# T1：表格姓名列判定
# ---------------------------------------------------------------------------

def _is_masked_name(text: str) -> bool:
    """判断一个值是否为「已被全局规则脱敏的姓名」形态。

    形如 `XXX` / `XXXX` / `XX`（全 X，或 X 与汉字混合的脱敏产物）。

    **为什么需要它**：Word 适配器的执行顺序是「正文处理 → 表格列处理」。
    正文处理已把姓名列的值脱敏成 `XXX`，若形态一致性判定只看原始形态，
    该列会因为「已无姓名形态值」而被误判为非姓名列，导致列级补漏失效。
    把脱敏产物视为**正向姓名证据**可解决这个问题。
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if not re.match(r"^[Xx]+$", stripped):
        return False
    return 2 <= len(stripped) <= 4


def _cell_shape_ratio(values: Sequence[str], min_len: int = 2,
                      allow_extra_surnames: bool = False,
                      relaxed: bool = False) -> float:
    """值列形态一致性：非空值中有多大比例**含有人名证据**。

    - 「已被全局规则脱敏成 XXX 的值」计为姓名证据（见 `_is_masked_name`）；
    - 判定粒度是 **token 级**（见 `_cell_has_name_evidence`），
      因此「科技运营：赤帅超」这类「组织前缀 + 姓名」的单元格
      也算命中（整格形态判定会漏）；
    - `allow_extra_surnames`：放行池外姓（寇/赤…），
      仅在「表头已声明人角色」的列里开启；
    - `relaxed`：启用 B 级宽松形态（名字位字频不限）。
    """
    vals = [v for v in values if v and v.strip()]
    if not vals:
        return 0.0
    ok = 0
    for v in vals:
        if _cell_has_name_evidence(v, allow_extra_surnames=allow_extra_surnames,
                                   relaxed=relaxed):
            ok += 1
    return ok / len(vals)


def detect_name_columns_detailed(header_cells: Sequence[str],
                                 data_rows: Sequence[Sequence[str]],
                                 header_ratio: float = 0.6,
                                 shape_ratio: float = 0.6,
                                 relaxed_ratio: float = 0.4) -> Dict[int, str]:
    """T1 判定（带档位）：返回 {列索引: "strict" | "relaxed"}。

    档位含义：
      - `strict`  —— 表头声明人角色 + 强形态一致率 ≥ `shape_ratio`；
                     遮盖时启用 A 级 + C 级判定。
      - `relaxed` —— 表头声明人角色，但强形态一致率不足（多见于
                     「一半名单外人名」的列，如「BOSS」列 = 杨龙 / 陈鲲）；
                     遮盖时额外启用 B 级宽松形态（名字位字频不限），
                     以救回「陈鲲 / 吴县波」这类名字位不在 D16 库的真名。

    为什么需要 relaxed 档：
        实测（平安信用卡A+项目 PPT slide33/34）「进度负责人」列
        取值为「寇建杰 / 夏凌」——「寇」是池外姓，强形态下一致率
        50% < 60% → 整列不被选中 → 同列「夏凌」明文残留。
        放行池外姓后一致率 100%，整列进入补漏通道。
    """
    result: Dict[int, str] = {}
    for ci, raw_header in enumerate(header_cells):
        if not raw_header:
            continue
        # 多行表头逐行匹配（§4.6 表头跨行）
        header_lines = [ln.strip() for ln in str(raw_header).split("\n") if ln.strip()]
        if not header_lines:
            continue
        person = False
        for h in header_lines:
            if is_person_header(h):
                person = True
                break
        if not person:
            continue
        # 值列形态一致性判定
        values = [row[ci] for row in data_rows if ci < len(row)]
        values = [v for v in values if v and v.strip()]
        if len(values) < 2:
            # 稀疏列：仅凭表头角色词判定
            result[ci] = "strict"
            continue
        strong = _cell_shape_ratio(values, allow_extra_surnames=True)
        if strong >= shape_ratio:
            result[ci] = "strict"
            continue
        loose = _cell_shape_ratio(values, allow_extra_surnames=True, relaxed=True)
        if loose >= relaxed_ratio:
            result[ci] = "relaxed"
    return result


def detect_name_columns(header_cells: Sequence[str],
                        data_rows: Sequence[Sequence[str]],
                        header_ratio: float = 0.6,
                        shape_ratio: float = 0.6) -> List[int]:
    """T1：判定表格中哪些列是「姓名列」。

    判定依据（Issue #50 §4.2）：
      - 表头角色词（锚点词表的 role 类 + 名单类）
      - 值列形态一致性（该列多数值满足姓名形态）

    **这是共用函数**：`table_role_detector` 与 Word 适配器都应调用本函数，
    避免两处实现漂移（Issue #50 §4.1）。

    参数：
      header_cells   —— 表头行各列的文本。**允许含换行**（多行表头聚合，
                         见 Issue #50 §4.6「表头可能跨多行」），
                         逐行独立参与角色词匹配。
      data_rows      —— 数据行二维数组
      header_ratio   —— 表头角色词命中率下限（保留参数，当前判定为逐列）
      shape_ratio    —— 值列形态一致性下限

    返回：姓名列的列索引列表。

    **稀疏数据放宽**：当某列的有效值少于 2 个时，形态一致性无法可靠统计，
    此时仅凭表头角色词判定——表头写明「姓名/成员/组员」本身已是强语义声明，
    不应因数据稀疏而漏掉整列。
    """
    return sorted(detect_name_columns_detailed(
        header_cells, data_rows, header_ratio=header_ratio,
        shape_ratio=shape_ratio).keys())


# ---------------------------------------------------------------------------
# 区域/列上下文的单元格级脱敏
# ---------------------------------------------------------------------------

def _nc_d_name_len(token: str) -> int:
    """D 级：返回 token **开头**应视为姓名（稀有字真名）的长度（0 = 不遮）。

    与 `_nc_name_len` 的差异：不要求名字位字频，但要求
      - 前缀是 2~3 字纯 CJK、姓首、非业务词、文档级低频；
      - 若前缀短于整个 token，则 **token 剩余部分必须以非 CJK 开头**
        （即「陈鲲（38）」这类「姓名 + 括注」结构），
        从而不会把「渠道拓展」这类 4 字业务词的前 3 字误当姓名。
    """
    t = (token or "").strip()
    if not t:
        return 0
    for L in (3, 2):
        if len(t) < L:
            continue
        rest = t[L:]
        if rest and "一" <= rest[0] <= "龥":
            # 剩余仍是 CJK → 这是「渠道拓展」型业务词，不做前缀猜测
            continue
        if _nc_tier_d(t[:L]):
            return L
    return 0


def name_cell_spans(text: str, allow_extra_surnames: bool = False,
                    relaxed: bool = False, weak: bool = True,
                    d_column_confirm_ratio: Optional[float] = None
                    ) -> List[Tuple[int, int]]:
    """返回单元格文本中应被遮盖的姓名片段位置 [(start, end)]（原串坐标）。

    与 `redact_name_cell` 同源，但输出**位置**而非文本——
    PPT 适配器需要按位置回写，才能保住多 run / `<a:br/>` 的结构。

    遮盖粒度统一为 `XXX`（与规则层 `get_replacement("NAME", "XXX")`
    及标准版产物一致），不再按姓名字数给不同长度的掩码。

    `d_column_confirm_ratio`：该列 A/C 级确认率（0~1）。
      - 提供该参数时启用 **D 级补漏通道**（Issue #52）：对 A/C 级
        漏掉的**稀有字真名**补位；
      - 为 None（或文档级词频上下文未注入）时 D 级不启用。
    """
    spans: List[Tuple[int, int]] = []
    if not text:
        return spans
    tokens = _split_cell_name_tokens(text)
    # 第一轮：A/B/C 级强证据
    strong: List[bool] = []
    for tok, _s, _e in tokens:
        t = tok.strip()
        if not t or t in _NC_ROLE_LABELS:
            strong.append(False)
            continue
        ln = _nc_name_len(t, allow_extra_surnames=allow_extra_surnames,
                          relaxed=relaxed, weak=weak)
        strong.append(ln > 0)
    # 第二轮：D 级补漏（只在已确认的姓名列内启用）
    use_d = d_column_confirm_ratio is not None
    cand = [(i, tok) for i, (tok, _s, _e) in enumerate(tokens)
            if tok.strip() and tok.strip() not in _NC_ROLE_LABELS]
    cand_idx = [i for i, _ in cand if not strong[i]]
    has_sibling = any(strong[i] for i, _t in cand)
    for i, (tok, s, _e) in enumerate(tokens):
        t = tok.strip()
        if not t or t in _NC_ROLE_LABELS:
            continue
        if strong[i]:
            ln = _nc_name_len(t, allow_extra_surnames=allow_extra_surnames,
                              relaxed=relaxed, weak=weak)
            # 【半脱敏修复】A 级可能只命中姓 + 前 1 个名字位
            # （如「吴佳翌」→ 命中「吴佳」，尾字「翌」留在明文里）。
            # 在**已确认的姓名列**内，若 D 级判定「整个 token」是一个
            # 更长的真名（3 字），则取更长的遮盖长度 —— 与标准版产物
            # （整名遮成 XXX）一致，也避免出现「XXX翌」这种半脱敏。
            if use_d:
                dln = _nc_d_name_len(t)
                if dln > ln:
                    ln = dln
        else:
            if not use_d:
                continue
            # D1 同格兄弟证据 / D2 同列共识（该列 A/C 确认率 ≥ 50%）
            if not (has_sibling or (len(cand) == 1
                                    and d_column_confirm_ratio >= 0.5)):
                continue
            ln = _nc_d_name_len(t)
        if ln <= 0:
            continue
        lead = len(tok) - len(tok.lstrip())
        spans.append((s + lead, s + lead + ln))
    return spans


def redact_name_cell(text: str, mask: str = "XXX",
                     allow_extra_surnames: bool = False,
                     relaxed: bool = False,
                     weak: bool = True,
                     d_column_confirm_ratio: Optional[float] = None) -> str:
    """在**已确认是姓名列/姓名区**的上下文中补漏脱敏。

    与全局规则的差异：
      - 全局规则受字频闸门约束（高危姓需 2 个名用字、名字位尾字
        必须是「热字」），「陈冠男 / 陆检彪 / 夏凌 / 蔡海姗」这类
        **真实人名**会被漏掉（实测确认）；
      - 列头已声明「这一列是人名」，上下文本身就是证据，
        因此这里对**全局规则漏掉**的项做补漏。

    遮盖方式（Issue #52 重写）：
      - **token 级**：按分隔符 + 角色标签切分，只遮姓名片段，
        组织前缀（「品宣部门公关组」）与括注（「（38）」「（追猎者）」）
        原样保留 —— 与标准版产物一致；
      - **整块遮盖**：遮盖粒度统一为 `XXX`（不再按字数给不同掩码），
        顺带修掉规则层「王倩茹 → XXX茹」的**半脱敏**问题。

    返回脱敏后的文本。
    """
    if not text:
        return text
    spans = name_cell_spans(text, allow_extra_surnames=allow_extra_surnames,
                            relaxed=relaxed, weak=weak,
                            d_column_confirm_ratio=d_column_confirm_ratio)
    if not spans:
        return text
    out = text
    for s, e in reversed(spans):
        out = out[:s] + mask + out[e:]
    return out


def _looks_like_name(text: str) -> bool:
    """宽松姓名形态判定：2-4 字纯 CJK 且首字为姓氏字（或复姓起头）。

    比 `is_name_shaped` 宽松——不要求名字位命中名字池，
    因为「樊霖」的「霖」本就不在名字池里，但列上下文已提供证据。

    **但仍需过「高危姓闸门」**：仅「首字是姓氏」不足以判定，
    「渠道拓展」「白名单」等首字恰为姓氏的常用词会被误判
    （实测复现）。因此要求首字落在 `config.high_risk_surnames`
    ——该集合是全局规则同款闸门：**极少作为常用词首字**的姓氏，
    「渠」「白」「活」「定」等均不在其中，从而天然挡住这类误伤。
    """
    if not _is_name_like(text):
        return False
    for comp in _compound_surnames():
        if text.startswith(comp):
            return True
    return text[0] in _high_risk_surname_set()


def _high_risk_surname_set() -> Set[str]:
    """config.high_risk_surnames —— 全局规则同款高危姓闸门。"""
    try:
        cfg = _load_config()
        return set(cfg.get("high_risk_surnames") or ())
    except Exception:
        return set()


def _is_biz_word(text: str) -> bool:
    """是否命中业务词保护表（业务词不得被当成姓名遮盖）。"""
    try:
        from common_rules import _get_biz_guard_words
        return text in _get_biz_guard_words()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 「全员姓名列」判定（Issue #52 续）
#
# 背景
# ----
# 实测（真实 PPT slide47「报送清单」）：「业务所属」列的表头**不含
# 人角色词**（「业务所属」既不是角色词、也不含「人」字），但该列
# 的取值全是人名（陈柯 / 胡璐 / 唐雨晨 / 董欣 / 杨城鑫 / 刘宇牵头）
# → 表头语义通道不命中 → 整列不进 N-C → 真实人名明文残留。
#
# 口径
# ----
# 走**值列形态**通道而不是放宽表头语义：只有当一列的取值**几乎全部**
# 是「整体就是一个姓名」的短值时，才把该列认定为姓名列。
# 与 T1 的区别：
#   - T1 需要表头声明人角色 → 值列形态只作**辅助**闸门；
#   - 本通道没有表头人角色声明 → 闸门必须更严：
#     值数量 ≥ 3、命中率 ≥ 90%、取值必须**整体**是姓名（不含业务文本）、
#     表头不得命中业务词保护表。
# ---------------------------------------------------------------------------

# 单元格取值尾部可能跟的角色/状态词（「刘宇牵头」「张莉负责」）
_NC_TRAILING_ROLE_WORDS = ("牵头", "负责", "主导", "跟进", "承接",
                            "协办", "经办", "owner")


def _nc_pure_name_value(text: str) -> int:
    """判断单元格取值是否「整体就是一个人名」，返回姓名长度（0 = 不是）。

    允许尾部带一个角色词（「刘宇牵头」→ 姓名部分「刘宇」）。
    """
    t = (text or "").strip()
    if not t:
        return 0
    for w in _NC_TRAILING_ROLE_WORDS:
        if t.endswith(w) and len(t) > len(w):
            t = t[:-len(w)].strip()
            break
    if not (2 <= len(t) <= 3):
        return 0
    if not all("一" <= c <= "龥" for c in t):
        return 0
    if t in _NC_ROLE_LABELS or _is_biz_word(t):
        return 0
    if _nc_name_len(t, allow_extra_surnames=True) > 0:
        return len(t)
    return 0


def detect_name_only_columns(header_cells: Sequence[str],
                             data_rows: Sequence[Sequence[str]],
                             min_values: int = 3,
                             min_ratio: float = 0.9,
                             max_header_len: int = 12
                             ) -> Dict[int, str]:
    """「全员姓名列」判定：表头无人角色词，但取值几乎全是姓名。

    返回 {列索引: "strict"}。

    安全约束（缺一不可）：
      - 有效取值数 ≥ `min_values`（稀疏列不判）；
      - 命中率 ≥ `min_ratio`（几乎每个取值都得是「整体姓名」）；
      - 取值必须是**短**值（整体 2~3 字姓名），带业务文本的一律不算；
      - 表头存在、长度 ≤ `max_header_len`、且不命中业务词保护表。
    """
    result: Dict[int, str] = {}
    for ci, raw_header in enumerate(header_cells):
        h = (raw_header or "").strip()
        if not h or len(h) > max_header_len:
            continue
        if _is_biz_word(h):
            continue
        vals = [row[ci] for row in data_rows
                if ci < len(row) and row[ci] and row[ci].strip()]
        if len(vals) < min_values:
            continue
        ok = sum(1 for v in vals if _nc_pure_name_value(v))
        if ok / len(vals) >= min_ratio:
            result[ci] = "strict"
    return result




# ---------------------------------------------------------------------------
# T3：签名/落款区
# ---------------------------------------------------------------------------

def detect_signature_regions(text: str, tail_chars: int = 200) -> List[NameRegion]:
    """T3：检测文档末尾的签名/落款区。

    判定依据（Issue #50 §4.2）：位置特征（文档末尾）+ 角色词（签名类锚点）。

    置信度为「中」——落款区内容通常很短，误判代价相对可控，
    但仍需保留业务词保护与 D16 校验。
    """
    if not text:
        return []
    tail = text[-tail_chars:] if len(text) > tail_chars else text
    offset = len(text) - len(tail)
    anchors = get_anchor_words().get("sign", ())

    for w in anchors:
        idx = tail.rfind(w)
        if idx < 0:
            continue
        # 取该词之后的内容作为候选区
        seg_start = offset + idx + len(w)
        seg = text[seg_start:]
        items = []
        for m in _CANDIDATE_RE.finditer(seg):
            frag = m.group(0)
            if is_name_shaped(frag):
                items.append((seg_start + m.start(), seg_start + m.end(), frag))
        if items:
            return [NameRegion(items[0][0], items[-1][1], items,
                               kind="T3", confidence="medium")]
    return []


# ---------------------------------------------------------------------------
# 统一对外接口
# ---------------------------------------------------------------------------

def detect_name_regions(text: str, include_signature: bool = True) -> List[NameRegion]:
    """统一入口：检测文本中的全部姓名集中区（T2 + T3）。

    N-C 层作为**前置区域标注器**，输出区域坐标集合，
    后续规则据此调整置信度（Issue #50 §4.1）。
    """
    regions = detect_text_regions(text)
    if include_signature:
        regions.extend(detect_signature_regions(text))
    regions.sort(key=lambda r: r.start)
    return regions


def region_texts(text: str, regions: Optional[List[NameRegion]] = None) -> List[str]:
    """取出所有区域内的项文本（便于测试与统计）。"""
    if regions is None:
        regions = detect_name_regions(text)
    out: List[str] = []
    for r in regions:
        out.extend(r.texts)
    return out
