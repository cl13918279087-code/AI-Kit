#!/usr/bin/env python3
"""
redact_excel.py - Excel 电子表格脱敏脚本
支持 .xlsx（OOXML）和 .xls（Excel 97-2003 二进制格式）

依赖: openpyxl（.xlsx）、xlrd+xlwt（.xls）、olefile（格式检测）
安装: pip install openpyxl xlrd xlwt olefile
"""

import sys
import re
import zipfile
import shutil
import tempfile
from pathlib import Path

from common_rules import apply_redactions, REDACTION_LABELS

# ---------------------------------------------------------------------------
# .xlsx 处理（OOXML / ZIP 格式）
# ---------------------------------------------------------------------------

def redact_xlsx(input_path: str, output_path: str) -> dict:
    """处理 .xlsx 文件（直接操作 XML，保持格式不变）"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="redact_xlsx_"))
    counts = {}

    try:
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(tmp_dir)

        # ① 共享字符串表（Excel 最常用文本存储位置）
        shared = tmp_dir / "xl" / "sharedStrings.xml"
        if shared.exists():
            counts.update(_process_xml_file(shared, "共享字符串"))

        # ② 工作表 XML（内联文本）
        ws_dir = tmp_dir / "xl" / "worksheets"
        if ws_dir.exists():
            for ws in sorted(ws_dir.glob("sheet*.xml")):
                c = _process_xml_file(ws, ws.name)
                _merge_counts(counts, c)

        # ③ 批注
        for cm in sorted(tmp_dir.glob("xl/comments*.xml")):
            _process_xml_file(cm, f"批注 {cm.name}")

        # ④ 页眉页脚
        for hf in sorted(tmp_dir.rglob("header*.xml")):
            _process_xml_file(hf, f"页眉 {hf.name}")
        for hf in sorted(tmp_dir.rglob("footer*.xml")):
            _process_xml_file(hf, f"页脚 {hf.name}")

        # ⑤ 文档属性
        core = tmp_dir / "docProps" / "core.xml"
        if core.exists():
            _process_xml_file(core, "文档属性")

        # 重新打包（保持 ZIP 压缩级别）
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(tmp_dir.rglob("*")):
                if fp.is_file():
                    arcname = str(fp.relative_to(tmp_dir))
                    zf.write(fp, arcname)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return counts


def _process_xml_file(path: Path, label: str = "") -> dict:
    """读取 XML 文件 → 执行脱敏 → 写回（仅在有变化时）"""
    try:
        content = path.read_text("utf-8")
        original = content
        redacted = apply_redactions(content)
        if redacted != original:
            path.write_text(redacted, "utf-8")
            print(f"  [更新] {label or path.name}")
    except Exception as e:
        print(f"  [警告] 处理 {label or path.name} 出错: {e}", file=sys.stderr)
    return {}


def _merge_counts(base: dict, new: dict) -> None:
    for k, v in new.items():
        base[k] = base.get(k, 0) + v


# ---------------------------------------------------------------------------
# .xls 处理（Excel 97-2003 二进制格式）
# ---------------------------------------------------------------------------

def redact_xls(input_path: str, output_path: str) -> dict:
    """
    处理 .xls 文件（BIFF8/Biff5 二进制格式）。
    使用 xlrd 读取内容，xlwt 写入（公式结果被脱敏，公式本身保留）。
    注意：.xls 二进制格式不支持保留宏/图表，仅处理单元格文本和批注。
    """
    import xlrd
    import xlwt

    counts = {}

    try:
        book = xlrd.open_workbook(input_path, formatting_info=False)
    except Exception as e:
        print(f"  [警告] xlrd 无法打开 .xls 文件: {e}，尝试直接复制原文件（跳过内容处理）")
        shutil.copy2(input_path, output_path)
        return counts

    wb_new = xlwt.Workbook(encoding="utf-8")
    style_base = xlwt.XFStyle()

    for sheet_idx in range(book.nsheets):
        sheet = book.sheet_by_index(sheet_idx)
        ws_new = wb_new.add_sheet(sheet.name, cell_overwrite_ok=True)

        for row_idx in range(sheet.nrows):
            for col_idx in range(sheet.ncols):
                cell = sheet.cell(row_idx, col_idx)
                ctype, value, _ = cell

                if ctype == xlrd.XL_CELL_TEXT:
                    redacted = apply_redactions(value)
                    ws_new.write(row_idx, col_idx, redacted, style_base)
                    if redacted != value:
                        _count_type(counts, value)
                elif ctype == xlrd.XL_CELL_DATE:
                    # 日期以序列值存储，脱敏为占位符
                    ws_new.write(row_idx, col_idx, "YYYY/MM/DD", style_base)
                    _count_type(counts, "日期")
                else:
                    # 其他类型（数字、布尔等）保留原值
                    try:
                        ws_new.write(row_idx, col_idx, value, style_base)
                    except Exception:
                        ws_new.write(row_idx, col_idx, str(value), style_base)

        # 处理批注
        try:
            if hasattr(sheet, "cell_comments"):
                for (row, col), comment in sheet.cell_comments.items():
                    if comment and comment.text:
                        redacted = apply_redactions(comment.text)
                        if redacted != comment.text:
                            _count_type(counts, "批注")
        except Exception:
            pass

    wb_new.save(output_path)
    return counts


def _count_type(counts: dict, label: str) -> None:
    counts[label] = counts.get(label, 0) + 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def redact_excel(input_path: str, output_path: str = None) -> dict:
    """
    统一入口，自动根据扩展名分发到 xlsx 或 xls 处理函数。
    返回各类脱敏统计。
    """
    if output_path is None:
        stem = Path(input_path).stem
        output_path = str(Path(input_path).with_name(f"{stem}_脱敏.xlsx"))

    ext = Path(input_path).suffix.lower()
    counts = {}

    if ext == ".xlsx":
        counts = redact_xlsx(input_path, output_path)
    elif ext == ".xls":
        # .xls 输出仍保存为 .xls（xlwt 不支持 .xlsx 输出）
        xls_output = str(Path(output_path).with_suffix(".xls"))
        counts = redact_xls(input_path, xls_output)
        # 如果用户指定了其他扩展名，复制一份
        if xls_output != output_path:
            shutil.copy2(xls_output, output_path)
    else:
        print(f"[错误] 不支持的文件格式: {ext}（仅支持 .xlsx 和 .xls）", file=sys.stderr)
        sys.exit(1)

    total = sum(counts.values())
    print(f"[完成] 共遮盖 {total} 处敏感内容，结果保存至: {output_path}")
    if counts:
        for label, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"         - {label}: {n} 处")
    return counts


def main():
    if len(sys.argv) < 2:
        print("用法: python3 redact_excel.py <输入文件.xlsx/.xls> [输出文件路径]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    redact_excel(input_file, output_file)


if __name__ == "__main__":
    main()
