"""
Issue #21 / #54 D1：--auto-gray 标志真正生效 + 灰区自动裁决留痕回归。

【背景】
Issue #21 报告 `redact_all.py` 解析了 `--auto-gray` 却从未使用；
`8bbf6cc`（Issues #18~#24 整改合入）把它传给了各 handler，但：
  ① `redact_word()` **不接受** `auto_gray` 关键字 →
     `redact_all.py input.docx`（无论是否带 `--auto-gray`）**必然抛 TypeError**，
     主入口对 Word 文件**零产出**（异常被 try/except 吞掉，只打印错误）；
  ② 即便传对了，各 handler 也只是「接收但不使用」——flag 仍形同虚设。

本文件的断言：
  1. `redact_all.redact_file()` 对 .docx 路径**不再崩溃**，且**真实产出脱敏文件**；
  2. `--auto-gray` 导出 `decisions_pending.json` 留痕（灰区自动裁决为 redact）；
  3. 留痕文件带 `auto_adjudicated` 标记，且**不影响**脱敏结果本身。

运行：
    python3 -m pytest tests/test_issue21_auto_gray.py -v
"""
import json
import os
import shutil
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import redact_all  # noqa: E402


@pytest.fixture()
def docx_file():
    """构造一个最小 .docx（真名 + 手机号 + 业务短语）。"""
    from docx import Document

    tmp = tempfile.mkdtemp(prefix="issue21_")
    path = os.path.join(tmp, "sample.docx")
    doc = Document()
    doc.add_paragraph("李强负责张三项目，联系电话13800138000。")
    doc.add_paragraph("需求组张经理汇报进展")
    doc.save(path)
    yield path
    shutil.rmtree(tmp, ignore_errors=True)


def test_redact_file_docx_does_not_crash(docx_file):
    """Issue #21 回归：`redact_file()` 对 .docx 不得抛 TypeError（主入口零产出）。"""
    tmp = tempfile.mkdtemp(prefix="issue21_out_")
    try:
        out = os.path.join(tmp, "out.docx")
        counts = redact_all.redact_file(docx_file, out)
        assert os.path.exists(out), "脱敏文件未产出——auto_gray 传参导致主入口崩溃"
        assert isinstance(counts, dict) and counts, f"脱敏统计为空：{counts}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_docx_output_actually_redacted(docx_file):
    """产出的 .docx 必须真的被脱敏（姓名/手机号已遮盖，业务短语保持原文）。"""
    from docx import Document

    tmp = tempfile.mkdtemp(prefix="issue21_out2_")
    try:
        out = os.path.join(tmp, "out.docx")
        redact_all.redact_file(docx_file, out)
        texts = [p.text for p in Document(out).paragraphs]
        joined = "\n".join(texts)
        assert "李强" not in joined, f"真名未脱敏：{texts}"
        assert "张三" not in joined, f"真名未脱敏：{texts}"
        assert "13800138000" not in joined, f"手机号未脱敏：{texts}"
        # 业务短语不得被误伤（#51 零误伤口径）：
        #   纯业务短语保持原文；「需求组张经理汇报进展」按 #52 §3.2 口径
        #   （姓 + 职务后缀 = 称谓形态人名）脱敏为「需求组XXX汇报进展」，
        #   这是上游 #51 §4.1 明确要求的回归用例。
        assert "需求组XXX汇报进展" in joined, f"称谓形态未脱敏：{texts}"
        assert "张经理" not in joined, f"称谓形态未脱敏：{texts}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_auto_gray_exports_decisions_pending(docx_file):
    """`--auto-gray` 必须导出 decisions_pending.json 留痕（flag 不再形同虚设）。"""
    tmp = tempfile.mkdtemp(prefix="issue21_ag_")
    try:
        out = os.path.join(tmp, "out.docx")
        pending_path = redact_all.export_auto_gray_decisions(docx_file, out)
        assert pending_path and os.path.exists(pending_path), "未导出 decisions_pending.json"
        with open(pending_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("mode") == "auto-gray", data
        assert "threshold" in data, data
        for item in data.get("pending", []):
            assert item.get("decision") == "redact", f"灰区未自动裁决为遮盖：{item}"
            assert item.get("auto_adjudicated") is True, f"缺少自动裁决标记：{item}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_auto_gray_does_not_change_redaction_result(docx_file):
    """auto-gray 只做留痕，不得改变脱敏结果本身（灰区仍照常遮盖）。"""
    from docx import Document

    tmp = tempfile.mkdtemp(prefix="issue21_cmp_")
    try:
        out_a = os.path.join(tmp, "plain.docx")
        out_b = os.path.join(tmp, "auto.docx")
        redact_all.redact_file(docx_file, out_a)
        redact_all.export_auto_gray_decisions(docx_file, out_b)
        redact_all.redact_file(docx_file, out_b, auto_gray=True)
        text_a = "\n".join(p.text for p in Document(out_a).paragraphs)
        text_b = "\n".join(p.text for p in Document(out_b).paragraphs)
        assert text_a == text_b, f"auto-gray 改变了脱敏结果：\n{text_a!r}\n{text_b!r}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
