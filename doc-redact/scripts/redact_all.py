#!/usr/bin/env python3
"""
redact_all.py - 文档脱敏统一入口
根据文件扩展名自动分发到对应处理器。

支持格式：
  .docx / .doc  → Word 文档
  .xlsx / .xls  → Excel 电子表格
  .pptx / .ppt  → PPT 演示文稿
  .pdf          → PDF 文档
  .png / .jpg / .jpeg / .bmp / .gif / .webp / .tiff → 图片

用法：
  python3 redact_all.py <输入文件> [输出文件]

示例：
  python3 redact_all.py report.docx
  python3 redact_all.py data.xlsx output_redacted.xlsx
  python3 redact_all.py contract.pdf --method mosaic
"""

import sys
import os
import shutil
import tempfile
from pathlib import Path

from common_rules import add_custom_replacement, REDACTIONS, REDACTION_LABELS


# ---------------------------------------------------------------------------
# 动态导入各格式处理器（延迟加载，加快启动速度）
# ---------------------------------------------------------------------------

def _load_handler(ext: str):
    handlers = {
        (".docx", ".doc"):  ("redact_word",   "redact_word"),
        (".xlsx", ".xls"):  ("redact_excel",  "redact_excel"),
        (".pptx", ".ppt"):  ("redact_ppt",    "redact_ppt"),
        (".pdf",):           ("redact_pdf",     "redact_pdf"),
        (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"):
                            ("redact_image",   "redact_image"),
    }
    for keys, (module, func) in handlers.items():
        if ext in keys:
            mod = __import__(module, fromlist=[func])
            return getattr(mod, func)
    return None


# ---------------------------------------------------------------------------
# 批量脱敏（支持 glob 模式）
# ---------------------------------------------------------------------------

def redact_file(input_path: str, output_path: str = None,
                method: str = "mosaic") -> dict:
    """对单个文件执行脱敏，返回统计"""
    input_path = Path(input_path).resolve()
    ext = input_path.suffix.lower()

    if output_path:
        output_path = Path(output_path).resolve()
    else:
        suffix_map = {
            ".docx": "_脱敏.docx", ".doc": "_脱敏.doc",
            ".xlsx": "_脱敏.xlsx", ".xls": "_脱敏.xls",
            ".pptx": "_脱敏.pptx", ".ppt": "_脱敏.ppt",
            ".pdf": "_脱敏.pdf",
        }
        default_suffix = suffix_map.get(ext, "_脱敏" + ext)
        output_path = input_path.with_name(f"{input_path.stem}{default_suffix}")

    handler = _load_handler(ext)
    if handler is None:
        print(f"[错误] 不支持的文件格式: {ext}", file=sys.stderr)
        return {}

    print(f"\n[处理] {input_path.name}")
    print(f"[格式] {ext}")

    try:
        if ext in (".pdf",):
            return handler(str(input_path), str(output_path))
        elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"):
            return handler(str(input_path), str(output_path), method)
        else:
            return handler(str(input_path), str(output_path))
    except Exception as e:
        print(f"[错误] 处理失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return {}


def redact_batch(patterns: list, output_dir: str = None,
                 method: str = "mosaic") -> None:
    """对多个文件/glob 模式执行批量脱敏"""
    files = []
    for pattern in patterns:
        p = Path(pattern)
        if p.is_file():
            files.append(p)
        else:
            files.extend(p.glob(pattern) if "*" in pattern else [])

    files = sorted(set(f for f in files if f.is_file()))
    if not files:
        print("[错误] 未找到匹配的文件", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(output_dir) if output_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    total_counts = {}
    for f in files:
        out = out_dir / f.name if out_dir else None
        counts = redact_file(str(f), str(out) if out else None, method)
        for k, v in counts.items():
            total_counts[k] = total_counts.get(k, 0) + v

    print(f"\n{'='*50}")
    print(f"批量处理完成: {len(files)} 个文件")
    if total_counts:
        print("汇总统计:")
        for k, v in sorted(total_counts.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v} 处")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_usage():
    print(__doc__)


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    method = "mosaic"
    files = []
    output = None

    args = sys.argv[1:]
    while args:
        arg = args.pop(0)
        if arg == "--help" or arg == "-h":
            print_usage()
            sys.exit(0)
        elif arg.startswith("--method="):
            method = arg.split("=", 1)[1]
        elif arg == "--output" or arg == "-o":
            output = args.pop(0)
        elif arg == "--batch":
            # 后续所有参数均为文件/模式
            files.extend(args)
            args = []
        elif not arg.startswith("--"):
            files.append(arg)

    if not files:
        print("[错误] 请指定输入文件", file=sys.stderr)
        sys.exit(1)

    if len(files) == 1:
        redact_file(files[0], output, method)
    else:
        redact_batch(files, output, method)


if __name__ == "__main__":
    main()
