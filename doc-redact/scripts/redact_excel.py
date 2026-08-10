#!/usr/bin/env python3
"""
redact_excel.py - Excel 电子表格脱敏脚本
支持 .xlsx / .xls 格式
依赖: openpyxl
安装: pip install openpyxl
"""

import sys
import re
import zipfile
import shutil
from pathlib import Path

# ---------------------------------------------------------------------------
# 脱敏规则
# ---------------------------------------------------------------------------
REPLACEMENTS = [
    (re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'), 'XXXXX@XXXXX'),
    (re.compile(
        r'[^\x00-\xFF]{2,6}(?:省|自治区|市)?[^\x00-\xFF]{0,10}'
        r'(?:市|区)?[^\x00-\xFF]{0,10}'
        r'(?:街|路|道|巷|弄|号|大道|大街|东路|西路|南路|北路)[^\x00-\xFF]{0,30}'
    ), 'XX省XX市XX区XXXX'),
    (re.compile(r'[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]'),
     'XXXXXXXXXXXXXXXXXX'),
    (re.compile(r'\b(?:\d{16}|\d{17}|\d{18}|\d{19})\b'), 'XXXXXXXXXXXXXXXX'),
    (re.compile(
        r'\d{4}[-年](?:0[1-9]|1[0-2])[-月](?:0[1-9]|[12]\d|3[01])[日]?\s*'
        r'|(?:19|20)\d{2}年\d{1,2}月\d{1,2}日'
    ), 'YYYY/MM/DD'),
    (re.compile(r'\b1[3-9]\d{9}\b'), 'XXXXXXXXXXX'),
    (re.compile(r'0\d{2,3}[-\s]?\d{7,8}'), '0XX-XXXXXXXX'),
    (re.compile(
        r'(?:(?:中国|交通|招商|浦发|兴业|民生|华夏|平安|光大|广发|浙商|渤海|恒丰|'
        r'农业|建设|工商|南京|宁波|杭州|深圳|上海|北京|广州)银行|'
        r'(?:农信社|信用社|农商银行|合作银行|人民银行))'
    ), '[某银行]'),
    (re.compile(r'[\u4e00-\u9fa5]{2,4}(?![a-zA-Z0-9\u4e00-\u9fa5])'), 'XXX'),
]


def apply_redactions(text: str) -> str:
    if not text:
        return text
    result = text
    for pattern, replacement in REPLACEMENTS:
        result = pattern.sub(replacement, result)
    return result


def redact_xlsx(input_path: str, output_path: str = None) -> str:
    """处理 .xlsx 文件（直接操作 XML）"""
    if output_path is None:
        stem = Path(input_path).stem
        output_path = str(Path(input_path).with_name(f"{stem}_脱敏.xlsx"))

    tmp_dir = Path('/tmp/redact_xlsx_tmp')
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    with zipfile.ZipFile(input_path, 'r') as zf:
        zf.extractall(tmp_dir)

    # 处理共享字符串表（最常见文本存储位置）
    shared_strings = tmp_dir / 'xl' / 'sharedStrings.xml'
    if shared_strings.exists():
        try:
            content = shared_strings.read_text('utf-8')
            original = content
            content = apply_redactions(content)
            if content != original:
                shared_strings.write_text(content, 'utf-8')
                print(f"  [处理] sharedStrings.xml 已更新")
        except Exception as e:
            print(f"  [警告] 处理 sharedStrings.xml 时出错: {e}", file=sys.stderr)

    # 处理工作表 XML（单元格内联文本）
    worksheets = list((tmp_dir / 'xl' / 'worksheets').glob('sheet*.xml'))
    for ws in worksheets:
        try:
            content = ws.read_text('utf-8')
            original = content
            content = apply_redactions(content)
            if content != original:
                ws.write_text(content, 'utf-8')
        except Exception as e:
            print(f"  [警告] 处理 {ws.name} 时出错: {e}", file=sys.stderr)

    # 处理批注
    comments = list((tmp_dir / 'xl').glob('comments*.xml'))
    for cm in comments:
        try:
            content = cm.read_text('utf-8')
            content = apply_redactions(content)
            cm.write_text(content, 'utf-8')
        except Exception:
            pass

    # 处理文档属性
    core_props = tmp_dir / 'docProps' / 'core.xml'
    if core_props.exists():
        try:
            content = core_props.read_text('utf-8')
            content = apply_redactions(content)
            core_props.write_text(content, 'utf-8')
        except Exception:
            pass

    # 重新打包
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file_path in tmp_dir.rglob('*'):
            if file_path.is_file():
                arcname = file_path.relative_to(tmp_dir)
                zf.write(file_path, arcname)

    shutil.rmtree(tmp_dir)
    print(f"[完成] 脱敏文档已保存至: {output_path}")
    return output_path


def main():
    if len(sys.argv) < 2:
        print("用法: python3 redact_excel.py <输入文件.xlsx> [输出文件路径]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    ext = Path(input_file).suffix.lower()
    if ext in ('.xlsx', '.xls'):
        redact_xlsx(input_file, output_file)
    else:
        print(f"[错误] 不支持的文件格式: {ext}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
