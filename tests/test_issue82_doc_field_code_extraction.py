"""Issue #82：`.doc` 域代码（TOC / PAGEREF / HYPERLINK 指令）不进正文文本流。

【背景】
Word 97-2003 二进制正文流把**域指令段**与**域结果段**同体存储：
    0x13 域开始 → <指令文本> → 0x14 域分隔 → <结果文本> → 0x15 域结束
指令文本（`TOC \\o "1-5" \\h \\z \\u`、`PAGEREF _Toc2599986 \\h`、
`HYPERLINK \\l _Toc…`）是**域的元数据**，不是可见正文；
Word / WPS 渲染出的目录只显示结果段。
若把指令原样抽进正文，会与 docx 侧（python-docx 只读 `w:t`、不读 `w:instrText`）
产生**口径分裂** → 两侧文本不可比 → G3 比对指标被对齐噪声污染
（域码类伪 residual 127 条、over 39 条中绝大多数为 `_TocNNNN` 数字碎片）。

【整改要点】
`scripts/doc_ole_text.py` 新增：
- `strip_field_instructions(text)`：丢弃指令段、保留结果段；
- `extract_doc_text(path, include_field_codes=False)`：默认输出**渲染文本**，
  传 `include_field_codes=True` 可取回原始码（回溯兼容）。

运行：
    python3 -m pytest tests/test_issue82_doc_field_code_extraction.py -v
"""
import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from doc_ole_text import extract_doc_text, strip_field_instructions  # noqa: E402


BEGIN = "\x13"
SEP = "\x14"
END = "\x15"


# ---------------------------------------------------------------------------
# 1. 域指令被丢弃、域结果被保留
# ---------------------------------------------------------------------------

def test_toc_instruction_dropped_result_kept():
    """目录域：指令段丢弃、结果段保留。"""
    text = (f"目录{BEGIN}TOC \\o \"1-5\" \\h \\z \\u{SEP}"
            f"1.前言\t1{END}正文")
    out = strip_field_instructions(text)
    assert "TOC" not in out, f"域指令泄漏：{out!r}"
    assert "_Toc" not in out
    assert "1.前言" in out, f"域结果被误删：{out!r}"
    assert "正文" in out


def test_pageref_instruction_dropped():
    """PAGEREF 域：`_TocNNNN` 编号不得进入正文。"""
    text = f"1.前言{BEGIN}PAGEREF _Toc2599986 \\h{SEP}3{END}"
    out = strip_field_instructions(text)
    assert "_Toc2599986" not in out, f"PAGEREF 指令泄漏：{out!r}"
    assert "PAGEREF" not in out
    assert out == "1.前言3"


def test_hyperlink_instruction_dropped():
    """HYPERLINK 域：指令段（含 URL）不得进入正文。"""
    text = (f"6.1.6 核对进账{BEGIN}HYPERLINK \"ftp://191.0.1.153\"{SEP}"
            f"核对进账{END}")
    out = strip_field_instructions(text)
    assert "HYPERLINK" not in out, f"HYPERLINK 指令泄漏：{out!r}"
    assert "ftp://" not in out, f"URL 泄漏：{out!r}"
    assert "核对进账" in out


def test_nested_field_instruction_dropped():
    """嵌套域：内层域位于**外层指令段**时，内层结果同样丢弃。"""
    text = (f"{BEGIN}TOC {BEGIN}PAGEREF _Toc1 \\h{SEP}7{END} \\o \"1-5\"{SEP}"
            f"目录结果{END}")
    out = strip_field_instructions(text)
    assert "TOC" not in out
    assert "_Toc1" not in out
    assert "7" not in out, f"嵌套域结果不应保留：{out!r}"
    assert "目录结果" in out, f"外层域结果被误删：{out!r}"


def test_plain_text_unchanged():
    """无域的普通正文必须原样保留。"""
    text = "第一章 前言\n本节说明系统改造范围。\n"
    assert strip_field_instructions(text) == text


def test_unbalanced_field_chars_are_tolerated():
    """域控制符不成对时不得抛异常（健壮性）。"""
    for text in (BEGIN, END, SEP, f"正文{BEGIN}未闭合", f"正文{END}多余"):
        out = strip_field_instructions(text)
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# 2. extract_doc_text 默认输出渲染文本
# ---------------------------------------------------------------------------

def test_extract_doc_text_signature_supports_field_codes_switch():
    """`extract_doc_text` 必须支持 `include_field_codes` 开关。"""
    sig = inspect.signature(extract_doc_text)
    assert "include_field_codes" in sig.parameters
    assert sig.parameters["include_field_codes"].default is False


def test_extract_doc_text_default_is_render_text():
    """默认抽取结果为渲染文本（缺文件时降级为空串，不抛异常）。"""
    missing = REPO / ".tmpwork" / "__no_such_file_issue82__.doc"
    assert not missing.exists()
    assert extract_doc_text(missing) == ""
