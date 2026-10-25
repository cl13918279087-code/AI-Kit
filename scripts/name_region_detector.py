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
from typing import Dict, List, Optional, Sequence, Set, Tuple

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


def is_name_shaped(text: str) -> bool:
    """形态性判定：该项是否满足「姓 + 1~3 个名用字」或命中 D16 精确全名库。

    这是区域判定的第三条准则。**不依赖任何上下文**，因此可以脱离
    规则A 的字频闸门独立判断——这正是「名单外人名能被捞出」的关键。

    支持复姓（「欧阳娜娜」「司马光」等）——复姓表见
    `common_rules._NAME_GATE_COMPOUND_SURNAMES`（Issue #49 P1 引入）。
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
        return False
    return all(c in pool for c in text[1:])


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


def _cell_shape_ratio(values: Sequence[str], min_len: int = 2) -> float:
    """值列形态一致性：非空值中有多大比例满足姓名形态。

    「已被全局规则脱敏成 XXX 的值」同样计为姓名证据（见 `_is_masked_name`）。
    """
    vals = [v for v in values if v and v.strip()]
    if not vals:
        return 0.0
    ok = 0
    for v in vals:
        s = v.strip()
        if is_name_shaped(s) or _is_masked_name(s):
            ok += 1
    return ok / len(vals)


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
    anchors = get_anchor_words()
    anchor_set: Set[str] = set(anchors.get("role", ())) | set(anchors.get("list", ()))

    result: List[int] = []
    for ci, raw_header in enumerate(header_cells):
        if not raw_header:
            continue
        # 多行表头逐行匹配（§4.6 表头跨行）
        header_lines = [ln.strip() for ln in str(raw_header).split("\n") if ln.strip()]
        if not header_lines:
            continue
        header_hit = False
        for h in header_lines:
            if h in anchor_set or any(h.endswith(a) for a in anchor_set):
                header_hit = True
                break
        if not header_hit:
            continue
        # 值列形态一致性判定
        values = [row[ci] for row in data_rows if ci < len(row)]
        values = [v for v in values if v and v.strip()]
        if len(values) < 2:
            # 稀疏列：仅凭表头角色词判定
            result.append(ci)
            continue
        if _cell_shape_ratio(values) >= shape_ratio:
            result.append(ci)
    return result


# ---------------------------------------------------------------------------
# 区域/列上下文的单元格级脱敏
# ---------------------------------------------------------------------------

def redact_name_cell(text: str, mask: str = "XX") -> str:
    """在**已确认是姓名列/姓名区**的上下文中补漏脱敏。

    与全局规则的差异：
      - 全局规则受字频闸门约束（高危姓需 2 个名用字），
        「樊霖」这类**高危姓 + 单字名**会被漏掉（实测确认）；
      - 列头已声明「这一列是人名」，上下文本身就是证据，
        因此这里对**全局规则漏掉**的项做补漏。

    补漏条件（保守，宁可漏也不错杀）：
      - 该块是 2-4 字纯 CJK 块；
      - 首字是姓氏字（证明它以人名形态起头）；
      - 未命中业务词保护表；
      - 全局规则处理后**仍未被脱敏**（即确实是漏网项）。

    与全局规则的处理结果合并：已被全局规则脱敏的部分保持不变。

    返回脱敏后的文本。
    """
    if not text:
        return text
    try:
        from common_rules import apply_redactions
    except Exception:
        return text

    base = apply_redactions(text)
    if base == text:
        # 全局规则完全没动它 → 整格判断
        stripped = text.strip()
        if _NAME_ITEM_RE.match(stripped) and _looks_like_name(stripped):
            return mask * len(stripped)
        return text

    # 全局规则动了部分内容：对**剩余**的 CJK 块做补漏判断
    surnames = _surname_set()
    out = []
    pos = 0
    for m in _CANDIDATE_RE.finditer(base):
        frag = m.group(0)
        # 该块在原文中是否已被脱敏（含 X 的块不算）
        if "X" in frag:
            continue
        if not _looks_like_name(frag):
            continue
        if frag[0] not in surnames:
            continue
        if _is_biz_word(frag):
            continue
        out.append(base[pos:m.start()])
        out.append(mask * len(frag))
        pos = m.end()
    out.append(base[pos:])
    return "".join(out)


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
