#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# residual_audit.py - 脱敏后残留敏感信息扫描
# doc-redact-project / v1.3.9
#
# 用途：对脱敏后的文档（.docx/.xlsx/.pptx）执行二次扫描，
#       检测是否存在手机号、邮箱、身份证等明显残留。
#
# 原理：不依赖 common_rules 的全套规则，只检测"漏脱"类高风险模式。
#       低风险模式（地名/姓名误判）不在本脚本范围。
#
# 用法：
#   python residual_audit.py <脱敏后文档路径> [--verbose]
#   python residual_audit.py output_dir/ --verbose   # 扫描整个目录
# ---------------------------------------------------------------------------

from __future__ import annotations

import re
import sys
import zipfile
import argparse
from pathlib import Path
from typing import List, Tuple, Optional


# ---------------------------------------------------------------------------
# 残留模式定义（均为"不应出现在脱敏后文档中"的强信号）
# ---------------------------------------------------------------------------

_RESIDUAL_PATTERNS: List[Tuple[str, str, re.Pattern]] = [
    # 手机号：11位以1开头
    ("手机号", "mobile",
     re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    # 邮箱
    ("邮箱", "email",
     re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-z9]{2,}")),
    # 身份证（18位）
    ("身份证", "id_card",
     re.compile(r"(?<![0-9Xx])([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
                r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9Xx])")),
    # 身份证（15位）
    ("身份证(15位)", "id_card",
     re.compile(r"(?<!\d)([1-9]\d{5}\d{2}(?:0[1-9]|1[0-2])"
                r"(?:0[1-9]|[12]\d|3[01])\d{3})(?!\d)")),
    # 固定电话（区号-号码）
    ("固话", "phone",
     re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")),
    # 银行卡号（16-19位，纯数字串）
    ("银行卡号", "bank_card",
     re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    # IP地址
    ("IP地址", "ip",
     re.compile(r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.])")),
    # 姓名疑似残留：姓氏+单名字池字（如"张明""李娟"），
    # 脱敏后姓名应被替换为 XXX，若仍存在则说明漏脱敏
    ("姓名残留", "name",
     re.compile(r"(?<![a-zA-Z0-9\u4e00-\u9fa5])"
                r"[\u4e00-\u9fa5]{2,3}"
                r"(?![a-zA-Z0-9\u4e00-\u9fa5])")),
]

# 误报白名单（排除已知的占位符/格式文本）
_FALSE_POSITIVE_GUARDS: List[Tuple[str, re.Pattern]] = [
    # XXX 占位符
    ("占位符XXX", re.compile(r"^XXX$")),
    # YYYY/MM/DD 等格式占位符
    ("日期占位符", re.compile(r"\d{4}/\d{2}/\d{2}|\d{4}-\d{2}-\d{2}")),
    # X.X.X.X IP占位符
    ("IP占位符", re.compile(r"X\.X\.X\.X")),
    # XXXX@XXXXX 邮箱占位符
    ("邮箱占位符", re.compile(r"XXXXX@XXXXX")),
    # 纯数字长串（工号/流水号/账号）不应视为银行卡号
    ("工号长串", re.compile(r"^\d{6,12}$")),
]


# ---------------------------------------------------------------------------
# 文档文本提取
# ---------------------------------------------------------------------------

def _extract_text_from_docx(path: Path) -> List[str]:
    """从 .docx 提取所有 w:t 节点文本"""
    import xml.etree.ElementTree as ET
    texts: List[str] = []
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    try:
        with zipfile.ZipFile(path, "r") as zf:
            xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
            for xml_name in xml_names:
                try:
                    content = zf.read(xml_name)
                    root = ET.fromstring(content)
                    for elem in root.iter(f"{{{ns}}}t"):
                        if elem.text:
                            texts.append(elem.text)
                except Exception:
                    pass
    except Exception:
        pass
    return texts


def _extract_text_from_xlsx(path: Path) -> List[str]:
    """从 .xlsx 提取所有单元格文本"""
    import xml.etree.ElementTree as ET
    texts: List[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            xml_names = [n for n in zf.namelist() if n.startswith("xl/") and n.endswith(".xml")]
            for xml_name in xml_names:
                try:
                    content = zf.read(xml_name)
                    root = ET.fromstring(content)
                    for elem in root.iter():
                        if elem.text:
                            texts.append(elem.text)
                except Exception:
                    pass
    except Exception:
        pass
    return texts


def _extract_text_from_pptx(path: Path) -> List[str]:
    """从 .pptx 提取所有文本"""
    import xml.etree.ElementTree as ET
    texts: List[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
            for xml_name in xml_names:
                try:
                    content = zf.read(xml_name)
                    root = ET.fromstring(content)
                    for elem in root.iter():
                        if elem.text:
                            texts.append(elem.text)
                except Exception:
                    pass
    except Exception:
        pass
    return texts


def _extract_text(path: Path) -> List[str]:
    """根据文件类型提取文本"""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _extract_text_from_docx(path)
    elif suffix in (".xlsx", ".xls"):
        return _extract_text_from_xlsx(path)
    elif suffix in (".pptx", ".ppt"):
        return _extract_text_from_pptx(path)
    else:
        return []


# ---------------------------------------------------------------------------
# 残留检测
# ---------------------------------------------------------------------------

def _is_false_positive(text: str) -> bool:
    """判断命中是否属于误报（占位符/格式文本）"""
    for label, guard_re in _FALSE_POSITIVE_GUARDS:
        if guard_re.search(text):
            return True
    return False


def _audit_file(path: Path, verbose: bool = False) -> List[Tuple[str, str, str, str]]:
    """
    扫描单个文件，返回残留项列表。
    返回格式：(类别, 标签, 匹配文本, 上下文)
    """
    findings: List[Tuple[str, str, str, str]] = []
    all_text = _extract_text(path)

    if not all_text:
        return findings

    combined = "\n".join(all_text)

    for label, category, pattern in _RESIDUAL_PATTERNS:
        for m in pattern.finditer(combined):
            matched = m.group(0)
            # 误报过滤
            if _is_false_positive(matched):
                continue
            # 上下文（前30字）
            ctx_start = max(0, m.start() - 15)
            ctx_end = min(len(combined), m.end() + 15)
            context = combined[ctx_start:ctx_end].replace("\n", " ").strip()
            findings.append((category, label, matched, context))

    return findings


def _audit_dir(dir_path: Path, verbose: bool = False) -> Tuple[int, List]:
    """扫描目录，返回 (总文件数, 所有残留项列表)"""
    total = 0
    all_findings: List = []
    extensions = {".docx", ".xlsx", ".xls", ".pptx", ".ppt"}

    for path in sorted(dir_path.rglob("*")):
        if path.is_file() and path.suffix.lower() in extensions:
            total += 1
            file_findings = _audit_file(path, verbose)
            if file_findings:
                all_findings.append((path.relative_to(dir_path), file_findings))

    return total, all_findings


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------

def _render_report(target: Path, total_files: int, all_findings: List) -> str:
    """生成文本报告"""
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("  脱敏残留扫描报告  (residual_audit.py)")
    lines.append("=" * 60)
    lines.append(f"  扫描目标：{target}")
    lines.append(f"  扫描文件数：{total_files}")
    lines.append(f"  发现残留项：{sum(len(f) for _, f in all_findings)} 处")
    lines.append("=" * 60)

    if not all_findings:
        lines.append("")
        lines.append("  ✅ 未发现明显残留，脱敏有效。")
        lines.append("")
        return "\n".join(lines)

    # 按文件分组
    for rel_path, findings in all_findings:
        lines.append("")
        lines.append(f"  📄 {rel_path}")
        lines.append(f"  {'-' * 50}")
        # 按类别分组
        by_category: dict = {}
        for category, label, matched, context in findings:
            by_category.setdefault(label, []).append((matched, context))

        for label, items in by_category.items():
            lines.append(f"    [{label}] ×{len(items)}")
            shown = items[:3]  # 最多显示3例
            for matched, context in shown:
                # 高亮匹配文本
                safe = context.replace(matched, f"▶{matched}◀")
                lines.append(f"      \"{safe}\"")
            if len(items) > 3:
                lines.append(f"      ... 另有 {len(items) - 3} 处未显示")
        lines.append("")

    lines.append("=" * 60)
    lines.append("  ⚠️  如有残留，请检查：")
    lines.append("    1. 是否使用了正确的配置（bank_names / bank_codes）")
    lines.append("    2. 是否存在内嵌附件（OLE Package）需递归脱敏")
    lines.append("    3. 是否有图片内嵌文字（EMF/WMF）需单独处理")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="脱敏残留扫描：检测脱敏后文档中的手机/邮箱/身份证等残留",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python residual_audit.py output/XX银行_脱敏.docx
  python residual_audit.py output/ --verbose
  python residual_audit.py output/ > residual_report.txt
"""
    )
    parser.add_argument("target", help="脱敏后文档路径或目录")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="显示详细上下文")
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    if not target.exists():
        print(f"错误：路径不存在 → {target}", file=sys.stderr)
        sys.exit(1)

    if target.is_dir():
        total, all_findings = _audit_dir(target, args.verbose)
    else:
        total = 1
        all_findings = []
        findings = _audit_file(target, args.verbose)
        if findings:
            all_findings = [(target.name, findings)]

    report = _render_report(target, total, all_findings)
    print(report)


if __name__ == "__main__":
    main()
