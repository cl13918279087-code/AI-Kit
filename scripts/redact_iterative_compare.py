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

# 五组文件配置
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
    """从XML提取文本内容"""
    # 提取<w:t>标签中的文本
    texts = re.findall(r'<w:t[^>]*>([^<]*)</w:t>', xml_content)
    # 提取普通文本节点
    texts2 = re.findall(r'>([^<]+)<', xml_content)
    combined = ' '.join(texts + texts2)
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


def extract_doc_text(path):
    """从DOC提取文本（基础方法，尝试多种编码）"""
    try:
        # 尝试直接读取二进制，提取可见字符
        with open(path, 'rb') as f:
            raw = f.read()
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


def compare_redactions(our_text, standard_text, group_name):
    """
    比对我们的脱敏结果与标准版差异
    返回：{
        '漏检': [未脱敏的人名],
        '误检': [被错误脱敏的内容],
        '其他差异': []
    }
    """
    results = {
        '漏检': [],      # 标准版有XXX，我们没有
        '误检': [],      # 我们有XXX，标准版没有（可能是我们误检）
        '可疑': [],      # 脱敏标记位置不一致
    }

    # 标准化：合并所有空白
    our_norm = re.sub(r'\s+', '', our_text)
    std_norm = re.sub(r'\s+', '', standard_text)

    # 提取所有 XXX 序列（我们的结果）
    our_xxx_positions = set()
    i = 0
    while i < len(our_norm):
        if our_norm[i:i+3] == 'XXX':
            our_xxx_positions.add(i)
            i += 3
        else:
            i += 1

    # 提取所有 XXX 序列（标准版）
    std_xxx_positions = set()
    i = 0
    while i < len(std_norm):
        if std_norm[i:i+3] == 'XXX':
            std_xxx_positions.add(i)
            i += 3
        else:
            i += 1

    # 找出差异区域
    # 漏检：标准版有XXX，我们没有（在标准版XXX范围内，检测是否有人名）
    for pos in std_xxx_positions:
        if pos not in our_xxx_positions:
            # 检查周围是否有中文人名模式
            start = max(0, pos - 10)
            end = min(len(std_norm), pos + 13)
            context = std_norm[start:end]
            # 检查是否有人名姓氏模式
            names = re.findall(r'[\u4e00-\u9fa5]{2,4}', context)
            if names:
                results['漏检'].append({
                    '位置': pos,
                    '上下文': context,
                    '可能的姓名': names
                })

    # 误检：我们有XXX，标准版没有
    for pos in our_xxx_positions:
        if pos not in std_xxx_positions:
            start = max(0, pos - 10)
            end = min(len(our_norm), pos + 13)
            context = our_norm[start:end]
            names = re.findall(r'[\u4e00-\u9fa5]{2,4}', context)
            if names:
                results['误检'].append({
                    '位置': pos,
                    '上下文': context,
                    '可能的姓名': names
                })

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
