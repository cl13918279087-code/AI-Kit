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
_ROLE_COLUMN_HEADER_WORDS: Set[str] = {
    # 核心角色列
    "姓名", "负责人", "联系人", "组长", "组员", "成员",
    # 扩展角色列
    "主讲", "汇报人", "审核人", "审批人", "编制人",
    "修订人", "修改人", "复核人", "拟稿人", "校对",
    "协办人", "参会人", "参会人员",
    # 机构角色
    "承办人", "主持人", "主讲人", "记录人",
}

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


def _extract_cell_text(tc_xml: str) -> str:
    """从 <a:tc> 中提取所有 <a:t> 文本内容，HTML 解码后拼接。"""
    parts = []
    for m in _PPT_A_T_RE.finditer(tc_xml):
        parts.append(_html_unescape(m.group(1) or ""))
    return "".join(parts)


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


def _detect_role_columns_in_ppt_table(tbl_xml: str) -> List[int]:
    """
    分析 <a:tbl> XML，检测角色列索引。
    策略：从第一行（表头行）提取所有单元格文本，
    含角色词表的单元格标记为角色列；角色列的相邻列一并纳入（姓名列特征）。
    """
    rows = list(_PPT_TR_RE.finditer(tbl_xml))
    if not rows:
        return []

    header_row = rows[0]
    cells = list(_PPT_TC_RE.finditer(header_row.group(0)))
    role_cols: List[int] = []

    for col_idx, cell_m in enumerate(cells):
        cell_text = _extract_cell_text(cell_m.group(1)).strip()
        # D6（赵辉，第5批，2026-10-09）：表头列名排除（H3 + 长度 + 结尾词）
        # ① 排除含设备/品牌词的表头（H3）
        if any(kw in cell_text for kw in _TBL_HDR_NONNAME):
            continue
        # ② 表头须 ≤20 字（含句读则排除，防止"问题与风险类别"等数据列）
        if len(cell_text) > 20 or any(cell_text.endswith(p) for p in ('。', '，', '；', '！', '？')):
            continue
        # ③ 表头结尾词排除（如"问题与风险类别"）
        if any(cell_text.endswith(end) for end in _TBL_HDR_EXCLUDE_ENDINGS):
            continue
        # 精准匹配角色词（词边界保护）
        for word in _ROLE_COLUMN_HEADER_WORDS:
            if re.search(rf'(?<!\w){re.escape(word)}(?!\w)', cell_text):
                role_cols.append(col_idx)
                break

    if not role_cols:
        return []

    # 角色列右侧所有列纳入（分工表中，角色列右侧通常全是姓名列）
    # 左侧保留精确匹配（分工表中姓名列通常在右侧）
    expanded: Set[int] = set(role_cols)
    last_role = max(role_cols)
    # 角色列之后的所有列（姓名列通常在角色关键词列的右侧）
    for adj in range(last_role + 1, len(cells)):
        adj_text = _extract_cell_text(cells[adj].group(1)).strip()
        # 排除纯数字/纯符号列（如序号、金额）
        if adj_text and not re.match(r'^[\d\-+.()]+$', adj_text):
            expanded.add(adj)
    return sorted(expanded)


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
        role_cols = _detect_role_columns_in_ppt_table(tbl_xml)
        if not role_cols:
            return tbl_xml

        # 逐行处理（跳过表头行 row[0]）
        rows = list(_PPT_TR_RE.finditer(tbl_xml))
        new_rows = [rows[0].group(0)]  # 表头行原样保留
        for row_m in rows[1:]:
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
            body_start = rows[0].end()  # 表头行结束位置
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


def _detect_role_columns_in_xlsx_worksheet(ws_xml: str) -> List[int]:
    """
    分析 XLSX worksheet XML，检测角色列索引（基于第一行表头）。
    """
    rows = list(_XLSX_ROW_RE.finditer(ws_xml))
    if not rows:
        return []

    first_row = rows[0]
    cells = list(_XLSX_CELL_RE.finditer(first_row.group(0)))
    role_cols: List[int] = []

    for cell_m in cells:
        cell_ref = _XLSX_CELL_ATTR_R.search(cell_m.group(1))
        if not cell_ref:
            continue
        col_idx = _cell_ref_col(cell_ref.group(1))
        # 尝试从 <v> 提取文本
        inner = cell_m.group(2)
        v_m = _XLSX_V_RE.search(inner)
        cell_text = v_m.group(1).strip() if v_m else ""
        # 还需要处理内联字符串 <is><t>...</t></is>
        is_m = re.search(r'<is><t[^>]*>(.*?)</t></is>', inner, re.S)
        if is_m:
            cell_text = _html_unescape(is_m.group(1))
        if not cell_text:
            continue
        # D6（赵辉，第5批，2026-10-09）：表头列名排除（H3 + 长度 + 结尾词）
        # ① 排除含设备/品牌词的表头（H3）
        if any(kw in cell_text for kw in _TBL_HDR_NONNAME):
            continue
        # ② 表头须 ≤20 字（含句读则排除，防止"问题与风险类别"等数据列）
        if len(cell_text) > 20 or any(cell_text.endswith(p) for p in ('。', '，', '；', '！', '？')):
            continue
        # ③ 表头结尾词排除
        if any(cell_text.endswith(end) for end in _TBL_HDR_EXCLUDE_ENDINGS):
            continue
        for word in _ROLE_COLUMN_HEADER_WORDS:
            if re.search(rf'(?<!\w){re.escape(word)}(?!\w)', cell_text):
                role_cols.append(col_idx)
                break

    if not role_cols:
        return []

    # 角色列右侧所有列纳入
    expanded: Set[int] = set(role_cols)
    last_role = max(role_cols)
    for adj in range(last_role + 1, len(cells)):
        adj_ref = _XLSX_CELL_ATTR_R.search(cells[adj].group(1))
        if adj_ref:
            adj_col_idx = _cell_ref_col(adj_ref.group(1))
            if adj_col_idx == adj:
                expanded.add(adj)
    return sorted(expanded)


def process_xlsx_worksheet(ws_xml: str) -> Tuple[str, int]:
    """
    对 XLSX worksheet XML 执行表格角色列批量脱敏。

    策略：找第一行（表头行）→ 检测角色列 → 批量脱敏该列及相邻列单元格文本。

    返回：(处理后 XML, 脱敏处数)
    """
    total_count = 0
    role_cols = _detect_role_columns_in_xlsx_worksheet(ws_xml)
    if not role_cols:
        return ws_xml, 0

    rows = list(_XLSX_ROW_RE.finditer(ws_xml))
    if len(rows) < 2:
        return ws_xml, 0

    def _process_row(row_m: re.Match) -> str:
        nonlocal total_count
        row_xml = row_m.group(0)
        # 判断本行是否是第一行（表头行）
        row_idx_matches = re.findall(r'\br="(\d+)"', row_xml)
        if not row_idx_matches:
            return row_xml
        try:
            row_num = int(row_idx_matches[0])
        except ValueError:
            return row_xml
        if row_num == 1:
            return row_xml  # 表头行不处理

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
    """
    names: set = set()
    for m in _NAME_BLOCK_PAT.finditer(text):
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
        names.add(word)
    return names


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
        role_cols = _detect_role_columns_in_ppt_table(tbl_xml)
        if not role_cols:
            continue
        # 扫描所有行（跳过表头行）
        for row_m in _PPT_TR_RE.finditer(tbl_xml):
            row_xml = row_m.group(0)
            cells = list(_PPT_TC_RE.finditer(row_xml))
            for col_idx, cell_m in enumerate(cells):
                if col_idx not in role_cols:
                    continue
                cell_text = _extract_cell_text(cell_m.group(1))
                names = _collect_names_from_role_column(cell_text)
                collected.update(names)

    # 预扫 XLSX 工作表（单 sheet）
    for row_m in _XLSX_ROW_RE.finditer(xml_content):
        row_xml = row_m.group(0)
        row_num_matches = re.findall(r'\br="(\d+)"', row_xml)
        if not row_num_matches:
            continue
        try:
            row_num = int(row_num_matches[0])
        except ValueError:
            continue
        if row_num == 1:
            continue  # 跳过表头行
        for cell_m in _XLSX_CELL_RE.finditer(row_xml):
            cell_full = cell_m.group(0)
            cell_ref_m = _XLSX_CELL_ATTR_R.search(cell_m.group(1))
            if not cell_ref_m:
                continue
            col_idx = _cell_ref_col(cell_ref_m.group(1))
            # 用 _detect_role_columns_in_xlsx_worksheet 判断（需传完整 worksheet XML）
            # 简化：直接复用 _ROLE_COLUMN_EXCLUDED 做格级判断
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
