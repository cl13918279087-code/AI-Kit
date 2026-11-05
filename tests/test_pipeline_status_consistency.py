#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_pipeline_status_consistency.py
----------------------------------------
Issue P0-1 回归：pipeline.py 产出方与消费方状态值不一致。

历史缺陷
    process_single_file 产出终态 "complete"，而全部消费方（断点续传
    mark_done / mark_failed、进度条统计、Markdown 报告统计、CLI 摘要）
    一律判定 "success"。后果：
      · 批量报告输出「处理 1 / 成功 0 / 失败 0 / 跳过 0」——审计台账三项恒零；
      · 断点续传 mark_done 永不触发，--resume 形同虚设。

修复口径
    1. 终态成功统一取 "complete"（与 redaction_engine.RedactionStatus.COMPLETE 一致）；
    2. 消费方一律经 is_success_status / is_failed_status / is_skipped_status 判定；
    3. "success" 保留为历史别名，兼容外部调用方构造的 RedactResult。

同批覆盖
    P0-2  batch_process API 层输出目录兜底创建；
    台账完整性：被过滤的文件也必须进结果列表，处理文件数 == 成功+失败+跳过。
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import pipeline  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture
def docx_file():
    """构造一个最小 .docx（真名 + 手机号 + 业务短语）。"""
    from docx import Document

    tmp = tempfile.mkdtemp(prefix="pipeline_status_")
    path = os.path.join(tmp, "sample.docx")
    doc = Document()
    doc.add_paragraph("李强负责张三项目，联系电话13800138000。")
    doc.save(path)
    yield path
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. 状态常量与判定函数
# ---------------------------------------------------------------------------

def test_status_constants_defined():
    """终态常量须齐备且取值稳定。"""
    assert pipeline.STATUS_COMPLETE == "complete"
    assert pipeline.STATUS_FAILED == "failed"
    assert pipeline.STATUS_SKIPPED == "skipped"
    for name in ("STATUS_PENDING", "STATUS_DETECTING", "STATUS_APPLYING", "STATUS_WRITING"):
        assert getattr(pipeline, name)


def test_is_success_status_accepts_complete_and_legacy_success():
    """"complete" 与历史别名 "success" 均判成功。"""
    assert pipeline.is_success_status("complete") is True
    assert pipeline.is_success_status("success") is True
    assert pipeline.is_success_status("Complete") is True
    assert pipeline.is_success_status("failed") is False
    assert pipeline.is_success_status("skipped") is False


def test_is_failed_and_skipped_status():
    """失败/跳过的判定互斥且不与成功混淆。"""
    assert pipeline.is_failed_status("failed") is True
    assert pipeline.is_skipped_status("skipped") is True
    assert pipeline.is_failed_status("complete") is False
    assert pipeline.is_skipped_status("complete") is False


def test_status_helpers_accept_enum_like_objects():
    """判定函数须容忍带 .value 的枚举对象（与 RedactionStatus 兼容）。"""

    class FakeStatus:
        value = "complete"

    assert pipeline.is_success_status(FakeStatus()) is True
    assert pipeline._status_value(FakeStatus()) == "complete"


def test_redact_result_properties():
    """RedactResult 的终态属性与判定函数一致。"""
    ok = pipeline.RedactResult(input_path="a", output_path="b", status="complete")
    bad = pipeline.RedactResult(input_path="a", output_path="", status="failed")
    skip = pipeline.RedactResult(input_path="a", output_path="", status="skipped")
    legacy = pipeline.RedactResult(input_path="a", output_path="b", status="success")

    assert ok.is_success and not ok.is_failed and not ok.is_skipped
    assert bad.is_failed and not bad.is_success
    assert skip.is_skipped and not skip.is_success
    assert legacy.is_success


# ---------------------------------------------------------------------------
# 2. 产出方：单文件处理返回规范终态
# ---------------------------------------------------------------------------

def test_process_single_file_returns_canonical_complete(docx_file):
    """成功处理后终态必须是 "complete"，且被判定函数认作成功。"""
    tmp = tempfile.mkdtemp(prefix="pipeline_status_out_")
    try:
        out = Path(tmp) / "out"
        out.mkdir()
        result = pipeline.process_single_file((docx_file, str(out), "word", True))
        assert result.status == pipeline.STATUS_COMPLETE, f"终态异常：{result.status!r}"
        assert result.is_success
        assert not result.is_failed
        assert Path(result.output_path).exists(), "输出文件未落盘"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_process_single_file_failed_terminal_state(tmp_path):
    """异常路径终态必须是 "failed" 且带错误信息。"""
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not-a-real-docx")
    out = tmp_path / "out"
    out.mkdir()
    result = pipeline.process_single_file((str(broken), str(out), "word", True))
    assert result.status == pipeline.STATUS_FAILED
    assert result.is_failed
    assert result.error


# ---------------------------------------------------------------------------
# 3. 消费方：批量统计 / 报告 / 断点续传
# ---------------------------------------------------------------------------

def test_batch_report_counts_success(docx_file, tmp_path):
    """批量跑一份正常文档：报告必须计为「成功 1」，而非三项全零。"""
    out = tmp_path / "out"
    results = pipeline.batch_process([docx_file], str(out), max_workers=1, overwrite=True)

    assert results, "批量处理未产出任何结果"
    assert len(results) == 1
    assert results[0].status == pipeline.STATUS_COMPLETE

    report_path = pipeline.generate_markdown_report(results, str(out))
    text = Path(report_path).read_text(encoding="utf-8")
    assert "**处理文件数**: 1" in text
    assert "**成功**: 1 | **失败**: 0 | **跳过**: 0" in text


def test_report_totals_match_result_count(docx_file, tmp_path):
    """台账恒等式：处理文件数 == 成功 + 失败 + 跳过。"""
    out = tmp_path / "out"
    results = pipeline.batch_process([docx_file], str(out), max_workers=1, overwrite=True)
    report_path = pipeline.generate_markdown_report(results, str(out))
    text = Path(report_path).read_text(encoding="utf-8")

    ok = sum(1 for r in results if r.is_success)
    bad = sum(1 for r in results if r.is_failed)
    skip = sum(1 for r in results if r.is_skipped)
    assert len(results) == ok + bad + skip, "终态归类存在遗漏，存在第五种状态"
    assert f"**处理文件数**: {len(results)}" in text
    assert f"**成功**: {ok} | **失败**: {bad} | **跳过**: {skip}" in text


def test_resume_state_marks_done_on_success(docx_file, tmp_path):
    """断点续传：成功后必须 mark_done（历史缺陷此处永不触发）。"""
    out = tmp_path / "out"
    state_file = tmp_path / "state.json"
    resume = pipeline.ResumeState(str(state_file))

    results = pipeline.batch_process(
        [docx_file], str(out), max_workers=1, overwrite=True, resume_state=resume
    )
    assert results and results[0].is_success

    summary = resume.get_summary()
    assert summary["done"] == 1, f"断点续传未记录完成：{summary}"
    assert summary["failed"] == 0
    assert resume.is_done(docx_file)

    # 二轮：命中续传状态 → 记为跳过，且台账依然对得上账
    results2 = pipeline.batch_process(
        [docx_file], str(out), max_workers=1, overwrite=True, resume_state=resume
    )
    assert len(results2) == 1
    assert results2[0].is_skipped, "已完成的文件应记为跳过"
    assert len(results2) == sum(1 for r in results2 if r.is_success) \
                             + sum(1 for r in results2 if r.is_failed) \
                             + sum(1 for r in results2 if r.is_skipped)


def test_skipped_when_output_exists_and_no_overwrite(docx_file, tmp_path):
    """输出已存在且未 --overwrite → 终态为 skipped，且计入报告「跳过」。"""
    out = tmp_path / "out"
    pipeline.batch_process([docx_file], str(out), max_workers=1, overwrite=True)

    results = pipeline.batch_process([docx_file], str(out), max_workers=1, overwrite=False)
    assert len(results) == 1
    assert results[0].is_skipped, f"期望 skipped，实际 {results[0].status!r}"

    report_path = pipeline.generate_markdown_report(results, str(out))
    text = Path(report_path).read_text(encoding="utf-8")
    assert "**成功**: 0 | **失败**: 0 | **跳过**: 1" in text


def test_failed_counted_as_failed(tmp_path):
    """失败项计入报告「失败」。"""
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not-a-real-docx")
    out = tmp_path / "out"
    out.mkdir()
    results = pipeline.batch_process([str(broken)], str(out), max_workers=1, overwrite=True)
    assert len(results) == 1
    assert results[0].is_failed

    report_path = pipeline.generate_markdown_report(results, str(out))
    text = Path(report_path).read_text(encoding="utf-8")
    assert "**成功**: 0 | **失败**: 1 | **跳过**: 0" in text


# ---------------------------------------------------------------------------
# 4. P0-2：API 层输出目录兜底
# ---------------------------------------------------------------------------

def test_batch_process_creates_output_dir(docx_file, tmp_path):
    """输出目录不存在时自动创建（历史缺陷：每个文件都 FileNotFoundError）。"""
    out = tmp_path / "nested" / "out"
    assert not out.exists()

    results = pipeline.batch_process([docx_file], str(out), max_workers=1, overwrite=True)
    assert out.exists(), "输出目录未被创建"
    assert results and results[0].is_success, f"实际：{results[0].status if results else '无结果'}"
    assert Path(results[0].output_path).exists()


def test_unsupported_type_enters_ledger(tmp_path):
    """不支持的文件类型必须进台账（记失败），不得静默丢弃。"""
    odd = tmp_path / "a.txt"
    odd.write_text("x", encoding="utf-8")
    out = tmp_path / "out"
    results = pipeline.batch_process([str(odd)], str(out), max_workers=1, overwrite=True)
    assert len(results) == 1
    assert results[0].is_failed
    assert "不支持的文件类型" in results[0].error


def test_type_filter_skips_enters_ledger(docx_file, tmp_path):
    """--types 范围外的文件记为跳过并进台账。"""
    out = tmp_path / "out"
    results = pipeline.batch_process(
        [docx_file], str(out), file_types=["excel"], max_workers=1, overwrite=True
    )
    assert len(results) == 1
    assert results[0].is_skipped
    assert "指定类型范围" in results[0].error
