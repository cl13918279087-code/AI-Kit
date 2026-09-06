#!/usr/bin/env python3
"""
脱敏迭代比对分析脚本
对原文件进行脱敏，与标准脱敏文件比对，分析差异并优化规则。
每次运行处理一组文件，多组文件分批处理。
"""

import sys
import os
import json
import re
import zipfile
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common_rules import reset_patterns, apply_redactions, get_config

WORK_DIR = Path("/Users/clzxr/WorkBuddy/Claw/工作目录")

# 六组文件配置
FILE_GROUPS = [
    {
        "name": "郑州银行新核心项目启动会材料",
        "original": "郑州银行新核心项目_启动会材料V0.6-20170308.pptx",
        "standard": "XX银行新核心项目_启动会材料V0.6-20170308_脱敏标准版.pptx",
        "ext": "pptx",
    },
    {
        "name": "郑州银行新一代信息系统差异分析启动会",
        "original": "郑州银行新一代信息系统建设项目文档_差异分析启动会.docx",
        "standard": "XX银行新一代信息系统建设项目文档_差异分析启动会_脱敏标准版.docx",
        "ext": "docx",
    },
    {
        "name": "福建海峡银行业务演练切换操作指南",
        "original": "福建海峡银行新核心项目第四轮业务演练切换操作指南V0.2.doc",
        "standard": "XX银行新核心项目第四轮业务演练切换操作指南V0.2_脱敏标准版.doc",
        "ext": "doc",
    },
    {
        "name": "PMO-QA人员分工表",
        "original": "PMO，QA组人员分工表-20171120.xlsx",
        "standard": "PMO，QA组人员分工表-20171120_脱敏标准版.xlsx",
        "ext": "xlsx",
    },
    {
        "name": "尖刀测试内容",
        "original": "尖刀测试内容.xlsx",
        "standard": "尖刀测试内容_脱敏标准版.xlsx",
        "ext": "xlsx",
    },
    {
        "name": "尖刀组UAT3测试规划",
        "original": "尖刀组UAT3测试规划0907.docx",
        "standard": "尖刀组UAT3测试规划0907_脱敏标准版.docx",
        "ext": "docx",
    },
]

STATE_FILE = WORK_DIR / ".redact_iter_state.json"
REPORT_DIR = WORK_DIR / ".redact_reports"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def extract_pptx_text(path):
    """从PPTX提取所有文本"""
    texts = []
    try:
        with zipfile.ZipFile(path, 'r') as z:
            for name in z.namelist():
                if name.startswith('ppt/slides/slide') and name.endswith('.xml'):
                    content = z.read(name).decode('utf-8')
                    texts.append(extract_text_from_xml(content))
    except Exception as e:
        return [f"[PPTX读取错误: {e}]"]
    return texts


def extract_docx_text(path):
    """从DOCX提取所有文本"""
    texts = []
    try:
        with zipfile.ZipFile(path, 'r') as z:
            for name in z.namelist():
                if name.startswith('word/document'):
                    content = z.read(name).decode('utf-8')
                    texts.append(extract_text_from_xml(content))
    except Exception as e:
        return [f"[DOCX读取错误: {e}]"]
    return texts


def extract_text_from_xml(xml_content):
    """从XML提取文本内容（优先标签文本，避免普通文本节点导致重复提取）"""
    # 提取 <w:t>（Word）或 <a:t>（PPTX）标签文本
    texts = re.findall(r'<(?:w|a):t[^>]*>([^<]*)</(?:w|a):t>', xml_content)
    if not texts:
        # 兜底：提取普通文本节点
        texts = re.findall(r'>([^<]+)<', xml_content)
    combined = ' '.join(texts)
    # 清理多余空白
    combined = re.sub(r'\s+', ' ', combined).strip()
    return combined


def extract_xlsx_text(path):
    """从XLSX提取所有文本"""
    texts = []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_texts = []
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is not None:
                        sheet_texts.append(str(cell.value))
            if sheet_texts:
                texts.append(' '.join(sheet_texts))
    except Exception as e:
        return [f"[XLSX读取错误: {e}]"]
    return texts


def _text_quality_ok(text, min_cjk_ratio=0.15):
    """校验提取文本质量：中文占比过低说明是二进制刮取垃圾"""
    if not text:
        return False
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    return cjk / len(text) >= min_cjk_ratio


def extract_doc_text(path):
    """从DOC提取文本（OOXML伪装.doc自动走docx通道；真OLE2用textutil兜底）"""
    try:
        with open(path, 'rb') as f:
            head = f.read(4)
            f.seek(0)
            raw = f.read()
        if head[:2] == b'PK':
            # 实为 OOXML（docx 换壳 .doc），用 docx 通道提取
            return extract_docx_text(path)
        # 真 OLE2 .doc：优先 textutil（macOS），次选 LibreOffice，失败再走二进制刮取
        import subprocess
        try:
            r = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and len(r.stdout.strip()) > 50:
                return [re.sub(r'\s+', ' ', r.stdout).strip()]
        except Exception:
            pass
        # LibreOffice 转纯文本（中文 .doc 兼容性好）
        try:
            with tempfile.TemporaryDirectory() as td:
                r = subprocess.run(
                    ["soffice", "--headless", "--convert-to", "txt:Text",
                     "--outdir", td, str(path)],
                    capture_output=True, text=True, timeout=300)
                txt_files = list(Path(td).glob('*.txt'))
                if r.returncode == 0 and txt_files:
                    content = txt_files[0].read_text(errors='replace')
                    content = re.sub(r'\s+', ' ', content).strip()
                    if len(content) > 50 and _text_quality_ok(content):
                        return [content]
        except Exception:
            pass
        # 尝试 UTF-16 LE
        try:
            text = raw.decode('utf-16-le', errors='strict')
            if '\x00' in text[:100]:
                # 确实是UTF-16
                text = ''.join(c for c in text if c.isprintable() or c in '\n\r\t ')
                text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\n\r\t ]+', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                if len(text) > 50:
                    return [text]
        except:
            pass
        # 回退：提取所有可见ASCII+中文
        text = ''.join(c for c in raw.decode('latin-1', errors='replace') if c.isprintable())
        text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\n\r\t ]+', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 100:
            return [text]
        return [f"[DOC文本提取受限，长度={len(text)}]"]
    except Exception as e:
        return [f"[DOC读取错误: {e}]"]


def extract_all_text(file_path, ext):
    """提取文件所有文本"""
    if ext == 'pptx':
        return extract_pptx_text(file_path)
    elif ext == 'docx':
        return extract_docx_text(file_path)
    elif ext == 'xlsx':
        return extract_xlsx_text(file_path)
    elif ext == 'doc':
        return extract_doc_text(file_path)
    return []


def redact_text(text):
    """对文本执行脱敏"""
    if not text:
        return text
    return apply_redactions(text)


def _extract_markers(norm_text, radius=15):
    """提取所有脱敏标记（连续X串，长度>=2）及其上下文。

    上下文以文本内容为键（前缀/后缀），与绝对位置无关——
    文本头部的增删不会导致全量错位，从根本上消除对齐噪声。
    """
    markers = []
    for m in re.finditer(r'X{2,}', norm_text):
        before = norm_text[max(0, m.start()-radius):m.start()]
        after = norm_text[m.end():m.end()+radius]
        markers.append({'marker': m.group(0), 'before': before, 'after': after})
    return markers


def compare_redactions(our_text, standard_text, group_name):
    """
    比对我们的脱敏结果与标准版差异（上下文指纹对齐版）

    对齐策略：
    1. 每个脱敏点提取 (前缀, 后缀) 上下文指纹；
    2. 按指纹精确匹配（含重复次数配对）；
    3. 剩余未匹配项用 rapidfuzz 模糊对齐（容忍邻近微小编輯）；
    4. 仍无法对齐的才判定为真实差异。

    返回：{'漏检': [...], '误检': [...], '统计': {...}}
    漏检：标准版有脱敏点，我们没有
    误检：我们有脱敏点，标准版没有
    """
    from collections import Counter
    from rapidfuzz import fuzz

    results = {
        '漏检': [],      # 标准版有脱敏点，我们没有
        '误检': [],      # 我们有脱敏点，标准版没有（可能是我们误检）
        '统计': {},
    }

    # 标准化：合并所有空白，去除BOM等零宽字符
    our_norm = re.sub(r'[\s\ufeff]+', '', our_text)
    std_norm = re.sub(r'[\s\ufeff]+', '', standard_text)

    our_markers = _extract_markers(our_norm)
    std_markers = _extract_markers(std_norm)

    # 第一轮：上下文指纹精确匹配（按出现次数配对）
    our_counter = Counter((m['before'], m['after']) for m in our_markers)
    std_counter = Counter((m['before'], m['after']) for m in std_markers)

    leftover_our_keys = Counter()
    for key, cnt in our_counter.items():
        matched = min(cnt, std_counter.get(key, 0))
        if cnt > matched:
            leftover_our_keys[key] = cnt - matched
    leftover_std_keys = Counter()
    for key, cnt in std_counter.items():
        matched = min(cnt, our_counter.get(key, 0))
        if cnt > matched:
            leftover_std_keys[key] = cnt - matched

    # 第二轮：模糊对齐剩余项（容忍上下文±1-2字的微小编辑差异）
    fuzzy_matched_std = Counter()
    final_our = Counter()
    for okey, ocnt in leftover_our_keys.items():
        remaining = ocnt
        # 按相似度从高到低尝试匹配标准版剩余指纹
        candidates = sorted(
            ((fuzz.ratio(okey[0], skey[0]) * 0.5 +
              fuzz.ratio(okey[1], skey[1]) * 0.5, skey)
             for skey in leftover_std_keys
             if leftover_std_keys[skey] - fuzzy_matched_std.get(skey, 0) > 0),
            key=lambda x: -x[0])
        for score, skey in candidates:
            if remaining <= 0:
                break
            if score < 80:
                break
            avail = leftover_std_keys[skey] - fuzzy_matched_std.get(skey, 0)
            take = min(remaining, avail)
            fuzzy_matched_std[skey] += take
            remaining -= take
        if remaining > 0:
            final_our[okey] = remaining

    final_std = {
        key: cnt - fuzzy_matched_std.get(key, 0)
        for key, cnt in leftover_std_keys.items()
        if cnt - fuzzy_matched_std.get(key, 0) > 0
    }

    # 生成差异报告
    for (before, after), cnt in final_std.items():
        context = f"...{before}█{after}..."
        results['漏检'].append({
            '上下文': context,
            '出现次数': cnt,
            '可能的姓名': re.findall(r'[\u4e00-\u9fa5]{2,4}', after[:6]),
        })
    for (before, after), cnt in final_our.items():
        context = f"...{before}█{after}..."
        results['误检'].append({
            '上下文': context,
            '出现次数': cnt,
            '可能的姓名': re.findall(r'[\u4e00-\u9fa5]{2,4}', after[:6]),
        })

    aligned = min(len(our_markers), len(std_markers)) - \
        sum(final_our.values()) - sum(final_std.values())
    results['统计'] = {
        '标准版脱敏点': len(std_markers),
        '我们脱敏点': len(our_markers),
        '对齐成功': max(0, aligned),
    }

    return results


def build_name_set_from_pool():
    """从name_pool构建姓氏和名字集合"""
    cfg = get_config()
    surname_set = set(cfg.get('surname_pool', ''))
    name_pool = set(cfg.get('name_pool', ''))
    compound = set(cfg.get('compound_surnames', []))
    return surname_set, name_pool, compound


def find_chinese_names_in_text(text):
    """从文本中提取所有可能是人名的2-4字连续中文"""
    surname_set, name_pool, compound = build_name_set()
    found = []

    # 检查复姓
    for comp in compound:
        for m in re.finditer(re.escape(comp), text):
            pos = m.start()
            # 复姓后跟1-2个名字字符
            rest = text[pos+len(comp):]
            name_chars = ''.join(re.findall(r'[\u4e00-\u9fa5]', rest[:4]))
            if len(name_chars) >= 1:
                # 取1-2个名字字符
                for n in range(1, min(3, len(name_chars)+1)):
                    full_name = comp + name_chars[:n]
                    found.append((pos, full_name))

    # 检查单姓 + 1-2个名字
    for m in re.finditer(r'[\u4e00-\u9fa5]', text):
        ch = m.group(0)
        if ch in surname_set:
            pos = m.start()
            rest = text[pos+1:]
            name_chars = ''.join(re.findall(r'[\u4e00-\u9fa5]', rest[:4]))
            for n in range(1, min(3, len(name_chars)+1)):
                full_name = ch + name_chars[:n]
                # 简单验证：第二字开始在name_pool或常见名字
                if n == 1 or (n >= 2 and name_chars[0] in name_pool):
                    found.append((pos, full_name))

    return found


def analyze_file_group(group, round_num):
    """分析单个文件组"""
    orig_path = WORK_DIR / group['original']
    std_path = WORK_DIR / group['standard']

    if not orig_path.exists():
        return {'error': f'原文件不存在: {orig_path}'}
    if not std_path.exists():
        return {'error': f'标准文件不存在: {std_path}'}

    # 提取标准版文本
    std_texts = extract_all_text(std_path, group['ext'])
    std_full = '\n'.join(std_texts)

    # 对原文件脱敏
    orig_texts = extract_all_text(orig_path, group['ext'])
    our_redacted = [redact_text(t) for t in orig_texts]
    our_full = '\n'.join(our_redacted)

    # 比对
    comparison = compare_redactions(our_full, std_full, group['name'])

    # 详细差异报告
    report = {
        'group': group['name'],
        'round': round_num,
        'timestamp': datetime.now().isoformat(),
        '文件': {
            '原文件': group['original'],
            '标准文件': group['standard'],
        },
        '差异': comparison,
        '文本长度': {
            '我们脱敏后': len(our_full),
            '标准脱敏后': len(std_full),
        },
    }

    return report


def run_iteration():
    """运行一次完整迭代"""
    reset_patterns()
    REPORT_DIR.mkdir(exist_ok=True)

    state = load_state()
    if 'rounds' not in state:
        state['rounds'] = 0
        state['group_states'] = {}

    state['rounds'] += 1
    current_round = state['rounds']

    # 初始化各组状态
    for g in FILE_GROUPS:
        key = g['name']
        if key not in state['group_states']:
            state['group_states'][key] = {'round': 0, 'done': False}

    results = []

    for i, g in enumerate(FILE_GROUPS):
        key = g['name']
        gs = state['group_states'][key]

        # 跳过已完成的组（达到3轮且无改善）
        if gs.get('done'):
            continue

        # 更新组轮次
        gs['round'] += 1
        group_round = gs['round']

        # 执行分析
        report = analyze_file_group(g, group_round)
        report['group_index'] = i + 1
        results.append(report)

        # 检查是否已无改善余地
        diff = report.get('差异', {})
        漏检_count = len(diff.get('漏检', []))
        误检_count = len(diff.get('误检', []))

        if group_round >= 3 and 漏检_count == 0 and 误检_count == 0:
            gs['done'] = True
            report['status'] = '完成（无差异）'
        elif group_round >= 5:  # 保险：最多5轮
            gs['done'] = True
            report['status'] = '完成（达到最大轮次）'
        else:
            report['status'] = f'需继续（第{group_round}轮，漏检{漏检_count}处，误检{误检_count}处）'

        # 保存报告
        report_file = REPORT_DIR / f"round{current_round}_group{i+1}_{g['name'][:10]}.json"
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    # 检查是否全部完成
    all_done = all(gs.get('done') for gs in state['group_states'].values())
    if all_done:
        state['completed'] = True

    save_state(state)

    # 生成汇总报告
    summary = {
        'round': current_round,
        'timestamp': datetime.now().isoformat(),
        'all_completed': all_done,
        'group_states': state['group_states'],
        'results': [
            {
                'group': r['group'],
                'status': r.get('status', ''),
                '漏检': len(r.get('差异', {}).get('漏检', [])),
                '误检': len(r.get('差异', {}).get('误检', [])),
            }
            for r in results
        ]
    }

    summary_file = REPORT_DIR / f"summary_round{current_round}.json"
    summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    return summary, results


def print_report(summary, results):
    """打印报告到标准输出"""
    print(f"\n{'='*60}")
    print(f"第 {summary['round']} 轮迭代报告 - {summary['timestamp']}")
    print(f"{'='*60}")

    for r in results:
        print(f"\n【{r['group']}】{r.get('status', '')}")
        diff = r.get('差异', {})
        if diff.get('漏检'):
            print(f"  漏检 ({len(diff['漏检'])}处):")
            for item in diff['漏检'][:5]:
                print(f"    - 上下文: ...{item['上下文']}...")
                print(f"      可能的姓名: {item['可能的姓名']}")
            if len(diff['漏检']) > 5:
                print(f"    ... 还有 {len(diff['漏检'])-5} 处")

        if diff.get('误检'):
            print(f"  误检 ({len(diff['误检'])}处):")
            for item in diff['误检'][:5]:
                print(f"    - 上下文: ...{item['上下文']}...")
                print(f"      可能的姓名: {item['可能的姓名']}")
            if len(diff['误检']) > 5:
                print(f"    ... 还有 {len(diff['误检'])-5} 处")

        if not diff.get('漏检') and not diff.get('误检'):
            print(f"  ✓ 与标准版一致")

    # 打印汇总
    print(f"\n汇总:")
    for s in summary['results']:
        print(f"  [{s['group']}] {s['status']}")

    if summary['all_completed']:
        print(f"\n{'='*60}")
        print("全部文件已完成迭代优化，无更多改善余地")
        print(f"{'='*60}")


if __name__ == '__main__':
    summary, results = run_iteration()
    print_report(summary, results)
