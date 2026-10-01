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

  # 阶段一（检测 + 灰区评审）
  python3 redact_all.py input.docx --detect-only
  # Agent 读取 gray_review.json，评审后写入 decisions.json

  # 阶段二（应用 Agent 决策后执行）
  python3 redact_all.py input.docx --agent-decisions /path/to/decisions.json

  # 纯自动化（灰区自动遮盖，无人交互）
  python3 redact_all.py input.docx --auto-gray
"""

import sys
import os
import json
import shutil
import tempfile
from pathlib import Path

from common_rules import (
    add_custom_replacement, REDACTION_LABELS,
    GRAY_CONFIDENCE_THRESHOLD, is_gray_entity, load_agent_decisions
)


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
# 阶段一：灰区检测（不修改文件，输出评审 JSON）
# ---------------------------------------------------------------------------

def _extract_text_for_detection(file_path: str, ext: str) -> str:
    """提取纯文本用于实体检测"""
    p = Path(file_path)
    if ext in (".docx", ".xlsx", ".pptx"):
        import zipfile
        try:
            with zipfile.ZipFile(p) as z:
                if ext == ".docx":
                    members = [n for n in z.namelist() if n.endswith(".xml") and "word/" in n]
                elif ext == ".xlsx":
                    members = [n for n in z.namelist() if n.endswith(".xml")]
                else:
                    members = [n for n in z.namelist() if n.endswith(".xml")]
                texts = []
                for member in members[:20]:  # 限制最多 20 个 XML，避免超长
                    try:
                        content = z.read(member).decode("utf-8", errors="ignore")
                        # 简单去除 XML 标签
                        import re as _re
                        txt = _re.sub(r"<[^>]+>", " ", content)
                        txt = _re.sub(r"\s+", " ", txt).strip()
                        if txt:
                            texts.append(txt)
                    except Exception:
                        pass
                return " ".join(texts)[:50000]  # 限制 5 万字
        except Exception as e:
            return f"[文本提取失败: {e}]"
    elif ext == ".pdf":
        return f"[PDF 请用 --auto-gray 模式直接处理，不支持 --detect-only]"
    elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"):
        return f"[图片请用 --auto-gray 模式，不支持 --detect-only]"
    elif ext == ".doc":
        return f"[.doc 请先转换为 .docx 后再使用 --detect-only]"
    else:
        # 纯文本文件直接读
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                return f.read()[:50000]
        except Exception:
            return ""


def detect_gray_entities(input_path: str, output_json: str = None) -> dict:
    """
    阶段一：对输入文件执行实体检测，识别灰区实体并写入 JSON。

    返回：
        {
          "file": str,           # 输入文件路径
          "total_entities": int,  # 总实体数
          "green": [entity],     # 高置信，直接遮盖
          "gray": [entity],       # 灰区，交 Agent 评审
          "gray_json_path": str, # 灰区详情写入路径
        }
    """
    from entity_detector import build_llm_detector

    p = Path(input_path).resolve()
    ext = p.suffix.lower()

    if not p.exists():
        print(f"[错误] 文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    # 提取文本
    print(f"[检测] {p.name}")
    print(f"[格式] {ext}")
    text = _extract_text_for_detection(str(p), ext)
    if text.startswith("[") or not text:
        print(f"[错误] {text}", file=sys.stderr)
        sys.exit(1)

    print(f"[文本] 已提取 {len(text)} 字")

    # 构建检测器
    config_path = Path(__file__).parent / "config.json"
    detector = build_llm_detector(str(config_path) if config_path.exists() else None)

    # 执行检测
    manifest = detector.detect(text)
    manifest.filename = p.name

    green = []
    gray = []

    for e in manifest.entities:
        info = {
            "text": e.text,
            "replacement": e.replacement,
            "category": e.category,
            "confidence": e.confidence,
            "source": e.source,
            "evidence": e.evidence,
        }
        if is_gray_entity(e.confidence):
            gray.append(info)
        else:
            green.append(info)

    print(f"[结果] 共检测 {len(green)} 个高置信实体，{len(gray)} 个灰区实体（需 Agent 评审）")
    print(f"[阈值] 灰区置信度 < {GRAY_CONFIDENCE_THRESHOLD}")

    # 生成灰区评审 JSON
    if output_json:
        gray_detail_path = output_json
    else:
        gray_detail_path = str(Path(tempfile.gettempdir()) / f"redact_gray_{os.getpid()}.json")

    with open(gray_detail_path, "w", encoding="utf-8") as f:
        json.dump({
            "file": str(p),
            "threshold": GRAY_CONFIDENCE_THRESHOLD,
            "total": len(green) + len(gray),
            "green_count": len(green),
            "gray_count": len(gray),
            "green": green,
            "gray": gray,
        }, f, ensure_ascii=False, indent=2)

    print(f"[灰区] 已写入: {gray_detail_path}")
    print(f"\n请将 gray_review.json 交给 Agent 评审，")
    print(f"评审后将决策写入 decisions.json，然后运行：")
    print(f"  python3 redact_all.py {input_path} --agent-decisions decisions.json\n")

    return {
        "file": str(p),
        "total_entities": len(green) + len(gray),
        "green": green,
        "gray": gray,
        "gray_json_path": gray_detail_path,
    }


# ---------------------------------------------------------------------------
# 阶段二：应用 Agent 决策（读取 decisions.json，跳过指定实体）
# ---------------------------------------------------------------------------

def apply_agent_decisions(input_path: str, decisions_path: str) -> None:
    """
    阶段二：根据 Agent 评审决策执行脱敏。

    decisions.json 格式：
      [{"text": "张明", "decision": "skip"},
       {"text": "黎明", "decision": "redact"}]
    """
    decisions = load_agent_decisions(decisions_path)
    if not decisions:
        print(f"[警告] 决策文件为空或解析失败，将正常执行脱敏", file=sys.stderr)
        return

    skip_set = {t for t, d in decisions.items() if d == "skip"}
    redact_set = {t for t, d in decisions.items() if d == "redact"}
    print(f"[决策] 跳过 {len(skip_set)} 个实体，追加遮盖 {len(redact_set)} 个实体")

    # 将追加遮盖实体写入 manifest_override 临时文件，供 handler 读取
    override_path = Path(tempfile.gettempdir()) / f"redact_override_{os.getpid()}.json"
    with open(override_path, "w", encoding="utf-8") as f:
        json.dump({"skip": list(skip_set), "redact": list(redact_set)}, f, ensure_ascii=False)

    return str(override_path)


# ---------------------------------------------------------------------------
# 批量脱敏（支持 glob 模式）
# ---------------------------------------------------------------------------

def redact_file(input_path: str, output_path: str = None,
                method: str = "mosaic",
                manifest_override: str = None,
                auto_gray: bool = False) -> dict:
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
    if manifest_override:
        print(f"[决策] 加载 Agent 评审决策: {manifest_override}")

    try:
        if ext in (".pdf",):
            return handler(str(input_path), str(output_path),
                          manifest_override=manifest_override, auto_gray=auto_gray)
        elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"):
            return handler(str(input_path), str(output_path), method,
                          manifest_override=manifest_override, auto_gray=auto_gray)
        else:
            return handler(str(input_path), str(output_path),
                          manifest_override=manifest_override, auto_gray=auto_gray)
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
    detect_only = False
    agent_decisions = None
    auto_gray = False

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
            files.extend(args)
            args = []
        elif arg == "--detect-only":
            detect_only = True
        elif arg == "--agent-decisions":
            agent_decisions = args.pop(0)
        elif arg == "--auto-gray":
            auto_gray = True
        elif not arg.startswith("--"):
            files.append(arg)

    if not files:
        print("[错误] 请指定输入文件", file=sys.stderr)
        sys.exit(1)

    # 阶段一：灰区检测
    if detect_only:
        if len(files) != 1:
            print("[错误] --detect-only 仅支持单文件", file=sys.stderr)
            sys.exit(1)
        detect_gray_entities(files[0], output)
        sys.exit(0)

    # 阶段二：应用 Agent 评审决策
    if agent_decisions:
        if len(files) != 1:
            print("[错误] --agent-decisions 仅支持单文件", file=sys.stderr)
            sys.exit(1)
        override_path = apply_agent_decisions(files[0], agent_decisions)
        # 正常执行，handlers 会读取 override_path
        redact_file(files[0], output, method, manifest_override=override_path)
        sys.exit(0)

    # 默认：纯自动化（灰区直接遮盖，不交 Agent）
    if len(files) == 1:
        redact_file(files[0], output, method, auto_gray=auto_gray)
    else:
        redact_batch(files, output, method)


if __name__ == "__main__":
    main()
