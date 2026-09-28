#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# keep_audit.py - 脱敏质量评估（误伤率分析）
# doc-redact-project / v1.3.9
#
# 用途：在基线语料（已知无害文本）上运行脱敏规则，
#       量化误伤率（false positive rate），辅助规则调优。
#
# 工作模式：
#   模式1（文件对比）：提供 原文件 + 脱敏后文件 → 分析脱敏差异点
#   模式2（语料扫描）：提供无害语料目录 → 统计误伤触发率
#
# 误伤定义：文本中无害内容被错误替换为占位符
# 用法：
#   python keep_audit.py --original input.docx --redacted output.docx
#   python keep_audit.py --corpus test_corpus/  # 扫描语料目录统计误伤率
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import re
import zipfile
import argparse
from pathlib import Path
from typing import List, Tuple, Optional, Dict


# ---------------------------------------------------------------------------
# 误伤模式库（常见误脱词，按"被误脱的文本 → 原始文本"映射）
# ---------------------------------------------------------------------------

# 已知误脱词库：占位符 → 原始词
# 当检测到以下占位符形态时，尝试还原并判断是否属于误脱
KNOWN_FALSE_POSITIVES: Dict[str, str] = {
    "XXX": None,       # 姓名占位符（需要上下文判断，不直接映射）
    "XX银行": None,    # 银行占位符（需要上下文判断）
    "XXXX": None,      # 组织占位符
    "XX省XX市XX区": None,  # 地址占位符
}

# 常见无害词（若被误脱为 XXX，归还原文）
HARMLESS_WORDS = {
    # 常见业务词
    "客户", "企业", "公司", "集团", "单位", "个人", "银行",
    "系统", "平台", "网络", "渠道", "业务", "产品", "服务",
    "管理", "运营", "风险", "合规", "安全", "数据", "信息",
    "交易", "支付", "清算", "结算", "账户", "账务", "核算",
    "流程", "制度", "规范", "标准", "方案", "计划", "报告",
    # 常见二字词（易被姓名规则误捕）
    "说明", "实施", "启动", "推进", "落实", "完善", "优化", "创新",
    "成立", "成功", "运营", "管理", "发展", "建设", "推进",
    "骨干", "成员", "创业", "兴业", "恒信", "隆昌",
    "腾飞", "卓越", "领先", "稳健", "风控",
    # 银行/金融术语
    "存款", "贷款", "理财", "基金", "债券", "外汇", "贵金属",
    "网点", "支行", "分行", "营业部", "总行",
    # 文档结构词
    "第一章", "第二章", "第三章", "第四章", "第五章",
    "一、", "二、", "三、", "四、", "五、",
    "（一）", "（二）", "（三）",
}


# ---------------------------------------------------------------------------
# 文本提取
# ---------------------------------------------------------------------------

def _extract_text_from_docx(path: Path) -> str:
    import xml.etree.ElementTree as ET
    parts: List[str] = []
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for xml_name in zf.namelist():
                if not xml_name.endswith(".xml"):
                    continue
                try:
                    content = zf.read(xml_name)
                    root = ET.fromstring(content)
                    for elem in root.iter(f"{{{ns}}}t"):
                        if elem.text:
                            parts.append(elem.text)
                except Exception:
                    pass
    except Exception:
        pass
    return "\n".join(parts)


def _extract_text_from_xlsx(path: Path) -> str:
    import xml.etree.ElementTree as ET
    parts: List[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for xml_name in zf.namelist():
                if not (xml_name.startswith("xl/") and xml_name.endswith(".xml")):
                    continue
                try:
                    content = zf.read(xml_name)
                    root = ET.fromstring(content)
                    for elem in root.iter():
                        if elem.text:
                            parts.append(elem.text)
                except Exception:
                    pass
    except Exception:
        pass
    return "\n".join(parts)


def _extract_text_from_pptx(path: Path) -> str:
    import xml.etree.ElementTree as ET
    parts: List[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for xml_name in zf.namelist():
                if not xml_name.endswith(".xml"):
                    continue
                try:
                    content = zf.read(xml_name)
                    root = ET.fromstring(content)
                    for elem in root.iter():
                        if elem.text:
                            parts.append(elem.text)
                except Exception:
                    pass
    except Exception:
        pass
    return "\n".join(parts)


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _extract_text_from_docx(path)
    elif suffix in (".xlsx", ".xls"):
        return _extract_text_from_xlsx(path)
    elif suffix in (".pptx", ".ppt"):
        return _extract_text_from_pptx(path)
    return ""


# ---------------------------------------------------------------------------
# 差异分析（模式1：原文件 vs 脱敏后文件）
# ---------------------------------------------------------------------------

def _analyze_diff(original_text: str, redacted_text: str) -> Dict:
    """使用 difflib 分析原文件与脱敏后文件的差异"""
    import difflib
    changes = []
    sm = difflib.SequenceMatcher(None, original_text, redacted_text)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            changes.append({
                "type": "replace",
                "original": original_text[i1:i2],
                "redacted": redacted_text[j1:j2],
            })
        elif tag == "delete":
            changes.append({
                "type": "delete",
                "original": original_text[i1:i2],
                "redacted": "",
            })
    return {"changes": changes, "total_changes": len(changes)}


def _classify_change(change: Dict) -> str:
    """判断变更是否属于误脱（harmless_fp）、真脱（legitimate）、或可疑（suspicious）"""
    orig = change.get("original", "")
    redac = change.get("redacted", "")

    # 真脱类型判断
    if redac in ("XXX", "XXXX") and len(orig) <= 4:
        # 2-4字中文词被替换为 XXX → 检查是否在无害词表
        if orig in HARMLESS_WORDS:
            return "harmless_fp"
        return "legitimate_name"
    if "XX" in redac and orig in HARMLESS_WORDS:
        return "harmless_fp"
    if redac.startswith("0XX"):
        return "legitimate_phone"
    if "@" not in orig and "XXXXX@XXXXX" in redac:
        return "suspicious"
    if redac.startswith("XX") and "银行" not in orig and len(orig) <= 6:
        return "suspicious"
    if "YYYY" in redac:
        return "legitimate_date"

    return "suspicious"


def _audit_diff(original_path: Path, redacted_path: Path) -> Dict:
    """模式1分析：原文件 vs 脱敏后文件"""
    orig_text = _extract_text(original_path)
    redac_text = _extract_text(redacted_path)

    analysis = _analyze_diff(orig_text, redac_text)
    classified: Dict[str, List] = {
        "harmless_fp": [],
        "legitimate_name": [],
        "legitimate_phone": [],
        "legitimate_date": [],
        "suspicious": [],
    }

    for change in analysis.get("changes", []):
        cat = _classify_change(change)
        classified[cat].append(change)

    # 统计
    total = len(analysis.get("changes", []))
    summary = {
        "total_changes": total,
        "false_positives": len(classified["harmless_fp"]),
        "legitimate": (
            len(classified["legitimate_name"])
            + len(classified["legitimate_phone"])
            + len(classified["legitimate_date"])
        ),
        "suspicious": len(classified["suspicious"]),
        "classified": classified,
    }
    return summary


# ---------------------------------------------------------------------------
# 语料扫描（模式2：统计误伤触发率）
# ---------------------------------------------------------------------------

def _scan_corpus(corpus_dir: Path) -> Dict:
    """模式2：扫描无害语料，统计误脱触发率"""
    import common_rules as cr
    cr.reset_patterns()

    total_lines = 0
    total_redacted = 0
    fp_lines: List[Tuple[str, str, str]] = []  # (行文本, 脱敏后, 误脱词)

    extensions = {".docx", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md"}

    for path in sorted(corpus_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue

        if path.suffix.lower() in (".txt", ".md"):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
        else:
            text = _extract_text(path)
            lines = text.splitlines()

        for line in lines:
            if not line.strip():
                continue
            total_lines += 1
            redacted = cr.apply_redactions(line)
            if redacted != line:
                total_redacted += 1
                # 提取被替换的词
                orig_words = set(w for w in HARMLESS_WORDS if w in line and w not in redacted)
                if orig_words:
                    fp_lines.append((line, redacted, ", ".join(orig_words)))

    fp_rate = (total_redacted / total_lines * 100) if total_lines > 0 else 0
    fp_pct_of_redacted = (len(fp_lines) / total_redacted * 100) if total_redacted > 0 else 0

    return {
        "total_lines": total_lines,
        "total_redacted": total_redacted,
        "redaction_rate": fp_rate,
        "false_positives": len(fp_lines),
        "fp_pct_of_redacted": fp_pct_of_redacted,
        "fp_lines": fp_lines[:20],  # 最多显示20例
    }


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------

def _render_diff_report(summary: Dict) -> str:
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("  脱敏质量评估报告  (keep_audit.py · 文件对比模式)")
    lines.append("=" * 60)

    total = summary["total_changes"]
    fp = summary["false_positives"]
    legit = summary["legitimate"]
    susp = summary["suspicious"]

    lines.append(f"  总变更次数：{total}")
    lines.append(f"  误脱（false positive）：{fp} ({fp/total*100:.1f}%)" if total else "  总变更次数：0")
    lines.append(f"  合法脱敏：{legit} ({legit/total*100:.1f}%)" if total else "")
    lines.append(f"  可疑变更：{susp} ({susp/total*100:.1f}%)" if total else "")
    lines.append("=" * 60)

    classified = summary.get("classified", {})

    if classified.get("harmless_fp"):
        lines.append("")
        lines.append("  ⚠️  误脱案例（无害词被错误遮盖）：")
        for ch in classified["harmless_fp"][:5]:
            lines.append(f"    原文本：\"{ch['original'][:30]}\"")
            lines.append(f"    脱敏后：\"{ch['redacted'][:30]}\"")
            lines.append("")

    if classified.get("suspicious"):
        lines.append("")
        lines.append("  ❓ 可疑变更（需人工确认）：")
        for ch in classified["suspicious"][:5]:
            lines.append(f"    原文本：\"{ch['original'][:30]}\"")
            lines.append(f"    脱敏后：\"{ch['redacted'][:30]}\"")
            lines.append("")

    if not classified.get("harmless_fp") and not classified.get("suspicious"):
        lines.append("")
        lines.append("  ✅ 未发现明显误脱，质量良好。")

    lines.append("")
    lines.append("=" * 60)
    lines.append("  建议：误脱词可加入 EXCLUDED_COMMON_WORDS 保护集")
    lines.append("=" * 60)
    return "\n".join(lines)


def _render_corpus_report(result: Dict) -> str:
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("  脱敏质量评估报告  (keep_audit.py · 语料扫描模式)")
    lines.append("=" * 60)
    lines.append(f"  扫描总行数：{result['total_lines']}")
    lines.append(f"  被脱敏行数：{result['total_redacted']} ({result['redaction_rate']:.1f}%)")
    lines.append(f"  疑似误脱行数：{result['false_positives']} (占被脱敏的 {result['fp_pct_of_redacted']:.1f}%)")
    lines.append("=" * 60)

    if result["fp_lines"]:
        lines.append("")
        lines.append("  ⚠️  疑似误脱案例（无害词被遮盖）：")
        for i, (orig, redacted, words) in enumerate(result["fp_lines"][:10], 1):
            lines.append(f"  {i}. 原：\"{orig[:40]}\"")
            lines.append(f"     脱：\"{redacted[:40]}\"")
            lines.append(f"     词：{words}")
            lines.append("")

    if not result["fp_lines"]:
        lines.append("")
        lines.append("  ✅ 未发现明显误脱，规则质量良好。")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="脱敏质量评估：量化误伤率，辅助规则调优",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
模式1（文件对比）：提供 原文件 和 脱敏后文件 → 分析每处差异
  python keep_audit.py --original input.docx --redacted output.docx

模式2（语料扫描）：扫描无害语料目录 → 统计误脱触发率
  python keep_audit.py --corpus test_corpus/
  python keep_audit.py --corpus test_corpus/ --config config.json
"""
    )
    parser.add_argument("--original", type=Path,
                        help="原始文档路径（对比模式）")
    parser.add_argument("--redacted", type=Path,
                        help="脱敏后文档路径（对比模式）")
    parser.add_argument("--corpus", type=Path,
                        help="语料目录路径（语料扫描模式）")
    parser.add_argument("--config", type=Path,
                        help="config.json 路径（默认使用脚本同目录的 config.json）")
    args = parser.parse_args()

    # 若指定了 config，加载它
    if args.config:
        sys.path.insert(0, str(args.config.parent))
        import common_rules as cr
        cr._CONFIG_CACHE = None
        cr.reset_patterns()

    if args.original and args.redacted:
        # 模式1：文件对比
        if not args.original.exists():
            print(f"错误：原文件不存在 → {args.original}", file=sys.stderr)
            sys.exit(1)
        if not args.redacted.exists():
            print(f"错误：脱敏文件不存在 → {args.redacted}", file=sys.stderr)
            sys.exit(1)
        summary = _audit_diff(args.original, args.redacted)
        print(_render_diff_report(summary))

    elif args.corpus:
        # 模式2：语料扫描
        if not args.corpus.is_dir():
            print(f"错误：目录不存在 → {args.corpus}", file=sys.stderr)
            sys.exit(1)
        result = _scan_corpus(args.corpus)
        print(_render_corpus_report(result))

    else:
        parser.print_help()
        print("\n错误：请指定 --original+--redacted 或 --corpus", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
