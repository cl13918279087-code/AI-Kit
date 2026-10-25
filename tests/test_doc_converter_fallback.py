#!/usr/bin/env python3
"""
test_doc_converter_fallback.py - .doc 备用转换链回归测试（Issue #33）

覆盖 scripts/redact_word.py 的 .doc → .docx 多级转换链：

  L1  LibreOffice（首选，保真最好）
  L2  textutil（macOS 系统自带）
  L3  antiword 纯文本重建（仅保正文文字，丢格式/表格）

安全口径（沿用 Issue #9）：**全链失败时严禁生成输出文件**，
必须抛出异常并登记人工检查——防止"假脱敏"泄漏路径。

运行方式：
    cd <repo>
    python tests/test_doc_converter_fallback.py
    # 或 pytest tests/test_doc_converter_fallback.py -v
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import redact_word as rw  # noqa: E402


def _make_sample_docx(path):
    """造一份含姓名/手机号的样例 .docx，再借 LibreOffice 转成 .doc。"""
    import docx
    d = docx.Document()
    d.add_paragraph("项目组成员如下：李思远、周慧敏、王根利负责需求梳理")
    d.add_paragraph("客户经理张伟提交授信审批材料")
    d.add_paragraph("联系电话：13598010596")
    d.save(path)


def _find_any_doc():
    """在临时目录里造一个 .doc（需要 LibreOffice 或 textutil 至少一个可用）。"""
    tmp = tempfile.mkdtemp(prefix="doc_conv_test_")
    docx_path = os.path.join(tmp, "sample.docx")
    _make_sample_docx(docx_path)
    doc_path = os.path.join(tmp, "sample.doc")

    conv = rw._find_converter()
    if conv:
        import subprocess
        subprocess.run(
            [conv, "-env:UserInstallation=" + (os.path.join(tmp, "lo") + "/").replace("\\", "/"),
             "--headless", "--convert-to", "doc", "--outdir", tmp, docx_path],
            capture_output=True, timeout=120,
        )
    if not os.path.exists(doc_path):
        # 退路：直接复制 docx 并改后缀（textutil 仍能读 OOXML）
        shutil.copy2(docx_path, doc_path)
    return tmp, doc_path


# ---------------------------------------------------------------------------
# 1. 转换链结构
# ---------------------------------------------------------------------------

def test_converter_chain_has_three_levels():
    """备用转换链必须至少含 LibreOffice / textutil / antiword 三级。"""
    names = [n for n, _ in rw._DOC_CONVERTERS]
    assert "LibreOffice" in names, "转换链缺少 LibreOffice 主通道"
    assert "textutil" in names, "转换链缺少 textutil 备用通道"
    assert "antiword" in names, "转换链缺少 antiword 兜底通道"
    # 保真度顺序：主通道在前
    assert names.index("LibreOffice") < names.index("textutil")


def test_all_failed_raises_and_no_output():
    """全链失败时必须抛异常，且**不得生成任何输出文件**（防假脱敏）。"""
    tmp = tempfile.mkdtemp(prefix="doc_conv_fail_")
    try:
        doc_path = os.path.join(tmp, "sample.doc")
        with open(doc_path, "wb") as f:
            f.write(b"not a real doc")
        out_path = os.path.join(tmp, "out.docx")

        # 强制三条通道全部失败
        orig = rw._DOC_CONVERTERS
        rw._DOC_CONVERTERS = (
            ("LibreOffice", lambda p, t: None),
            ("textutil", lambda p, t: None),
            ("antiword", lambda p, t: None),
        )
        try:
            raised = False
            try:
                rw.redact_doc_to_docx(doc_path, out_path)
            except RuntimeError:
                raised = True
            assert raised, "全链失败时未抛出 RuntimeError"
            assert not os.path.exists(out_path), (
                "全链失败却生成了输出文件——这是'假脱敏'泄漏路径（Issue #9 口径）"
            )
        finally:
            rw._DOC_CONVERTERS = orig
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. 降级路径
# ---------------------------------------------------------------------------

def test_degrade_to_textutil_still_redacts():
    """L1 不可用时降级到 textutil，脱敏结果须与主通道一致。"""
    if not shutil.which("textutil"):
        import pytest
        pytest.skip("当前环境无 textutil（仅 macOS 自带），跳过")

    tmp, doc_path = _find_any_doc()
    try:
        out_path = os.path.join(tmp, "out_textutil.docx")
        orig = rw._DOC_CONVERTERS
        rw._DOC_CONVERTERS = (
            ("LibreOffice", lambda p, t: None),
            ("textutil", rw._convert_doc_via_textutil),
            ("antiword", rw._convert_doc_via_antiword),
        )
        try:
            result = rw.redact_doc_to_docx(doc_path, out_path)
        finally:
            rw._DOC_CONVERTERS = orig

        assert os.path.exists(out_path), "textutil 降级通道未产出输出"
        assert result.get("degraded_level") == "textutil", (
            f"降级标记不对: {result.get('degraded_level')}"
        )
        assert result.get("doc_converter") == "textutil"
        assert result.get("degraded_note"), "降级时必须给出人工复核提示"

        import docx
        text = "\n".join(p.text for p in docx.Document(out_path).paragraphs)
        assert "李思远" not in text, f"降级通道漏脱真名: {text}"
        assert "13598010596" not in text, f"降级通道漏脱手机号: {text}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_primary_channel_reports_no_degradation():
    """主通道（LibreOffice）成功时不得标记降级。"""
    if not rw._find_converter():
        import pytest
        pytest.skip("当前环境无 LibreOffice，跳过")

    tmp, doc_path = _find_any_doc()
    try:
        out_path = os.path.join(tmp, "out_l1.docx")
        result = rw.redact_doc_to_docx(doc_path, out_path)
        assert os.path.exists(out_path)
        assert result.get("degraded_level") is None, (
            f"主通道成功却标记了降级: {result.get('degraded_level')}"
        )
        assert result.get("doc_converter") == "LibreOffice"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
