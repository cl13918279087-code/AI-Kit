#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# table_role_detector.py - N-A 层：表格角色列检测（Issue #16 / REQ-NAME-001）
#
# 功能：当表格表头（含第一行）出现角色词时，批量脱敏该列及相邻列内
# 的 2-4 字符中文词块。用于分工表/联络人表等人名密集场景。
#
# 适用格式：PPTX XML（<a:tbl>）、XLSX XML（<row>/<c>）
# 依赖：re, xml.etree（stdlib）
# ---------------------------------------------------------------------------

from __future__ import annotations

import re
from typing import List, Tuple, Set, Optional, Dict
from html import unescape as _html_unescape

# ---------------------------------------------------------------------------
# 角色列词表（N-A 层触发词）
# ---------------------------------------------------------------------------

# 表头角色词（精准匹配，大小写不敏感但目前均为中文）
#
# N-C（Issue #50 §4.7「锚点词表收敛」）：本表与 common_rules 的段落角色词、
# redact_word 的 Word 表头词表原本**分散三处、口径不一致**。现统一到
# config.json 的 `name_anchor_words.role`，本表保留为**兜底最小集**
# （配置缺失时使用），两者取并集，保证既有能力不退化。
_ROLE_COLUMN_HEADER_WORDS_LOCAL: Set[str] = {
    # 核心角色列
    "姓名", "负责人", "联系人", "组长", "组员", "成员",
    # 扩展角色列
    "主讲", "汇报人", "审核人", "审批人", "编制人",
    "修订人", "修改人", "复核人", "拟稿人", "校对",
    "协办人", "参会人", "参会人员",
    # 机构角色
    "承办人", "主持人", "主讲人", "记录人",
}


def _load_anchor_role_words() -> Set[str]:
    """从 config.json 的 name_anchor_words.role 读取表头角色词。

    配置缺失或读取失败时返回空集（调用方回退到本地最小集）。
    """
    try:
        from common_rules import _load_config
        cfg = _load_config()
        raw = (cfg.get("name_anchor_words") or {}).get("role") or []
        return {w for w in raw if isinstance(w, str) and w}
    except Exception:
        return set()


_ROLE_COLUMN_HEADER_WORDS: Set[str] = (
    _ROLE_COLUMN_HEADER_WORDS_LOCAL | _load_anchor_role_words()
)

# 排除短语（出现则不触发该列的批量脱敏）
_ROLE_COLUMN_EXCLUDED: Set[str] = {
    "今日", "昨日", "明日", "一组", "二组", "三组", "四组",
    "五组", "六组", "本组", "各组", "组长", "组员", "组别",
    "核心组", "开发组", "测试组", "业务组", "运维组",
    "项目组", "专家组", "评审组", "实施组",
    "编号", "序号", "序列", "项目", "阶段", "日期",
    "任务", "模块", "系统",
    # E2（赵辉，第3批，2026-10-08）：设备/网络/系统列名不触发角色列检测
    "服务器", "主机", "虚拟机", "数据库", "存储", "备份",
    "网络设备", "交换机", "路由器", "防火墙", "安全设备",
    "应用系统", "中间件", "操作系统", "软件", "版本",
    "IP地址", "MAC地址", "端口", "带宽", "线路",
    "机房", "机柜", "设备", "终端", "工作站",
    "云资源", "容器", "集群", "节点", "实例",
    # D7（赵辉，第5批，2026-10-09）：语义词守卫扩充
    "风险", "问题", "类别", "情况", "原因", "影响", "措施",
    "内容", "分析", "说明", "意见", "结果", "状态", "进度",
    "金额", "合计", "总计", "小计", "金额",
    "士气", "疲劳", "科技", "厂商", "人员", "成本",
    "日期", "时间", "计划", "实际", "完成", "进度",
    # D7：厂商名前缀守卫（厂商名不脱敏，用户口径）
    "联想", "戴尔", "惠普", "华为", "新华三", "浪潮", "曙光",
    "Cisco", "Juniper", "VMware", "Oracle", "IBM", "微软",
}

# H3（赵辉，第4批，2026-10-08）：表头设备/品牌词排除表
# ⚠ 勿放裸"设备"——"设备负责人"等真姓名列会误排除
_TBL_HDR_NONNAME: Set[str] = {
    "设备厂商", "硬件厂商", "产品厂商", "设备品牌",
    "设备型号", "设备类型", "设备名称", "设备用途",
    "设备编号", "设备状态", "设备位置",
    "网络设备", "安全设备", "存储设备", "备份设备",
    "软件版本", "系统版本", "应用版本",
    "云资源", "容器", "集群", "节点", "实例",
    "IP地址", "MAC地址", "端口号", "带宽", "线路",
}

# D6（赵辉，第5批，2026-10-09）：表头列名排除结尾词
# 含以下词结尾的列名不触发角色列检测（如"问题与风险类别"）
_TBL_HDR_EXCLUDE_ENDINGS: Set[str] = {
    "风险", "问题", "类别", "情况", "原因", "影响", "措施",
    "内容", "分析", "说明", "意见", "结果", "状态",
    "进度", "计划", "目标", "要求", "规范", "标准",
}


def _bulk_redact_chinese(text: str) -> Tuple[str, int]:
    """
    批量脱敏文本中的 2-4 字符中文词块（不含纯数字/英文）。
    排除含排除短语的词块。

    返回：(脱敏后文本, 脱敏处数)
    """
    if not text:
        return text, 0

    # E1（赵辉，第3批，2026-10-08）：整格成句跳过
    # 单元格内容为完整句子（≥15字，含句末标点/动词结构）时跳过整格，
    # 不对格内2-4字块单独脱敏（如"需求、开发、测试"整格保留）
    stripped = text.strip()
    if len(stripped) >= 15 and any(
        stripped.endswith(p) for p in ("。", "，", "；", "！", "？", "：", "、", "…")
    ):
        return text, 0

    # E2（赵辉，第3批，2026-10-08；D7修复，2026-10-09）：设备/网络列整格跳过
    # D7修复：改为词边界精确匹配，避免"设备厂商联想"整体被跳过
    # 只在设备列名（不含姓名词时）精确命中整格才跳过
    for excl in _ROLE_COLUMN_EXCLUDED:
        if excl in stripped and len(stripped) >= 2:
            # 精确匹配：整格等于排除词，或排除词是格内独立词（前后是分隔符/边界）
            if stripped == excl:
                return text, 0
            # 检查排除词是否作为独立词出现（前后为分隔符或边界）
            pat_wb = re.compile(rf'(?<![\u4e00-\u9fa5\w]){re.escape(excl)}(?![\u4e00-\u9fa5\w])')
            if pat_wb.search(stripped):
                return text, 0

    def _repl(m: re.Match) -> str:
        word = m.group(0)
        # 排除含排除短语的词块（如"今日"/"一组"等非姓名语义词）
        for excl in _ROLE_COLUMN_EXCLUDED:
            if excl in word:
                return word
        # 排除纯数字/英文词块
        if re.match(r'^[\dA-Za-z]+$', word):
            return word
        # 排除含数字为主的词块（手机号/日期碎片）
        digit_ratio = sum(c.isdigit() for c in word) / len(word)
        if digit_ratio >= 0.4:
            return word
        return "X" * len(word)

    # 匹配 2-4 个连续CJK字符
    pat = re.compile(r'[\u4e00-\u9fa5]{2,4}')
    result, n = pat.subn(_repl, text)
    return result, n


# ---------------------------------------------------------------------------
# PPT XML 表格处理（<a:tbl>）
# ---------------------------------------------------------------------------

# PPT 表格 XML 结构：
# <a:tbl>
#   <a:tblGrid><a:gridCol .../>...</a:tblGrid>
#   <a:tr> <a:tc>...<a:t>text</a:t>...</a:tc> </a:tr>
# </a:tbl>

_PPT_TBL_RE = re.compile(r'<a:tbl>.*?</a:tbl>', re.S)
_PPT_TR_RE = re.compile(r'<a:tr(?:[^>]*)>.*?</a:tr>', re.S)
_PPT_TC_RE = re.compile(r'<a:tc(?:[^>]*)>(.*?)</a:tc>', re.S)
_PPT_A_T_RE = re.compile(r'<a:t(?:\s[^>]*)?>(.*?)</a:t>', re.S)
# 保留 <a:t> 标签（含属性）的完整匹配
_PPT_A_T_FULL_RE = re.compile(r'(<a:t(?:\s[^>]*)?>)(.*?)(</a:t>)', re.S)
# 单元格内的文本节点与换行节点（<a:br/>）扫描器
_PPT_CELL_TOKEN_RE = re.compile(
    r'(<a:t(?:\s[^>]*)?>(.*?)</a:t>)|(<a:br[^>]*/>)', re.S)


def _extract_cell_text(tc_xml: str) -> str:
    """从 <a:tc> 中提取所有 <a:t> 文本内容，HTML 解码后拼接。"""
    parts = []
    for m in _PPT_A_T_RE.finditer(tc_xml):
        parts.append(_html_unescape(m.group(1) or ""))
    return "".join(parts)


def _cell_context(tc_xml: str):
    """解析单元格：返回 (node_texts, det_text, owner_map)。

    - `node_texts`：各 `<a:t>` 节点的原始文本（HTML 解码后）；
    - `det_text`：**检测用**文本，`<a:br/>` 处补一个 "\n"
      （换行是姓名 token 的天然边界，不补会把两行姓名粘成一个 token）；
    - `owner_map`：det_text 每个字符归属的 (节点下标, 节点内偏移)，
      换行符归属 (-1, -1)。
    """
    node_texts: List[str] = []
    det_parts: List[str] = []
    owner: List[Tuple[int, int]] = []
    for m in _PPT_CELL_TOKEN_RE.finditer(tc_xml):
        if m.group(3):          # <a:br/>
            det_parts.append("\n")
            owner.append((-1, -1))
            continue
        txt = _html_unescape(m.group(2) or "")
        ni = len(node_texts)
        node_texts.append(txt)
        for off, ch in enumerate(txt):
            det_parts.append(ch)
            owner.append((ni, off))
    return node_texts, "".join(det_parts), owner


def _apply_spans_to_nodes(node_texts: List[str], owner: List[Tuple[int, int]],
                          spans: List[Tuple[int, int]], mask: str) -> List[str]:
    """把 det_text 坐标上的姓名片段替换为 mask，按节点回写。

    保真要点（Issue #52）：
      - 姓名可能跨多个 `<a:t>`（run）→ 遮盖文本落在**首个**受影响节点，
        其余节点的对应字符删除，run 结构与 `<a:br/>` 均不受影响；
      - 括注（「（38）」「（追猎者）」）不在遮盖范围内 → 原样保留。
    """
    chars = [list(t) for t in node_texts]
    for (s, e) in sorted(spans, key=lambda x: -x[0]):
        if s < 0 or e > len(owner) or s >= e:
            continue
        segs = [g for g in owner[s:e] if g[0] >= 0]
        if not segs:
            continue
        (ni, oi) = segs[0]
        (nj, oj) = segs[-1]
        if ni == nj:
            chars[ni][oi:oj + 1] = [mask]
        else:
            chars[ni][oi:] = [mask]
            for k in range(ni + 1, nj):
                chars[k] = []
            chars[nj][:oj + 1] = []
    return ["".join(c) for c in chars]


def _rewrite_tc_nodes(tc_xml: str, node_texts: List[str]) -> str:
    """把新的节点文本写回 `<a:t>`（保留标签与属性，文本做 XML 转义）。"""
    it = iter(node_texts)

    def _repl(m: re.Match) -> str:
        try:
            t = next(it)
        except StopIteration:
            return m.group(0)
        safe = (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        return f"{m.group(1)}{safe}{m.group(3)}"

    return _PPT_A_T_FULL_RE.sub(_repl, tc_xml)


def _redact_tc_cell(tc_xml: str, mode: str = "strict",
                    d_column_confirm_ratio: Optional[float] = None
                    ) -> Tuple[str, int]:
    """对单个 `<a:tc>` 做 N-C token 级姓名遮盖。返回 (新 XML, 遮盖处数)。"""
    try:
        from name_region_detector import name_cell_spans
    except Exception:
        return tc_xml, 0
    node_texts, det_text, owner = _cell_context(tc_xml)
    if not det_text.strip():
        return tc_xml, 0
    try:
        spans = name_cell_spans(det_text, allow_extra_surnames=True,
                                relaxed=(mode == "relaxed"),
                                d_column_confirm_ratio=d_column_confirm_ratio)
    except Exception:
        return tc_xml, 0
    if not spans:
        return tc_xml, 0
    new_texts = _apply_spans_to_nodes(node_texts, owner, spans, "XXX")
    if new_texts == node_texts:
        return tc_xml, 0
    return _rewrite_tc_nodes(tc_xml, new_texts), len(spans)


def _get_cell_at_re(m: re.Match, at_re: str) -> str:
    """在 tc_xml 中找到第 n 个 <a:t> 并替换内容，返回新 tc_xml。"""
    tc_xml = m.group(1)
    idx = int(at_re)
    t_matches = list(_PPT_A_T_RE.finditer(tc_xml))
    if idx < len(t_matches):
        t_m = t_matches[idx]
        return tc_xml[:t_m.start()] + f'<a:t>{t_m.group(1)}</a:t>' + tc_xml[t_m.end():]
    return tc_xml


def _rebuild_tc(tc_xml: str, new_text: str) -> str:
    """
    用 new_text 替换 <a:tc> 中所有 <a:t> 的内容（不含标签）。
    单 run：<a:tc><a:t>X</a:t></a:tc> → <a:tc><a:t>XXXX</a:t></a:tc>
    多 run：<a:tc><a:t>X1</a:t><a:t>X2</a:t></a:tc> → <a:tc><a:t>XXXX</a:t><a:t>XXXX</a:t></a:tc>
    """
    def _repl(m: re.Match) -> str:
        # 保留 <a:t> 标签结构，替换内容
        return f'<a:t>{new_text}</a:t>'

    return _PPT_A_T_RE.sub(_repl, tc_xml)


def _detect_role_columns_in_ppt_table(tbl_xml: str) -> Tuple[List[int], int]:
    """
    D15（赵辉，第5批）：逐行判表头——遇有效表头行即停止搜索。

    场景：QRCB s19 表第2行是数据行"组长｜赵维"，其"组长"被误当第二表头，
    导致角色列被污染（整列打码）且第1行数据被排除在数据区外（"赵维"漏脱）。
    策略：从第1行开始遍历，每行用同一套排除规则判断是否为有效表头；
    首次命中即 break，不再扫描后续行（前两行并集方案废弃）。

    返回：(角色列索引列表, 表头行在 rows 数组中的下标)
    """
    rows = list(_PPT_TR_RE.finditer(tbl_xml))
    if not rows:
        return [], -1

    # 逐行找表头
    header_row_idx = -1
    header_row_m = None
    for ri, row_m in enumerate(rows):
        cells = list(_PPT_TC_RE.finditer(row_m.group(0)))
        if not cells:
            continue
        # 排除非空单元格 <2 的合并标题行（如跨列大标题）
        non_empty = sum(
            1 for c in cells
            if _extract_cell_text(c.group(1)).strip()
        )
        if non_empty < 2:
            continue
        # 检查各列是否有角色关键词
        found_role = False
        for col_idx, cell_m in enumerate(cells):
            cell_text = _extract_cell_text(cell_m.group(1)).strip()
            if not cell_text:
                continue
            # D6/H3 排除规则
            if any(kw in cell_text for kw in _TBL_HDR_NONNAME):
                continue
            if len(cell_text) > 20 or any(cell_text.endswith(p) for p in ('。', '，', '；', '！', '？')):
                continue
            if any(cell_text.endswith(end) for end in _TBL_HDR_EXCLUDE_ENDINGS):
                continue
            for word in _ROLE_COLUMN_HEADER_WORDS:
                if re.search(rf'(?<!\w){re.escape(word)}(?!\w)', cell_text):
                    found_role = True
                    break
            if found_role:
                break
        if found_role:
            header_row_idx = ri
            header_row_m = row_m
            break  # D15：找到即停止，不并集

    if header_row_idx < 0 or header_row_m is None:
        return [], -1

    cells = list(_PPT_TC_RE.finditer(header_row_m.group(0)))
    role_cols: List[int] = []

    for col_idx, cell_m in enumerate(cells):
        cell_text = _extract_cell_text(cell_m.group(1)).strip()
        if any(kw in cell_text for kw in _TBL_HDR_NONNAME):
            continue
        if len(cell_text) > 20 or any(cell_text.endswith(p) for p in ('。', '，', '；', '！', '？')):
            continue
        if any(cell_text.endswith(end) for end in _TBL_HDR_EXCLUDE_ENDINGS):
            continue
        for word in _ROLE_COLUMN_HEADER_WORDS:
            if re.search(rf'(?<!\w){re.escape(word)}(?!\w)', cell_text):
                role_cols.append(col_idx)
                break

    if not role_cols:
        return [], header_row_idx

    expanded: Set[int] = set(role_cols)
    last_role = max(role_cols)
    # 角色列之后的列：只有「该列值形态像人名」才纳入
    #
    # 【Issue #52 修复】旧实现把角色列右侧的**所有**非数字列都纳入，
    # 导致「工作职责」这类长业务文本列被整列打码（过遮盖）。
    # 现在改为按列形态判定：只有姓名证据占比达标的列才纳入。
    try:
        from name_region_detector import _cell_shape_ratio
    except Exception:
        _cell_shape_ratio = None
    for adj in range(last_role + 1, len(cells)):
        adj_text = _extract_cell_text(cells[adj].group(1)).strip()
        if not adj_text or re.match(r'^[\d\-+.()]+$', adj_text):
            continue
        if _cell_shape_ratio is None:
            expanded.add(adj)
            continue
        values = []
        for r in rows:
            if r is header_row_m:
                continue
            rcells = list(_PPT_TC_RE.finditer(r.group(0)))
            if adj < len(rcells):
                values.append(_extract_cell_text(rcells[adj].group(1)).strip())
        if not values:
            continue
        if _cell_shape_ratio(values, allow_extra_surnames=True) >= 0.6:
            expanded.add(adj)
    return sorted(expanded), header_row_idx


# ---------------------------------------------------------------------------
# N-C 层 T1 接入 PPT 表格（2026-10-26，Issue #48）
#
# 背景
# ----
# 旧的角色列判定用 `(?<!\w)词(?!\w)` 做表头角色词匹配。Python 的 `\w`
# **包含 CJK 字符**，因此「对接负责人」里的「负责人」前面是 CJK 字「接」，
# 前视断言失败 → 整行不被识别为表头 → `role_cols` 为空 → 该表**完全
# 没有列级脱敏**。
#
# 实测（平安信用卡A+项目 PPT，64 页）：受影响表 13 张、表头 5 种
# （对接负责人 / 开发负责人 / 测试负责人 / 监督负责人 / 进度负责人），
# 导致名单外人名（夏凌等）在**脱敏产物中明文残留**（真实泄漏）。
#
# 修复方式
# --------
# 改走 N-C 语义层的 T1（`name_region_detector.detect_name_columns`）：
# 表头角色词匹配用「表头以角色词结尾」口径（对 CJK 正确），并叠加
# **值列形态一致性**闸门（≥60% 值满足姓名形态），从而不必依赖
# 「角色列右侧所有列都纳入」的激进扩张。
#
# 逐格脱敏用 `redact_name_cell`（保守：只遮姓名形态块，且要求首字落在
# 高危姓闸门集合内），避免整格打码带来的过遮盖。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PPT 表格「逻辑列」展开（Issue #52 续）
#
# 背景
# ----
# PPT 表格支持 `gridSpan`（横向合并）/ `hMerge`（合并续接）/
# `rowSpan` / `vMerge`。合并会让**同一逻辑列在不同行的 `<a:tc>`
# 物理下标不同**：
#
#   实测（真实 PPT slide40 / slide48「捞数表」）：
#     表头行：<a:tc>捞数阶段</a:tc> <a:tc>投产阶段</a:tc>
#             <a:tc>数据提供方（对接人）</a:tc>
#             <a:tc gridSpan="4">捞数需求方（对接人）</a:tc>
#             <a:tc hMerge="1"/> ×3
#     子表头：<a:tc vMerge="1"/>×3 + <a:tc>运营</a:tc>
#             <a:tc>风险</a:tc> <a:tc>市场</a:tc> <a:tc>技术</a:tc>
#     数据行：<a:tc>数迁</a:tc> <a:tc>T-1日…</a:tc> <a:tc>李虎</a:tc>
#             <a:tc gridSpan="3">自动化，CRS中提取</a:tc> …
#
#   若按**物理下标**对齐表头与数据，「捞数需求方（对接人）」这一列
#   在数据行里是 4 个独立子列（谈叙 / 朱丽芸 / 邓君 / 陈瑜 / 胡璐 /
#   董欣 …），物理下标与表头完全对不上 → 整片真名明文残留
#   （实测泄漏 18 处）。
#
# 处理方式
# --------
# 1. **逻辑列对齐**：按 `gridSpan` 累计出每个单元格的逻辑起始列；
#    `hMerge="1"` 的续接单元格在视觉上并入左侧单元格，**不占用**
#    新的逻辑列，只映射到左侧单元格的起始列。
# 2. **合并列组池化**：表头里一个 `gridSpan="N"` 的单元格 = 一个
#    「列组」，组内各逻辑列共享同一表头语义；形态一致性判定按
#    **组内成员列拼接后的值**统计（否则「捞数需求方（对接人）」列
#    的 4 个子列里只有 1 列有名字，形态比会被「--」占位符稀释到
#    阈值以下 → 整组漏脱）。
# ---------------------------------------------------------------------------

_PPT_GRIDSPAN_RE = re.compile(r'gridSpan="(\d+)"')
_PPT_HMERGE_RE = re.compile(r'hMerge="1"')


def _ppt_grid_width(tbl_xml: str) -> int:
    """表格网格列数（`<a:gridCol>` 数量）；取不到时返回 0。"""
    return len(re.findall(r'<a:gridCol\b', tbl_xml))


def _ppt_row_cells(row_xml: str) -> List[Tuple[int, int, str, bool]]:
    """解析一行的单元格 → [(逻辑起始列, 跨列数, 文本, 是否合并续接)]。

    逻辑列号按 `gridSpan` 累计；`hMerge="1"` 的续接单元格不占用新的
    逻辑列（视觉上并入左侧单元格），只映射到左侧单元格的起始列。
    """
    out: List[Tuple[int, int, str, bool]] = []
    col = 0
    last_start: Optional[int] = None
    for m in _PPT_TC_RE.finditer(row_xml):
        raw = m.group(0)
        pos = raw.find('>')
        open_tag = raw[:pos + 1] if pos >= 0 else ''
        text = _extract_cell_text(m.group(1)).strip()
        g = _PPT_GRIDSPAN_RE.search(open_tag)
        span = int(g.group(1)) if g else 1
        if _PPT_HMERGE_RE.search(open_tag) and last_start is not None:
            out.append((last_start, span, text, True))
            continue
        start = col
        out.append((start, span, text, False))
        col += span
        last_start = start
    return out


def _ppt_row_groups(cells: List[Tuple[int, int, str, bool]],
                    grid_w: int) -> List[Tuple[int, int, str]]:
    """把一行单元格合并成「列组」[(起始列, 跨列数, 表头文本)]。

    紧跟在锚点单元格之后的 `hMerge` 续接单元格并入该锚点；
    组的右边界不超过表格网格宽度（PowerPoint 偶尔会写出越界的
    合并续接单元格，它们是空壳，不应把组撑到网格之外）。
    """
    groups: List[Tuple[int, int, str]] = []
    i = 0
    n = len(cells)
    while i < n:
        start, span, text, cont = cells[i]
        if cont:
            i += 1
            continue
        total = span
        j = i + 1
        while j < n and cells[j][3]:
            total += cells[j][1]
            j += 1
        end = start + total
        if grid_w:
            end = min(end, grid_w)
        if end <= start:
            end = start + 1
        groups.append((start, end - start, text))
        i = j
    return groups


def _nc_row_name_ratio(cells: List[str]) -> float:
    """一行单元格中「值形态像姓名」的比例（用于排除「伪表头行」）。"""
    try:
        from name_region_detector import _cell_has_name_evidence
    except Exception:
        return 0.0
    vals = [c for c in cells if c and c.strip()]
    if not vals:
        return 0.0
    ok = 0
    for v in vals:
        try:
            if _cell_has_name_evidence(v, allow_extra_surnames=True):
                ok += 1
        except Exception:
            continue
    return ok / len(vals)


def _nc_ppt_table_model(tbl_xml: str) -> Optional[Dict]:
    """构建 PPT 表格的「逻辑列」模型。

    返回 dict：
      - `header_idx`：表头行下标
      - `groups`：列组 [(起始列, 跨列数, 表头文本)]
      - `cols`：{逻辑列: 档位}
      - `ratios`：{逻辑列: 该列所属组的 A/C 级确认率}
      - `row_cells`：每行的 [(逻辑起始列, 跨列数, 文本, 是否续接)]
      - `logical_rows`：每行的逻辑列值（供外部核对）
    """
    try:
        from name_region_detector import detect_name_columns_detailed
    except Exception:
        return None

    rows = list(_PPT_TR_RE.finditer(tbl_xml))
    if len(rows) < 2:
        return None

    row_cells = [_ppt_row_cells(r.group(0)) for r in rows]
    grid_w = _ppt_grid_width(tbl_xml)

    # 表头候选行：取前 3 行中含角色词最多的那行作为表头
    best_idx, best_score = -1, 0
    for ri in range(min(3, len(rows))):
        cells = [c[2] for c in row_cells[ri] if c[2]]
        # 「值形态像姓名」的行不是表头 —— 否则行向结构里
        # 「捞数执行人 | 数据提供方（对接人） | 戚亚亚 | 李虎」这一行
        # 会被误当表头（其第 2 格含人角色词），导致整行被跳过。
        name_ratio = _nc_row_name_ratio(cells)
        if name_ratio >= 0.5:
            continue
        score = sum(1 for c in cells if _nc_header_is_role(c))
        if score > best_score:
            best_score, best_idx = score, ri
    has_role_header = best_score > 0
    if not has_role_header:
        # 没有「人角色」表头：仍按第 0 行为表头建立逻辑列模型，
        # 以便「全员姓名列」通道（表头无人角色词、取值全是姓名）
        # 生效（实测 slide47「业务所属」列）。
        best_idx = 0

    groups = _ppt_row_groups(row_cells[best_idx], grid_w)
    ncols = grid_w or max((c[0] + c[1] for cs in row_cells for c in cs),
                           default=0)
    if not groups or ncols <= 0:
        return None

    # 每行的逻辑列值（单元格文本落在其逻辑起始列；跨列单元格的
    # 后续列留空 —— 组池化时按成员列拼接，语义等价）
    logical_rows: List[List[str]] = []
    for cs in row_cells:
        vals = [""] * ncols
        for start, span, text, cont in cs:
            if start < ncols and text and not vals[start]:
                vals[start] = text
        logical_rows.append(vals)

    # 列组值：组内成员列拼接（合并表头下的多个子列合成一列判定）
    group_rows: List[List[str]] = []
    for vals in logical_rows:
        grow = []
        for gs, gspan, _t in groups:
            parts = [v for v in vals[gs:gs + gspan] if v and v.strip()]
            grow.append(" ".join(parts))
        group_rows.append(grow)

    data_group_rows = group_rows[best_idx + 1:]
    cols: Dict[int, str] = {}
    if has_role_header:
        vcols = detect_name_columns_detailed([g[2] for g in groups],
                                             data_group_rows)
        for vci, mode in vcols.items():
            gs, gspan, _t = groups[vci]
            for c in range(gs, min(gs + gspan, ncols)):
                cols[c] = mode

    # Issue #52：「全员姓名列」通道 —— 表头不含人角色词、但取值几乎
    # 全是姓名的列（如 slide47「业务所属」列）。只在**逻辑列**粒度上
    # 判定（合并列组的池化值含多个姓名，不适合「整体是姓名」口径）。
    try:
        from name_region_detector import detect_name_only_columns
    except Exception:
        detect_name_only_columns = None
    if detect_name_only_columns is not None:
        logical_header = [""] * ncols
        for gs, gspan, text in groups:
            for c in range(gs, min(gs + gspan, ncols)):
                logical_header[c] = text
        for c, mode in detect_name_only_columns(
                logical_header, logical_rows[best_idx + 1:]).items():
            cols.setdefault(c, mode)

    ratios_v = _nc_group_confirm_ratios(data_group_rows)
    ratios: Dict[int, float] = {}
    for vci, r in ratios_v.items():
        gs, gspan, _t = groups[vci]
        for c in range(gs, min(gs + gspan, ncols)):
            ratios[c] = r

    return {
        "header_idx": best_idx,
        "has_role_header": has_role_header,
        "groups": groups,
        "cols": cols,
        "ratios": ratios,
        "row_cells": row_cells,
        "logical_rows": logical_rows,
        "ncols": ncols,
    }


def _nc_name_columns_ppt(tbl_xml: str) -> Tuple[Dict[int, str], int]:
    """N-C T1：判定 PPT 表格中的姓名列。返回 ({逻辑列索引: 档位}, 表头行下标)。

    档位：`strict`（强形态一致）/ `relaxed`（额外启用 B 级宽松形态）。
    """
    model = _nc_ppt_table_model(tbl_xml)
    if not model:
        return {}, -1
    return model["cols"], model["header_idx"]


def _nc_name_columns_ppt_detail(tbl_xml: str) -> Tuple[Dict[int, str], int,
                                                       List[List[str]]]:
    """N-C T1（含数据行返回）：判定姓名列 + 保留数据行，供 D 级列级确认率使用。

    列索引为**逻辑列**（已按 `gridSpan`/`hMerge` 展开），数据行为
    逻辑列值数组。
    """
    model = _nc_ppt_table_model(tbl_xml)
    if not model:
        return {}, -1, []
    return model["cols"], model["header_idx"], model["logical_rows"]


def _nc_confirm_ratio(values) -> float:
    """一组值里 A/C 级姓名证据的占比（D 级「同列兄弟证据」用）。

    口径：分母只算**纯中文 token**（`--`、`CRS中提取`、`2020/8/22` 这类
    占位/技术串不计入分母，否则占位符会把确认率压到阈值以下）；
    分子算 A 级（姓氏位+名字位字频）与 C 级（高危姓+单字名）。
    """
    try:
        from name_region_detector import (_split_cell_name_tokens,
                                          _nc_name_len, _NC_ROLE_LABELS)
    except Exception:
        return 0.0
    ok = tot = 0
    for v in values:
        if not v or not v.strip():
            continue
        for tok, _s, _e in _split_cell_name_tokens(v):
            t = tok.strip()
            if not t or t in _NC_ROLE_LABELS:
                continue
            if not all("一" <= ch <= "龥" for ch in t):
                continue          # 占位符 / 含 ASCII 的技术串
            tot += 1
            if _nc_name_len(t, allow_extra_surnames=True) > 0:
                ok += 1
    return (ok / tot) if tot else 0.0


def _nc_group_confirm_ratios(group_rows: List[List[str]]) -> Dict[int, float]:
    """按**列组**统计 A/C 级确认率，返回 {组下标: 确认率}。"""
    stats: Dict[int, List[List[str]]] = {}
    for row in group_rows:
        for vci, val in enumerate(row):
            if not val or not val.strip():
                continue
            stats.setdefault(vci, []).append(val)
    return {vci: _nc_confirm_ratio(vals) for vci, vals in stats.items()}


def _nc_column_confirm_ratios(cols: Dict[int, str],
                              data_rows: List[List[str]]) -> Dict[int, float]:
    """计算每个姓名列的 **A/C 级确认率**（D 级同列兄弟证据用）。

    口径：列内所有 token 中，被 A 级（姓氏位+名字位字频）或 C 级
    （高危姓+单字名）确认的比例。B 级（relaxed 档位的宽松形态）
    不计入确认率 —— D 级闸门要求的是**强证据占比**。
    """
    out: Dict[int, float] = {}
    for ci in cols:
        vals = [row[ci] for row in data_rows if ci < len(row)]
        out[ci] = _nc_confirm_ratio(vals)
    return out


def _nc_header_is_role(cell_text: str) -> bool:
    """表头是否声明「人角色」（委托 N-C 共享语义层 `is_person_header`）。

    为什么不再用本地词表：
      - 本地词表口径「表头以角色词结尾」能覆盖「对接负责人」，
        但覆盖不了「负责部门/人」「案例执行人」「组员构成」「BOSS」；
      - N-C 的 `is_person_header` 是**共享**判定（表头语义 + 复合表头
        + 构造式表头 + 英文角色表头），与 Word 适配器同源，避免两处漂移。
    """
    if not cell_text:
        return False
    t = cell_text.strip()
    if len(t) > 20 or any(t.endswith(p) for p in ('。', '，', '；', '！', '？')):
        return False
    if any(kw in t for kw in _TBL_HDR_NONNAME):
        return False
    if any(t.endswith(end) for end in _TBL_HDR_EXCLUDE_ENDINGS):
        return False
    try:
        from name_region_detector import is_person_header
        return is_person_header(t)
    except Exception:
        return False


def _replace_tc_text(tc_xml: str, new_text: str) -> str:
    """把单元格文本整体替换为 new_text，多 run 时只保留首个 run 承载新文本。

    与 `_rebuild_tc` 的区别：`_rebuild_tc` 会把**每个** `<a:t>` 都替换成
    完整的新文本（多 run 单元格 → 文本重复 N 遍）。本函数只把新文本写入
    首个 `<a:t>`，其余 run 清空，保证「原多 run 拆分」的单元格在替换后
    不出现文本重复。
    """
    # 原文经 HTML 解码后可能含 & < >，写回前必须转义，否则破坏 XML。
    safe = (new_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    parts = []
    first = True
    for m in _PPT_A_T_RE.finditer(tc_xml):
        if first:
            parts.append(f'<a:t>{safe}</a:t>')
            first = False
        else:
            parts.append('<a:t></a:t>')
    if not parts:
        return tc_xml
    return _PPT_A_T_RE.sub(lambda _m: parts.pop(0), tc_xml)


# 行标签的人角色词尾（「捞数执行人」「对接人」「负责人」「组长」…）
#
# 为什么不用 `is_person_header` 单判：实测（slide42）首列为
# 「总行业务实施组」——它是**组织**标签，却能命中 `is_person_header`
# 的「以角色词结尾」口径（「实施组」在锚点词表里）→ 若据此触发行向
# 通道，会把整行的业务单元格拖进姓名脱敏。
# 这里额外要求行标签以**人角色词尾**收尾。
_NC_ROW_PERSON_TAIL_RE = re.compile(
    r"(?:人|负责人|联系人|组长|副组长|owner|Owner|OWNER)$")


def _ppt_row_person_cells(row_cells: List[Tuple[int, int, str, bool]],
                          skip_first: bool = True) -> List[int]:
    """「行标签声明人角色」检测：返回该行内应做姓名脱敏的单元格下标。

    背景（Issue #52，真实 PPT slide40/48 的「捞数工单要素」表）：
        该表是**行向**结构 —— 表头是「信息要素 / 填写要求 / 示例1 /
        示例2」，人名出现在**某一行**里：
            r4: 捞数执行人 | 数据提供方（对接人） | 戚亚亚 | 李虎
        首列的行标签「捞数执行人」声明了人角色，但列向判定完全不命中
        → 戚亚亚 / 李虎 明文残留。

    口径（三重闸门）：
      1. 首列是**人角色**标签（`is_person_header`）；
      2. 且该标签以**人角色词尾**收尾（排除「总行业务实施组」这类
         组织标签 —— 它也会命中 `is_person_header` 的角色词结尾口径）；
      3. 且该行**确实含姓名证据**（否则整行都是业务文本，不该触发）。

    脱敏仍走 `name_cell_spans`（要求 A/C 级强证据），因此不会把
    「数据提供方（对接人）」这类标签格误遮。
    """
    try:
        from name_region_detector import is_person_header, name_cell_spans
    except Exception:
        return []
    if not row_cells:
        return []
    first_text = row_cells[0][2]
    if not first_text or not is_person_header(first_text):
        return []
    if not _NC_ROW_PERSON_TAIL_RE.search(first_text.strip()):
        return []
    hits = []
    for idx in range(1 if skip_first else 0, len(row_cells)):
        text = row_cells[idx][2]
        if not text:
            continue
        try:
            if not name_cell_spans(text, allow_extra_surnames=True):
                continue
        except Exception:
            continue
        hits.append(idx)
    return hits


def _apply_nc_name_columns_ppt(tbl_xml: str) -> Tuple[str, int]:
    """对 PPT 表格执行 N-C T1 姓名列判定 + 逐格保守脱敏。

    返回：(处理后 XML, 脱敏处数)

    注意（历史缺陷，2026-10-26 修复）：
      原实现对同一行多个姓名列反复执行
      `new_rows[ri] = row_xml.replace(cell_xml, ...)`，其中 `row_xml`
      始终是**原始行**文本 → 后一次替换把前一次的结果整段覆盖回去，
      导致「同行两个姓名列」实际只保留最后一列的脱敏（实测：
      slide38 表格「对接负责人」列的夏凌 明文残留）。
      现改为：先收集本行所有待替换单元格的 (start, end, 新文本)，
      再一次性按位置切片重建行文本。
    """
    model = _nc_ppt_table_model(tbl_xml)
    if not model:
        return tbl_xml, 0
    cols = model["cols"]
    ratios = model["ratios"]
    header_idx = model["header_idx"]
    row_cells = model["row_cells"]

    rows = list(_PPT_TR_RE.finditer(tbl_xml))
    new_rows = [r.group(0) for r in rows]
    total = 0
    for ri, row_m in enumerate(rows):
        if ri <= header_idx:
            continue          # 表头行（含多行表头的前几行）原样保留
        row_xml = row_m.group(0)
        cells = row_cells[ri]
        # 列向通道：逻辑列命中姓名列
        col_hits = {ci for (ci, _s, _t, _c) in cells if ci in cols}
        # 行向通道：首列是人角色标签的行
        row_hits = set(_ppt_row_person_cells(cells))
        edits = []  # (start, end, 新 XML)
        for idx, (cell_m, (col_idx, _span, _text, _cont)) in enumerate(
                zip(_PPT_TC_RE.finditer(row_xml), cells)):
            in_col = col_idx in col_hits
            in_row = idx in row_hits
            if not in_col and not in_row:
                continue
            cell_xml = cell_m.group(0)
            if not _extract_cell_text(cell_xml).strip():
                continue
            if in_col:
                new_cell, n = _redact_tc_cell(
                    cell_xml, cols[col_idx],
                    d_column_confirm_ratio=ratios.get(col_idx))
            else:
                # 行向通道：只认 A/C 级强证据（严格档）
                new_cell, n = _redact_tc_cell(cell_xml, "strict")
            if n:
                edits.append((cell_m.start(), cell_m.end(), new_cell))
                total += n
        if not edits:
            continue
        pieces = []
        last = 0
        for s, e, xml in edits:
            pieces.append(row_xml[last:s])
            pieces.append(xml)
            last = e
        pieces.append(row_xml[last:])
        new_rows[ri] = "".join(pieces)

    if not total:
        return tbl_xml, 0
    body_start = rows[header_idx].end()
    body_end = tbl_xml.rfind("</a:tbl>")
    return (tbl_xml[:body_start] + "".join(new_rows[header_idx + 1:])
            + tbl_xml[body_end:]), total


def process_ppt_table_xml(slide_xml: str) -> Tuple[str, int]:
    """
    对 PPT 幻灯片 XML 执行表格角色列批量脱敏。

    策略：找到所有 <a:tbl> → 识别角色列 → 批量脱敏该列单元格文本。
    单元格内可能含多个 <a:t>（跨 run），仅替换首个 <a:t> 文本，
    其他 run 文本追加到其后（分工表单元格通常只含一个姓名）。

    返回：(处理后 XML, 脱敏处数)
    """
    total_count = 0

    def _process_tbl(m: re.Match) -> str:
        nonlocal total_count
        tbl_xml = m.group(0)
        # 【Issue #52】优先走 N-C 通道：表头语义 + 列形态一致（双证据），
        # 逐格 token 级遮盖。legacy 角色列通道仅在 N-C 无产出时兜底。
        new_tbl, n_nc = _apply_nc_name_columns_ppt(tbl_xml)
        if n_nc:
            total_count += n_nc
            return new_tbl
        role_cols, header_idx = _detect_role_columns_in_ppt_table(tbl_xml)
        if not role_cols:
            # D15 加固（2026-10-26，Issue #48）：旧的角色词边界匹配
            # `(?<!\w)词(?!\w)` 对 CJK 表头失效（Python 的 \w 含 CJK），
            # 「对接负责人/开发负责人/测试负责人」整行不被识别为表头 →
            # role_cols 为空 → 该表**完全没有列级脱敏** → 名单外人名
            # （实测：夏凌）明文泄漏。改走 N-C 层 T1（表头角色词 +
            # 值列形态一致性）判定 + 逐格保守脱敏（redact_name_cell）。
            new_tbl, n = _apply_nc_name_columns_ppt(tbl_xml)
            total_count += n
            return new_tbl

        # 逐行处理（跳过真正的表头行）
        rows = list(_PPT_TR_RE.finditer(tbl_xml))
        new_rows = [rows[header_idx].group(0)]  # 表头行原样保留
        for ri, row_m in enumerate(rows):
            if ri == header_idx:
                continue  # 跳过表头行
            row_xml = row_m.group(0)
            cells = list(_PPT_TC_RE.finditer(row_xml))
            # 对角色列执行批量脱敏
            for col_idx, cell_m in enumerate(cells):
                if col_idx not in role_cols:
                    continue
                cell_xml = cell_m.group(0)
                cell_text = _extract_cell_text(cell_xml)
                redacted_text, n = _bulk_redact_chinese(cell_text)
                if n > 0:
                    new_cell_xml = _rebuild_tc(cell_xml, redacted_text)
                    row_xml = row_xml.replace(cell_xml, new_cell_xml, 1)
                    total_count += n
            new_rows.append(row_xml)

        # 重建表格：保留表头行，替换后续行
        result = tbl_xml
        if len(rows) > 1:
            body_start = rows[header_idx].end()  # 真正的表头行结束位置
            body_end = tbl_xml.rfind("</a:tbl>")
            new_body_rows = "".join(new_rows[1:])
            result = tbl_xml[:body_start] + new_body_rows + tbl_xml[body_end:]
        return result

    # subn 返回值是匹配次数（≠ total_count），手动遍历累加
    result = _PPT_TBL_RE.sub(_process_tbl, slide_xml)
    return result, total_count


# ---------------------------------------------------------------------------
# XLSX XML 表格处理（<worksheet>/<row>/<c>）
# ---------------------------------------------------------------------------

# XLSX 表格 XML 结构：
# <worksheet>
#   <sheetData>
#     <row r="1"><c r="A1" t="s"><v>0</v></c>...</row>
#     <row r="2"><c r="A2"><v>张三</v></c>...</row>
#   </sheetData>
# </worksheet>
# 文本内容在 <v>（值为共享字符串索引）或直接 <v>文本</v>（内联字符串）

_XLSX_ROW_RE = re.compile(r'<row\b[^>]*>.*?</row>', re.S)
_XLSX_CELL_RE = re.compile(r'<c\b([^>]*)>(.*?)</c>', re.S)
_XLSX_V_RE = re.compile(r'<v>(.*?)</v>', re.S)
_XLSX_CELL_ATTR_R = re.compile(r'\br="([A-Z]+\d+)"')


def _cell_ref_col(ref: str) -> int:
    """从单元格引用（如 'B3'）提取列索引（A=0, B=1, ...）。"""
    col_str = "".join(c for c in ref if c.isalpha())
    idx = 0
    for c in col_str:
        idx = idx * 26 + (ord(c) - ord('A') + 1)
    return idx - 1


def _detect_role_columns_in_xlsx_worksheet(ws_xml: str) -> Tuple[List[int], int]:
    """
    D15（赵辉，第5批）：逐行判表头——XLSX 版本。

    返回：(角色列索引列表, 表头行号)；无表头时返回 ([], -1)。
    """
    rows = list(_XLSX_ROW_RE.finditer(ws_xml))
    if not rows:
        return [], -1

    # 逐行找表头
    header_row_num = -1
    header_row_cells = None
    for row_m in rows:
        row_nums = re.findall(r'\br="(\d+)"', row_m.group(0))
        if not row_nums:
            continue
        try:
            row_num = int(row_nums[0])
        except ValueError:
            continue
        cells = list(_XLSX_CELL_RE.finditer(row_m.group(0)))
        if not cells:
            continue
        # 排除非空单元格 <2 的合并标题行
        non_empty = 0
        for c in cells:
            inner = c.group(2)
            v_m = _XLSX_V_RE.search(inner)
            is_m = re.search(r'<is><t[^>]*>(.*?)</t></is>', inner, re.S)
            cell_text = ""
            if v_m:
                cell_text = v_m.group(1).strip()
            elif is_m:
                cell_text = _html_unescape(is_m.group(1))
            if cell_text:
                non_empty += 1
        if non_empty < 2:
            continue
        # 检查是否有角色关键词
        found_role = False
        for cell_m in cells:
            inner = cell_m.group(2)
            v_m = _XLSX_V_RE.search(inner)
            cell_text = v_m.group(1).strip() if v_m else ""
            is_m = re.search(r'<is><t[^>]*>(.*?)</t></is>', inner, re.S)
            if is_m:
                cell_text = _html_unescape(is_m.group(1))
            if not cell_text:
                continue
            if any(kw in cell_text for kw in _TBL_HDR_NONNAME):
                continue
            if len(cell_text) > 20 or any(cell_text.endswith(p) for p in ('。', '，', '；', '！', '？')):
                continue
            if any(cell_text.endswith(end) for end in _TBL_HDR_EXCLUDE_ENDINGS):
                continue
            for word in _ROLE_COLUMN_HEADER_WORDS:
                if re.search(rf'(?<!\w){re.escape(word)}(?!\w)', cell_text):
                    found_role = True
                    break
            if found_role:
                break
        if found_role:
            header_row_num = row_num
            header_row_cells = cells
            break  # D15：找到即停止

    if header_row_num < 0 or header_row_cells is None:
        return [], -1

    role_cols: List[int] = []
    for cell_m in header_row_cells:
        cell_ref = _XLSX_CELL_ATTR_R.search(cell_m.group(1))
        if not cell_ref:
            continue
        col_idx = _cell_ref_col(cell_ref.group(1))
        inner = cell_m.group(2)
        v_m = _XLSX_V_RE.search(inner)
        cell_text = v_m.group(1).strip() if v_m else ""
        is_m = re.search(r'<is><t[^>]*>(.*?)</t></is>', inner, re.S)
        if is_m:
            cell_text = _html_unescape(is_m.group(1))
        if not cell_text:
            continue
        if any(kw in cell_text for kw in _TBL_HDR_NONNAME):
            continue
        if len(cell_text) > 20 or any(cell_text.endswith(p) for p in ('。', '，', '；', '！', '？')):
            continue
        if any(cell_text.endswith(end) for end in _TBL_HDR_EXCLUDE_ENDINGS):
            continue
        for word in _ROLE_COLUMN_HEADER_WORDS:
            if re.search(rf'(?<!\w){re.escape(word)}(?!\w)', cell_text):
                role_cols.append(col_idx)
                break

    if not role_cols:
        return [], header_row_num

    # 角色列右侧所有列纳入
    expanded: Set[int] = set(role_cols)
    last_role = max(role_cols)
    for adj in range(last_role + 1, len(header_row_cells)):
        adj_ref = _XLSX_CELL_ATTR_R.search(header_row_cells[adj].group(1))
        if adj_ref:
            adj_col_idx = _cell_ref_col(adj_ref.group(1))
            if adj_col_idx == adj:
                expanded.add(adj)
    return sorted(expanded), header_row_num


def process_xlsx_worksheet(ws_xml: str) -> Tuple[str, int]:
    """
    对 XLSX worksheet XML 执行表格角色列批量脱敏。

    策略：找第一行（表头行）→ 检测角色列 → 批量脱敏该列及相邻列单元格文本。

    返回：(处理后 XML, 脱敏处数)
    """
    total_count = 0
    role_cols, header_row_num = _detect_role_columns_in_xlsx_worksheet(ws_xml)
    if not role_cols:
        return ws_xml, 0

    rows = list(_XLSX_ROW_RE.finditer(ws_xml))
    if len(rows) < 2:
        return ws_xml, 0

    def _process_row(row_m: re.Match) -> str:
        nonlocal total_count
        row_xml = row_m.group(0)
        # 判断本行是否是真正的表头行（用 D15 逐行检测结果）
        row_idx_matches = re.findall(r'\br="(\d+)"', row_xml)
        if not row_idx_matches:
            return row_xml
        try:
            row_num = int(row_idx_matches[0])
        except ValueError:
            return row_xml
        if row_num == header_row_num:
            return row_xml  # 真正的表头行不处理

        new_row = row_xml
        for cell_m in _XLSX_CELL_RE.finditer(row_xml):
            cell_full = cell_m.group(0)
            cell_ref_m = _XLSX_CELL_ATTR_R.search(cell_m.group(1))
            if not cell_ref_m:
                continue
            col_idx = _cell_ref_col(cell_ref_m.group(1))
            if col_idx not in role_cols:
                continue
            inner = cell_m.group(2)
            # 提取文本
            v_m = _XLSX_V_RE.search(inner)
            is_m = re.search(r'<is><t[^>]*>(.*?)</t></is>', inner, re.S)
            cell_text = ""
            if v_m:
                cell_text = v_m.group(1).strip()
            if is_m:
                cell_text = _html_unescape(is_m.group(1))
            if not cell_text:
                continue
            redacted_text, n = _bulk_redact_chinese(cell_text)
            if n > 0:
                total_count += n
                if v_m:
                    new_row = new_row.replace(cell_full, cell_full.replace(v_m.group(0), f"<v>{redacted_text}</v>"), 1)
                elif is_m:
                    new_row = new_row.replace(cell_full,
                        cell_full.replace(is_m.group(0), f"<is><t>{redacted_text}</t></is>"), 1)
        return new_row

    result = _XLSX_ROW_RE.subn(_process_row, ws_xml)[0]
    return result, total_count


# ---------------------------------------------------------------------------
# D15（赵辉，第5批，2026-10-09）：表头驱动列姓名预扫 → 全局注入
#
# 预扫表格姓名列取值 → 注入 common_rules 的额外姓名集 → 全文同名替换。
# 用途：正文引用"王根利的B角"不在表格列内，仅靠列脱敏够不着，
# 须通过姓名集注入实现跨正文联动。
# ---------------------------------------------------------------------------

# 模块级姓名收集（跨表格累积）
_TABLE_COLLECTED_NAMES: set = set()

# 姓名块正则（与 _bulk_redact_chinese 口径一致）
_NAME_BLOCK_PAT = re.compile(r'[\u4e00-\u9fa5]{2,4}')


def _collect_names_from_role_column(text: str) -> set:
    """
    从角色列单元格文本中提取候选姓名（供全局注入）。
    仅提取2-4字CJK块，排除已知的非姓名词。

    D15 加固（2026-10-26，Issue #48 差异归因）：**姓名形态闸门**

    背景：本函数原先只靠「排除词黑名单」过滤，产出的是「像名字的碎片」，
    而非「人名」。真实 PPT 实测（平安信用卡A+项目，64 页，采集 535 个串）：
        命中 D16 全名库 108 / 形态像姓名 165 / **非人名 261 / 业务词 1**
    非人名样例：上线期间、上线策略、业务支持、事项、制定协调、问题解决、
    食宿保障、配置、部署……

    这些碎片来自**表格单元格粘连**（单元格正文被相邻内容黏连，切块后
    落进角色列）。它们经 D15 注入后会在**全文**被替换 → 大面积过遮盖，
    直接放大 #48 表格里的「脱敏版 XXX 数远多于标准版」差异。

    闸门口径（与 Issue #50 N-C 层形态判定一致）：
        `is_name_shaped(word)` = 命中 D16 全名库，或满足「姓 + 1~3 名用字」
        （支持复姓）。
    实测闸门效果（同一文档）：
        保留 273（108 D16 真名 + 165 形态姓名）/ 拦掉 262（261 非人名 + 1 业务词）
        **被拦掉的 D16 真名：0**（不产生漏脱）
        **保留集合中的非人名：0**（无误伤残留）

    附加保留通道：若整个单元格文本**恰好等于**该 2-4 字块，则视为
    「独立单元格里的一个完整条目」，即便不在形态池内也保留——用于兜住
    名字用字不在池中的名单外真名（如生僻字姓名）。
    """
    names: set = set()
    stripped = (text or "").strip()
    for m in _NAME_BLOCK_PAT.finditer(text or ""):
        word = m.group(0)
        # 排除含排除词的块
        skip = False
        for excl in _ROLE_COLUMN_EXCLUDED:
            if excl in word:
                skip = True
                break
        if skip:
            continue
        # 排除数字为主的块
        digit_ratio = sum(c.isdigit() for c in word) / len(word)
        if digit_ratio >= 0.4:
            continue
        # 排除含"账"字结尾（业务词，如"内部账/总账/对账"）
        if word.endswith("账"):
            continue
        # 排除H3设备词
        if any(kw in word for kw in _TBL_HDR_NONNAME):
            continue
        # D15 加固：姓名形态闸门（拦掉粘连碎片等非人名）
        if not _is_name_shaped_word(word):
            # 附加保留通道：**3 字**整格 → 保留（兜住池外真名）。
            # 限长 3 字的理由（实测 535 串的构成）：
            #   · 4 字整格几乎全是业务短语（工作职责、上线策略、食宿保障…）
            #   · 2 字整格同样是业务短语高频区（事项、配置、部署、供电…），
            #     而 2 字真名已由 D16 全名库 + 形态池（姓+1 名用字）覆盖
            #   · 3 字池外真名（生僻字姓名）是形态池覆盖不到的真实缺口
            if stripped != word or len(word) != 3:
                continue
        names.add(word)
    return names


def _is_name_shaped_word(word: str) -> bool:
    """姓名形态判定（D15 注入闸门）。

    复用 N-C 语义层的 `is_name_shaped`（Issue #50），避免两处实现漂移；
    N-C 模块导入失败时降级为「只信 D16 全名库」。
    """
    try:
        from name_region_detector import is_name_shaped
    except Exception:
        try:
            from common_rules import get_full_name_set
            return word in get_full_name_set()
        except Exception:
            return False
    try:
        return bool(is_name_shaped(word))
    except Exception:
        return False


def collect_table_names(xml_content: str) -> set:
    """
    D15（赵辉，第5批，2026-10-09）：预扫整个文档 XML，收集表格角色列中的姓名。
    供 redact_ppt.py / redact_excel.py 在主流程开始前调用，
    将收集到的姓名注入 common_rules 的额外姓名集（_EXTRA_NAMES）。
    """
    global _TABLE_COLLECTED_NAMES
    collected: set = set()

    # 预扫 PPT 表格
    for tbl_m in _PPT_TBL_RE.finditer(xml_content):
        tbl_xml = tbl_m.group(0)
        role_cols, header_idx = _detect_role_columns_in_ppt_table(tbl_xml)
        if not role_cols:
            continue
        # 扫描所有行（跳过真正的表头行）
        rows = list(_PPT_TR_RE.finditer(tbl_xml))
        for ri, row_m in enumerate(rows):
            if ri == header_idx:
                continue  # 跳过表头行
            row_xml = row_m.group(0)
            cells = list(_PPT_TC_RE.finditer(row_xml))
            for col_idx, cell_m in enumerate(cells):
                if col_idx not in role_cols:
                    continue
                cell_text = _extract_cell_text(cell_m.group(1))
                names = _collect_names_from_role_column(cell_text)
                collected.update(names)

    # 预扫 XLSX 工作表（D15：逐 sheet 检测表头行）
    # 每个 <worksheet>...</worksheet> 独立检测表头行号
    _XLSX_SHEET_RE = re.compile(r'<worksheet\b[^>]*>.*?</worksheet>', re.S)
    for sheet_m in _XLSX_SHEET_RE.finditer(xml_content):
        ws_xml = sheet_m.group(0)
        role_cols, header_row_num = _detect_role_columns_in_xlsx_worksheet(ws_xml)
        if not role_cols:
            continue
        for row_m in _XLSX_ROW_RE.finditer(ws_xml):
            row_xml = row_m.group(0)
            row_num_matches = re.findall(r'\br="(\d+)"', row_xml)
            if not row_num_matches:
                continue
            try:
                row_num = int(row_num_matches[0])
            except ValueError:
                continue
            if row_num == header_row_num:
                continue  # 跳过真正的表头行
            for cell_m in _XLSX_CELL_RE.finditer(row_xml):
                cell_full = cell_m.group(0)
                cell_ref_m = _XLSX_CELL_ATTR_R.search(cell_m.group(1))
                if not cell_ref_m:
                    continue
                col_idx = _cell_ref_col(cell_ref_m.group(1))
                if col_idx not in role_cols:
                    continue
                inner = cell_m.group(2)
                v_m = _XLSX_V_RE.search(inner)
                is_m = re.search(r'<is><t[^>]*>(.*?)</t></is>', inner, re.S)
                cell_text = ""
                if v_m:
                    cell_text = v_m.group(1).strip()
                if is_m:
                    cell_text = _html_unescape(is_m.group(1))
                if cell_text:
                    names = _collect_names_from_role_column(cell_text)
                    collected.update(names)

    _TABLE_COLLECTED_NAMES.update(collected)
    return collected


def get_table_collected_names() -> set:
    """返回已收集的表格姓名集合（供外部注入）"""
    return _TABLE_COLLECTED_NAMES


def clear_table_collected_names() -> None:
    """清空表格姓名收集（文档处理完成后调用，避免跨文档污染）"""
    global _TABLE_COLLECTED_NAMES
    _TABLE_COLLECTED_NAMES.clear()


# ---------------------------------------------------------------------------
# 公共入口（供 redact_ppt.py / redact_excel.py 调用）
# ---------------------------------------------------------------------------

def process_ppt_slide_xml(slide_xml: str) -> Tuple[str, int]:
    """
    处理 PPT 幻灯片 XML（含段落级 + 表格级脱敏）。
    供 redact_ppt.py 的 _process_xml_file 调用。
    返回：(处理后 XML, 角色列批量脱敏处数)
    """
    return process_ppt_table_xml(slide_xml)


def process_xlsx_sheet_xml(sheet_xml: str) -> Tuple[str, int]:
    """
    处理 XLSX 工作表 XML（含角色列批量脱敏）。
    供 redact_excel.py 的 _process_xml_file 调用。
    返回：(处理后 XML, 角色列批量脱敏处数)
    """
    return process_xlsx_worksheet(sheet_xml)
