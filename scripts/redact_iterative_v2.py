#!/usr/bin/env python3
"""
脱敏迭代比对分析脚本 v2
每次触发时：
1. 实际运行脱敏管道，对原文件进行脱敏
2. 与标准脱敏文件进行结构化比对
3. 生成差异分析报告（含漏检、误检、可疑项）
4. 提供脱敏程序包修订方案
5. 标记完成状态

支持 6 组文件，持续迭代直至每组完成 3 轮且无改善余地。
"""

import sys
import os
import json
import re
import zipfile
import tempfile
import shutil
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, asdict
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WORK_DIR = Path("/Users/clzxr/WorkBuddy/Claw/工作目录")
OUTPUT_DIR = WORK_DIR / ".redact_v2_output"
STATE_FILE = WORK_DIR / ".redact_v2_state.json"
REPORT_DIR = WORK_DIR / ".redact_v2_reports"
TEMP_DIR = Path(tempfile.mkdtemp(prefix="redact_iter_"))

# 6组文件配置
FILE_GROUPS = [
    {
        "name": "郑州银行新核心项目启动会材料",
        "id": "group1",
        "original": "郑州银行新核心项目_启动会材料V0.6-20170308.pptx",
        "standard": "XX银行新核心项目_启动会材料V0.6-20170308_脱敏标准版.pptx",
        "ext": "pptx",
    },
    {
        "name": "郑州银行新一代信息系统差异分析启动会",
        "id": "group2",
        "original": "郑州银行新一代信息系统建设项目文档_差异分析启动会.docx",
        "standard": "XX银行新一代信息系统建设项目文档_差异分析启动会_脱敏标准版.docx",
        "ext": "docx",
    },
    {
        "name": "福建海峡银行业务演练切换操作指南",
        "id": "group3",
        "original": "福建海峡银行新核心项目第四轮业务演练切换操作指南V0.2.doc",
        "standard": "XX银行新核心项目第四轮业务演练切换操作指南V0.2_脱敏标准版.doc",
        "ext": "doc",
    },
    {
        "name": "PMO-QA人员分工表",
        "id": "group4",
        "original": "PMO，QA组人员分工表-20171120.xlsx",
        "standard": "PMO，QA组人员分工表-20171120_脱敏标准版.xlsx",
        "ext": "xlsx",
    },
    {
        "name": "尖刀测试内容",
        "id": "group5",
        "original": "尖刀测试内容.xlsx",
        "standard": "尖刀测试内容_脱敏标准版.xlsx",
        "ext": "xlsx",
    },
    {
        "name": "尖刀组UAT3测试规划",
        "id": "group6",
        "original": "尖刀组UAT3测试规划0907.docx",
        "standard": "尖刀组UAT3测试规划0907_脱敏标准版.docx",
        "ext": "docx",
    },
]


# ============================================================================
# 文本提取
# ============================================================================

def extract_pptx_texts(path: Path) -> List[str]:
    """提取PPTX每页幻灯片的文本"""
    texts = []
    try:
        with zipfile.ZipFile(path, 'r') as z:
            slides = sorted(
                [n for n in z.namelist() if re.match(r'ppt/slides/slide\d+\.xml', n)]
            )
            for slide_name in slides:
                content = z.read(slide_name).decode('utf-8')
                slide_text = extract_text_from_xml(content)
                if slide_text.strip():
                    texts.append(slide_text)
    except Exception as e:
        return [f"[PPTX读取错误: {e}]"]
    return texts


def extract_docx_texts(path: Path) -> List[str]:
    """提取DOCX每段落的文本"""
    texts = []
    try:
        with zipfile.ZipFile(path, 'r') as z:
            content = z.read('word/document.xml').decode('utf-8')
            # 按段落分割
            paras = re.split(r'</w:p>', content)
            for para in paras:
                para_text = extract_text_from_xml(para)
                if para_text.strip():
                    texts.append(para_text)
    except Exception as e:
        return [f"[DOCX读取错误: {e}]"]
    return texts


def extract_xlsx_texts(path: Path) -> List[str]:
    """提取XLSX每个sheet的文本"""
    texts = []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_parts = []
            for row in ws.iter_rows():
                row_texts = []
                for cell in row:
                    if cell.value is not None:
                        row_texts.append(str(cell.value))
                if row_texts:
                    sheet_parts.append(' '.join(row_texts))
            if sheet_parts:
                texts.append('\n'.join(sheet_parts))
    except Exception as e:
        return [f"[XLSX读取错误: {e}]"]
    return texts


def extract_doc_texts(path: Path) -> List[str]:
    """提取DOC文本（通过LibreOffice转换为DOCX后提取）"""
    try:
        # 使用LibreOffice将DOC转换为DOCX，然后提取文本
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ['/Applications/LibreOffice.app/Contents/MacOS/soffice',
                 '--headless', '--convert-to', 'docx',
                 '--outdir', tmpdir, str(path)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                # 降级：使用基础方法
                return _extract_doc_texts_fallback(path)

            # 查找转换后的文件
            docx_name = Path(path).stem + '.docx'
            converted = Path(tmpdir) / docx_name
            if not converted.exists():
                candidates = list(Path(tmpdir).glob('*.docx'))
                if candidates:
                    converted = candidates[0]
                else:
                    return _extract_doc_texts_fallback(path)

            # 从DOCX提取文本
            return extract_docx_texts(converted)
    except subprocess.TimeoutExpired:
        return ["[DOC转换超时]"]
    except Exception as e:
        return [f"[DOC转换错误: {e}]"]


def _extract_doc_texts_fallback(path: Path) -> List[str]:
    """DOC文本提取降级方案（不经过LibreOffice，仅适用于超简单DOC）"""
    try:
        with open(path, 'rb') as f:
            raw = f.read()
        try:
            text = raw.decode('utf-16-le', errors='strict')
            if '\x00' in text[:100]:
                text = ''.join(c for c in text if c.isprintable() or c in '\n\r\t ')
                text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\n\r\t ]+', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                if len(text) > 50:
                    return [text]
        except:
            pass
        text = ''.join(c for c in raw.decode('latin-1', errors='replace') if c.isprintable())
        text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\n\r\t ]+', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 100:
            return [text]
        return [f"[DOC文本提取受限，长度={len(text)}]"]
    except Exception as e:
        return [f"[DOC读取错误: {e}]"]


def extract_text_from_xml(xml_content: str) -> str:
    """从XML提取文本内容"""
    texts = re.findall(r'<w:t[^>]*>([^<]*)</w:t>', xml_content)
    texts2 = re.findall(r'>([^<]+)<', xml_content)
    combined = ' '.join(texts + texts2)
    combined = re.sub(r'\s+', ' ', combined).strip()
    return combined


def extract_all_texts(path: Path, ext: str) -> List[str]:
    if ext == 'pptx':
        return extract_pptx_texts(path)
    elif ext == 'docx':
        return extract_docx_texts(path)
    elif ext == 'xlsx':
        return extract_xlsx_texts(path)
    elif ext == 'doc':
        return extract_doc_texts(path)
    return []


# ============================================================================
# 脱敏管道（实际运行）
# ============================================================================

def run_redaction(input_path: Path, output_path: Path, ext: str) -> Tuple[bool, str]:
    """运行脱敏管道，返回 (success, error_message)"""
    try:
        if ext == 'docx':
            from scripts.redact_word import redact_docx
            redact_docx(str(input_path), str(output_path))
            return True, ""
        elif ext == 'doc':
            # 先用LibreOffice转换
            conv_path = TEMP_DIR / f"{input_path.stem}.docx"
            result = subprocess.run(
                ['/Applications/LibreOffice.app/Contents/MacOS/soffice', '--headless', '--convert-to', 'docx', '--outdir', str(TEMP_DIR), str(input_path)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                return False, f"LibreOffice转换失败: {result.stderr}"
            converted = TEMP_DIR / f"{input_path.stem}.docx"
            if not converted.exists():
                # 尝试其他可能的名字
                possible = list(TEMP_DIR.glob(f"{input_path.stem}*.docx"))
                if possible:
                    converted = possible[0]
                else:
                    return False, f"LibreOffice转换后文件未找到"
            from scripts.redact_word import redact_docx
            redact_docx(str(converted), str(output_path))
            return True, ""
        elif ext == 'pptx':
            from scripts.redact_ppt import redact_pptx
            redact_pptx(str(input_path), str(output_path))
            return True, ""
        elif ext == 'xlsx':
            from scripts.redact_excel import redact_excel
            redact_excel(str(input_path), str(output_path))
            return True, ""
        else:
            return False, f"不支持的文件类型: {ext}"
    except Exception as e:
        import traceback
        return False, f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"


# ============================================================================
# 比对分析
# ============================================================================

def comprehensive_normalize(text: str) -> str:
    """
    全面的文本标准化 — 消除文档结构差异对对比的影响。
    
    处理内容：
    1. Unicode NFC 规范化（消除全角/半角等价差异）
    2. 移除文本段落分隔符（|||）— 消除段落边界差异
    3. 统一换行符 (CRLF/LF/CR → LF)
    4. 移除所有空白字符（空格、Tab、不间断空格、全角空格、换行）
    5. 移除页眉页脚模式（"第 X 页"、"Page X"）
    6. 移除文档元数据残留（[DOC]、日期模式等）
    7. 折叠内部连续标点
    
    注意：最终移除所有空白，保持与 normalize_text_for_comparison 一致的无空白输出。
    """
    import unicodedata
    
    # 1. Unicode NFC 规范化
    text = unicodedata.normalize('NFC', text)
    
    # 2. 移除段落分隔符（join文本时插入的分隔符）
    text = text.replace('|||', '')
    
    # 3. 统一换行符
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    
    # 4. 移除各类空白字符
    # 普通空格、Tab、不间断空格(U+00A0)、全角空格(U+3000)、换行符
    text = re.sub(r'[\t \u00a0\u3000\n\r]+', '', text)
    
    # 5. 移除页码模式
    text = re.sub(r'第\s*[\d零一二三四五六七八九十百千]+\s*页', '', text)
    text = re.sub(r'Page\s*\d+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'-\s*\d+\s*-', '', text)  # "- 3 -" 格式
    
    # 6. 移除文档结构残留标记
    text = re.sub(r'\[DOC\d*\]', '', text)
    text = re.sub(r'\[EXCEL\]', '', text)
    
    # 7. 折叠连续标点为单字符（避免标点差异干扰）
    text = re.sub(r'[,，;；:：!！?？…—–-。]{2,}', lambda m: m.group()[0], text)
    
    return text


def normalize_text_for_comparison(text: str) -> str:
    """标准化文本用于比对：去除空白差异（兼容旧调用）"""
    return re.sub(r'\s+', '', text)


def extract_xxx_regions(text: str) -> List[Tuple[int, int]]:
    """提取文本中所有XXX区域 [(start, end), ...]"""
    regions = []
    i = 0
    text = normalize_text_for_comparison(text)
    while i < len(text):
        if text[i:i+3] == 'XXX':
            regions.append((i, i+3))
            i += 3
        else:
            i += 1
    return regions


def extract_name_like_regions(text: str, window: int = 15) -> List[Dict]:
    """提取文本中所有2-4字连续中文片段（可能是人名）"""
    results = []
    text = normalize_text_for_comparison(text)
    for m in re.finditer(r'[\u4e00-\u9fa5]{2,4}', text):
        start, end = m.start(), m.end()
        context_start = max(0, start - window)
        context_end = min(len(text), end + window)
        results.append({
            'text': m.group(),
            'pos': start,
            'context': text[context_start:context_end],
        })
    return results


# ============================================================================
# 文本标准化层（新增）
# ============================================================================

# 标准版可能使用的各类脱敏标记模式
REDACTION_MARKERS = re.compile(
    r'XXX+|XX银行|XX支行|XX分行|XX部|XX科|XX中心|XX室|XX处|XX组|XX公司|'
    r'XXXX|XXXXX|XXXX|0XX-XXXXXXXX|'
    r'▓{2,}|█{2,}|░{2,}|'
    r'\[REDACTED\]|'
    r'\*{3,}'
)


def extract_xxx_positions(text: str) -> List[int]:
    """提取所有XXX起始位置（处理变长XXX如XX、XXX、XXXX等）"""
    positions = []
    i = 0
    text = normalize_text_for_comparison(text)
    while i < len(text):
        if text[i:i+2] == 'XX' or text[i:i+3] == 'XXX' or text[i:i+4] == 'XXXX':
            # 找到连续X序列
            j = i
            while j < len(text) and text[j] in 'XxXＸx':
                j += 1
            positions.append(i)
            i = j
        else:
            i += 1
    return positions


def extract_context_name_candidates(text: str, pos: int, window: int = 15) -> List[str]:
    """
    在XXX位置周围提取可能的原始姓名。
    
    核心策略：XXX替换的是其前面的连续中文字符（通常是姓名）。
    我们向前查找XXX前面的所有中文片段，取最后2-4个字作为候选。
    """
    candidates = []
    text = normalize_text_for_comparison(text)
    
    # XXX前面最多window个字符
    prefix = text[max(0, pos - window):pos]
    
    # 提取prefix中所有连续中文字符
    chinese_runs = re.findall(r'[\u4e00-\u9fa5]+', prefix)
    if not chinese_runs:
        return []
    
    # 取最后一个中文片段（最接近XXX的）
    last_run = chinese_runs[-1]
    
    # 生成2-4字的候选（从last_run末尾取）
    for length in range(2, min(len(last_run) + 1, 5)):
        # 从末尾向前取length个字符
        candidate = last_run[-length:]
        if candidate:
            candidates.append(candidate)
    
    return candidates


def extract_redacted_entities(texts: List[str], use_comprehensive: bool = True) -> Tuple[set, List[Dict]]:
    """
    从脱敏文本中提取被脱敏的实体集合。
    
    注意：由于无法准确知道XXX替换了什么，此函数主要用于
    辅助分析和调试，实际比对以位置+上下文方法为主。
    
    返回：
        (entity_set, detail_list)
    """
    # 合并文本
    if use_comprehensive:
        full_text = comprehensive_normalize('|||'.join(texts))
    else:
        full_text = normalize_text_for_comparison('|||'.join(texts))
    
    # 提取XXX位置
    xxx_positions = extract_xxx_positions(full_text)
    
    entity_set = set()
    detail_list = []
    
    for pos in xxx_positions:
        candidates = extract_context_name_candidates(full_text, pos, window=12)
        
        for candidate in candidates:
            if not is_generic_term(candidate):
                entity_set.add(candidate)
                detail_list.append({
                    'entity': candidate,
                    'pos': pos,
                    'context': full_text[max(0, pos-10):pos+13],
                })
    
    return entity_set, detail_list


def entity_based_compare(
    our_texts: List[str],
    std_texts: List[str],
    use_comprehensive: bool = True
) -> Dict[str, Any]:
    """
    基于实体的脱敏比对方法（辅助参考）。
    
    由于无法准确从XXX还原原始人名，此方法仅供参考。
    主要指标仍使用位置+上下文比对的结果。
    """
    our_entities, our_details = extract_redacted_entities(our_texts, use_comprehensive)
    std_entities, std_details = extract_redacted_entities(std_texts, use_comprehensive)
    
    common = our_entities & std_entities
    only_ours = our_entities - std_entities
    only_std = std_entities - our_entities
    
    union = our_entities | std_entities
    jaccard = len(common) / len(union) if union else 1.0 if not std_entities else 0.0
    overlap_rate = len(common) / len(std_entities) if std_entities else 1.0 if not our_entities else 0.0
    
    return {
        'our_entities': our_entities,
        'std_entities': std_entities,
        'common_entities': common,
        'only_ours_misdetection': only_ours,
        'only_std_missed': only_std,
        'jaccard_similarity': round(jaccard, 4),
        'overlap_rate': round(overlap_rate, 4),
        'our_entity_count': len(our_entities),
        'std_entity_count': len(std_entities),
        'common_count': len(common),
        'our_details': our_details,
        'std_details': std_details,
    }


def compare_redactions(our_texts: List[str], std_texts: List[str], group_name: str) -> Dict[str, Any]:
    """
    比对脱敏结果与标准版差异。
    
    核心改进：使用 comprehensive_normalize 对文本进行深度标准化，
    消除文档结构差异（页眉/页码/换行/空白）对对比的影响。
    
    保留原有的上下文级比对用于详细差异分析。
    """
    # ---------- 1. 综合文本标准化（核心改进） ----------
    # 使用全面标准化消除文档结构差异
    our_norm = comprehensive_normalize('|||'.join(our_texts))
    std_norm = comprehensive_normalize('|||'.join(std_texts))
    
    # 同时保留旧版标准化用于向后兼容
    our_norm_old = normalize_text_for_comparison('|||'.join(our_texts))
    std_norm_old = normalize_text_for_comparison('|||'.join(std_texts))

    our_regions = extract_xxx_regions(our_norm)
    std_regions = extract_xxx_regions(std_norm)

    # ---------- 2. 上下文级比对 ----------
    def xxx_context_signatures(text: str, regions: List[Tuple[int, int]], window: int = 8) -> Dict[Tuple, int]:
        """每个XXX区域：[前N字符]XXX[后N字符] 的签名"""
        sigs = {}
        for i, (start, end) in enumerate(regions):
            before = text[max(0, start-window):start]
            after = text[end:min(len(text), end+window)]
            sigs[(before, after)] = i
        return sigs

    # 使用综合标准化后的文本来计算上下文签名
    our_sigs = xxx_context_signatures(our_norm, our_regions)
    std_sigs = xxx_context_signatures(std_norm, std_regions)

    # 上下文级重叠计数
    matched_std_indices = set()
    matched_our_indices = set()
    for sig, our_idx in our_sigs.items():
        if sig in std_sigs:
            matched_std_indices.add(std_sigs[sig])
            matched_our_indices.add(our_idx)

    ours_only = [r for i, r in enumerate(our_regions) if i not in matched_our_indices]
    std_only = [r for i, r in enumerate(std_regions) if i not in matched_std_indices]

    漏检 = []
    误检 = []

    # 漏检分析：标准有XXX，我们没有
    for pos, _ in std_only:
        start = max(0, pos - 15)
        end = min(len(std_norm), pos + 18)
        context = std_norm[start:end]
        name_likes = re.findall(r'[\u4e00-\u9fa5]{2,4}', context)
        if name_likes:
            filtered = [n for n in name_likes if not is_generic_term(n)]
            if filtered:
                漏检.append({
                    '位置': pos,
                    '上下文': context,
                    '候选姓名': filtered[:5],
                })

    # 误检分析：我们有XXX，标准没有
    for pos, _ in ours_only:
        start = max(0, pos - 15)
        end = min(len(our_norm), pos + 18)
        context = our_norm[start:end]
        name_likes = re.findall(r'[\u4e00-\u9fa5]{2,4}', context)
        if name_likes:
            filtered = [n for n in name_likes if not is_generic_term(n)]
            if filtered:
                误检.append({
                    '位置': pos,
                    '上下文': context,
                    '候选姓名': filtered[:5],
                })

    # ---------- 3. 整合结果 ----------
    total_std_xxx = len(std_regions)
    total_our_xxx = len(our_regions)
    
    if total_std_xxx > 0:
        overlap = len(matched_std_indices) / total_std_xxx
    else:
        overlap = 1.0 if total_our_xxx == 0 else 0.0

    # 尝试实体比对（作为辅助参考）
    entity_result = entity_based_compare(our_texts, std_texts, use_comprehensive=True)

    return {
        '漏检': 漏检,
        '误检': 误检,
        '可疑': [],
        '统计': {
            # 主指标：基于综合标准化的上下文重叠率
            '标准XXX总数': total_std_xxx,
            '我们XXX总数': total_our_xxx,
            'XXX重叠率': f"{overlap:.1%}",
            '上下文重叠数': len(matched_std_indices),
            '漏检处数': len(漏检),
            '误检处数': len(误检),
            # 辅助指标：实体级比对（参考用）
            '实体重叠率': f"{entity_result['overlap_rate']:.1%}",
            '实体Jaccard': f"{entity_result['jaccard_similarity']:.1%}",
            '标准实体数': entity_result['std_entity_count'],
            '我们实体数': entity_result['our_entity_count'],
            '共同实体数': entity_result['common_count'],
        },
        '_entity_result': {
            k: (list(v) if isinstance(v, set) else v)
            for k, v in entity_result.items()
        },
    }


def is_generic_term(text: str) -> bool:
    """判断是否为通用词汇（不是人名）"""
    generic_terms = {
        # 职务/角色词
        '组长', '副组长', '成员', '经理', '总监', '主任', '工程师', '架构师', '负责人',
        '主持', '汇报', '主讲', '参会', '请假', '缺席', '与会', '列席',
        # 机构/组织词
        '银行', '公司', '系统', '项目', '部门', '小组', '团队', '客户',
        '分行', '支行', '总行', '网点', '营业部', '事业部',
        '营运', '运营', '零售', '对公', '账务', '清算', '核算', '外汇',
        # 业务术语
        '测试', '开发', '设计', '分析', '管理', '协调', '支持',
        '版本', '规划', '方案', '报告', '文档', '材料', '启动', '会议',
        '演练', '切换', '补录', '核对', '审批', '复核', '录入',
        '配置', '风险', '资产', '交易', '核心', '外围', '渠道', '企业', '业务', '功能', '流程',
        '列表', '表格', '图表', '数据', '信息', '时间', '日期', '名称',
        '密码', '账号', '用户', '账户', '交易密码', '登录密码',
        # 序数词
        '第一', '第二', '第三', '第四', '一组', '二组', '三组', '四组',
        '小企业', '计财', '金融', '交易1', '交易2',
        # 复合业务词（从漏检/误检报告中提取）
        '规划管理', '支持协调', '分工安排', '总行支持', '支持人员',
        '内部账', '对公产品', '零售由', '各条线', '金融交易', '系统交易',
        '发账务', '账务组', '外围', '尖刀组', '尖刀组测', '刀组测试',
        '试安排', '尖刀', '试案例', '高新北', '网点', '尾号',
        '其中', '分别', '分别负责', '必须按', '务组要求', '按账',
        '汇总', '汇总全', '汇总全组', '汇总整', '条线分', '各条线分',
        '品由', '内部账产', '填报', '系统交', '新建', '新建新建',
        '营运管理', '部营运管', '运管理', '理部', '批后报表',
        '紧急业务', '补录', '移数据', '数据业', '务检核',
        '参与', '要求', '原则', '允许', '深圳', '高新',
        # 4字通用词（从误检分析中提取）
        '改造改造', '结束时间', '任务XXX', '主要工作', '挡板挡板',
        '福州分行', '福清分行', '测试环境', '零售金融', '信息技术',
        '接入三方', '支行本部', '部零售金', '融部', '部信息技',
        '计划财务', '管理系统', '授信管理', '有限公司', '部授信管',
        '来水', '燃气', '术部', '银行股份', '下线下线',
        '接入三', '行本部', '融事业', '事业部', '资产业', '易系统',
        '帐务处', '账户付', '交易密码', '登录密码',
    }
    return text in generic_terms


# ============================================================================
# 修订方案生成
# ============================================================================

def generate_revision_plan(diff_result: Dict, group_name: str, round_num: int) -> Dict[str, Any]:
    """
    根据差异分析生成脱敏规则修订方案。
    
    支持两种格式：
    - Entity格式（新增）：{'实体': xxx, '上下文': ...}
    - Context格式（旧）：{'候选姓名': [...], '上下文': ...}
    """
    漏检 = diff_result.get('漏检', [])
    误检 = diff_result.get('误检', [])
    stats = diff_result.get('统计', {})

    suggestions = []
    name_additions = set()
    rule_fixes = []

    # 分析漏检：收集可能缺失的姓名（支持新旧两种格式）
    for item in 漏检:
        # 新格式：实体级
        if '实体' in item:
            name = item.get('实体', '')
            if len(name) >= 2:
                name_additions.add(name)
        # 旧格式：上下文级
        elif '候选姓名' in item:
            for name in item.get('候选姓名', []):
                if len(name) >= 2:
                    name_additions.add(name)

    if name_additions:
        suggestions.append({
            'type': 'name_pool补充',
            'detail': f'建议向config.json的name_pool补充以下姓名：{", ".join(sorted(name_additions))}',
            'priority': 'P1',
        })

    # 分析误检模式
    if 误检:
        all_contexts = ' '.join(item.get('上下文', '') for item in 误检)
        if len(误检) >= 3:
            suggestions.append({
                'type': '规则收紧',
                'detail': f'误检{len(误检)}处，可能存在过度匹配，建议检查规则上下文条件',
                'priority': 'P2',
            })

    # 统计判断 - 优先使用实体级指标
    overlap_str = stats.get('实体重叠率', stats.get('XXX重叠率', '0%'))
    overlap_val = float(overlap_str.rstrip('%'))
    
    # 使用实体漏检/误检数（新增字段），兼容旧字段
    漏检_count = stats.get('实体漏检数', stats.get('漏检处数', len(漏检)))
    误检_count = stats.get('实体误检数', stats.get('误检处数', len(误检)))

    if overlap_val >= 0.95 and 漏检_count == 0 and 误检_count == 0:
        status = '已完成'
        suggestions.append({
            'type': '完成',
            'detail': '与标准版基本一致，无改善余地',
            'priority': '-',
        })
    elif overlap_val >= 0.85 and 漏检_count <= 3 and 误检_count <= 3:
        status = '基本达标'
        suggestions.append({
            'type': '微小调整',
            'detail': '差异较小，可继续观察或手动微调',
            'priority': 'P3',
        })
    else:
        status = '需优化'

    return {
        'group': group_name,
        'round': round_num,
        'timestamp': datetime.now().isoformat(),
        '当前状态': status,
        '统计': stats,
        '差异摘要': {
            '漏检': len(漏检),
            '误检': len(误检),
            '实体重叠率': overlap_str,
        },
        '修订建议': suggestions,
        '修订完成': status == '已完成',
    }


# ============================================================================
# 主流程
# ============================================================================

def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except:
            return {}
    return {}


def save_state(state: Dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def ensure_initial_state(state: Dict) -> Dict:
    if 'rounds' not in state:
        state['rounds'] = 0
    if 'group_states' not in state:
        state['group_states'] = {}
    for g in FILE_GROUPS:
        key = g['id']
        if key not in state['group_states']:
            state['group_states'][key] = {'round': 0, 'done': False, 'done_reason': ''}
    return state


def run_single_iteration(state: Dict) -> Tuple[Dict, List[Dict]]:
    """运行一次迭代，处理所有未完成的组"""
    state = ensure_initial_state(state)
    state['rounds'] += 1
    current_round = state['rounds']
    REPORT_DIR.mkdir(exist_ok=True, parents=True)
    OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

    iteration_results = []

    for g in FILE_GROUPS:
        key = g['id']
        gs = state['group_states'][key]

        if gs.get('done'):
            # 已完成，跳过
            iteration_results.append({
                'group': g['name'],
                'id': key,
                'round': gs['round'],
                'status': f"已结束（{gs.get('done_reason', '已达轮次上限')}）",
                'skipped': True,
            })
            continue

        # 执行脱敏
        orig_path = WORK_DIR / g['original']
        std_path = WORK_DIR / g['standard']

        if not orig_path.exists():
            gs['done'] = True
            gs['done_reason'] = f'原文件不存在'
            iteration_results.append({
                'group': g['name'],
                'id': key,
                'status': f'错误：原文件不存在 {orig_path}',
                'error': True,
            })
            continue

        if not std_path.exists():
            gs['done'] = True
            gs['done_reason'] = f'标准文件不存在'
            iteration_results.append({
                'group': g['name'],
                'id': key,
                'status': f'错误：标准文件不存在 {std_path}',
                'error': True,
            })
            continue

        # 更新轮次
        gs['round'] += 1
        group_round = gs['round']

        # 运行脱敏管道
        redacted_path = OUTPUT_DIR / f"{orig_path.stem}_脱敏v{group_round}{orig_path.suffix}"
        success, error = run_redaction(orig_path, redacted_path, g['ext'])

        if not success:
            iteration_results.append({
                'group': g['name'],
                'id': key,
                'round': group_round,
                'status': f'脱敏失败: {error[:100]}',
                'error': True,
            })
            continue

        # 提取脱敏后文本
        our_texts = extract_all_texts(redacted_path, g['ext'])
        std_texts = extract_all_texts(std_path, g['ext'])

        # 比对
        diff = compare_redactions(our_texts, std_texts, g['name'])

        # 生成修订方案
        revision = generate_revision_plan(diff, g['name'], group_round)

        # 判断是否完成
        if revision['修订完成']:
            gs['done'] = True
            gs['done_reason'] = '与标准版一致'
            revision['status'] = '完成'
        elif group_round >= 5:
            # 保险：最多5轮
            gs['done'] = True
            gs['done_reason'] = '达到最大轮次'
            revision['status'] = '完成（达到最大轮次）'
        else:
            revision['status'] = f'第{group_round}轮，漏检{len(diff.get("漏检", []))}处，误检{len(diff.get("误检", []))}处'

        # 保存报告
        report = {
            'group': g['name'],
            'id': key,
            'round': group_round,
            'timestamp': datetime.now().isoformat(),
            '文件': {
                '原文件': str(orig_path),
                '标准文件': str(std_path),
                '脱敏输出': str(redacted_path),
            },
            '差异': diff,
            '修订方案': revision,
        }

        report_file = REPORT_DIR / f"round{current_round}_{key}_report.json"
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

        iteration_results.append({
            'group': g['name'],
            'id': key,
            'round': group_round,
            'status': revision['status'],
            '修订完成': revision.get('修订完成', False),
            'diff_stats': diff.get('统计', {}),
            'report_file': str(report_file),
        })

    # 判断全部完成
    all_done = all(gs.get('done') for gs in state['group_states'].values())
    state['all_completed'] = all_done
    state['last_run'] = datetime.now().isoformat()

    save_state(state)

    # 生成汇总
    summary = {
        'round': current_round,
        'timestamp': datetime.now().isoformat(),
        'all_completed': all_done,
        'group_states': {
            key: {
                'round': gs['round'],
                'done': gs['done'],
                'done_reason': gs.get('done_reason', ''),
            }
            for key, gs in state['group_states'].items()
        },
        'results': iteration_results,
    }

    summary_file = REPORT_DIR / f"summary_round{current_round}.json"
    summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    return summary, iteration_results


def print_summary(summary: Dict, results: List[Dict]):
    """打印汇总报告"""
    print(f"\n{'='*70}")
    print(f"第 {summary['round']} 轮迭代报告 - {summary['timestamp']}")
    print(f"{'='*70}")

    for r in results:
        skipped = r.get('skipped', False)
        print(f"\n【{r['group']}】{'（已跳过）' if skipped else ''} {r.get('status', '')}")
        if not skipped:
            stats = r.get('diff_stats', {})
            if stats:
                # 显示实体级指标（主）和上下文级指标（参考）
                entity_overlap = stats.get('实体重叠率', '?')
                context_overlap = stats.get('上下文重叠率', stats.get('XXX重叠率', '?'))
                print(f"  实体重叠率: {entity_overlap} | 上下文重叠率: {context_overlap}")
                print(f"  标准实体: {stats.get('标准实体数', '?')} | 我们实体: {stats.get('我们实体数', '?')} | 共同: {stats.get('共同实体数', '?')}")
                print(f"  实体漏检: {stats.get('实体漏检数', '?')} | 实体误检: {stats.get('实体误检数', '?')}")

    print(f"\n{'─'*70}")
    print("各组状态:")
    for gid, gs in summary['group_states'].items():
        done_mark = '✓' if gs['done'] else '○'
        print(f"  {done_mark} [{gid}] 轮次:{gs['round']} {gs.get('done_reason', '')}")

    if summary['all_completed']:
        print(f"\n{'='*70}")
        print("✓ 全部文件已完成迭代优化，无更多改善余地")
        print(f"{'='*70}")


# ============================================================================
# 自动化任务入口
# ============================================================================

def main():
    print(f"[{datetime.now().isoformat()}] 脱敏迭代任务开始")

    state = load_state()
    state = ensure_initial_state(state)

    if state.get('all_completed'):
        print("所有文件已完成迭代，任务结束。")
        print(f"如需重新开始，请删除状态文件: {STATE_FILE}")
        return

    summary, results = run_single_iteration(state)
    print_summary(summary, results)

    # 保存最新报告路径
    latest = REPORT_DIR / "latest_summary.json"
    latest.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f"\n报告已保存至: {REPORT_DIR}")
    print(f"状态文件: {STATE_FILE}")
    print(f"下次运行{'（已无下次）' if summary['all_completed'] else '将继续未完成的组'}")

    # 清理临时文件
    try:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    except:
        pass


if __name__ == '__main__':
    main()
