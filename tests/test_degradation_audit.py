#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_degradation_audit.py
------------------------------
降级标记贯通审计链 + 预脱敏覆盖面扩展 + 端点自检（架构评审遗留项 1/3/4）

对应整改
    遗留项 1（P0-3）降级标记贯通审计链：
              `.doc` 三级转换降级链（LibreOffice → textutil → antiword）
              产出的 degraded_level/degraded_note 未透传，导致「降级产物
              以成功状态返回」的静默失真问题；
              现已贯通 manifest / 批量报告 / 退出码三处。
    遗留项 3      扩展预脱敏覆盖面：
              原仅覆盖 5 类个人唯一标识，现扩展至账号 / 证件号 / 住址 /
              证件影像引用。
    遗留项 4      Agent 直处理模式端点合规自检能力。

本文件的断言
    1. 降级台账：登记 / 清空 / 最重级别 / 摘要 / 只含元数据不含原文；
    2. manifest：`degradations` 段序列化 + Markdown「降级产物」章节；
    3. 批量报告：出现「⚠️ 降级产物清单」章节并逐条列出；
    4. 退出码语义：0 = 无降级、2 = 有降级；
    5. `.doc` 路径：转换元数据不得混入 counts（此前触发 TypeError 崩溃）；
    6. 预脱敏：新增类别被遮蔽、长度不变、偏移可回写、统计不含原文；
    7. 端点自检：内网放行 / 公网拒绝 / 白名单放行 / 退出码 0 与 2。

运行：
    python3 -m pytest tests/test_degradation_audit.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import degradation  # noqa: E402
import entity_detector  # noqa: E402
import endpoint_check  # noqa: E402
import manifest as manifest_mod  # noqa: E402
import pipeline  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_ledger():
    """每个用例前后清空降级台账，避免用例间串味。"""
    degradation.clear_degradations()
    yield
    degradation.clear_degradations()


@pytest.fixture()
def tmpd():
    d = tempfile.mkdtemp(prefix="degr_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. 降级台账
# ---------------------------------------------------------------------------

def test_ledger_record_and_clear():
    degradation.record("doc_converter", degradation.LEVEL_WARN,
                       detail="textutil", note="格式可能丢失")
    assert len(degradation.get_degradations()) == 1
    degradation.clear_degradations()
    assert degradation.get_degradations() == []


def test_ledger_levels_and_worst():
    degradation.record("a", degradation.LEVEL_INFO)
    degradation.record("b", degradation.LEVEL_WARN)
    degradation.record("c", degradation.LEVEL_CRITICAL)
    assert degradation.worst_level() == degradation.LEVEL_CRITICAL
    assert degradation.has_critical() is True
    degradation.clear_degradations()
    assert degradation.worst_level() is None
    assert degradation.has_critical() is False


def test_ledger_unknown_level_falls_back_to_info():
    ev = degradation.record("x", "不存在的级别")
    assert ev["level"] == degradation.LEVEL_INFO


def test_ledger_summary_shape():
    degradation.record("a", degradation.LEVEL_WARN, detail="d", note="n")
    s = degradation.summarize()
    assert s["count"] == 1
    assert s["worst_level"] == degradation.LEVEL_WARN
    assert s["by_level"][degradation.LEVEL_WARN] == 1


def test_ledger_contains_no_document_content():
    """登记内容只含组件名/级别/原因/复核提示——不含任何文档原文。"""
    secret = "张三的身份证号是110101199001011234"
    ev = degradation.record("doc_converter", degradation.LEVEL_WARN,
                            detail="textutil", note="格式可能丢失")
    blob = json.dumps(ev, ensure_ascii=False)
    assert secret not in blob
    assert "身份证" not in blob  # 原文片段不得进入台账
    assert set(ev.keys()) == {"component", "level", "detail", "note"}


# ---------------------------------------------------------------------------
# 2. manifest 贯通
# ---------------------------------------------------------------------------

def test_manifest_degradations_section():
    m = manifest_mod.RedactionManifest("a.docx")
    assert m.to_dict()["degradations"] == {"count": 0, "items": []}

    m.add_degradation({"component": "doc_converter", "level": "warn",
                       "detail": "textutil", "note": "需人工比对"})
    d = m.to_dict()
    assert d["degradations"]["count"] == 1
    assert d["degradations"]["items"][0]["detail"] == "textutil"


def test_manifest_add_degradations_filters_unknown_fields():
    m = manifest_mod.RedactionManifest("a.docx")
    m.add_degradations([
        {"component": "c", "level": "warn", "detail": "d", "note": "n",
         "evil": "should be dropped"},
    ])
    item = m.to_dict()["degradations"]["items"][0]
    assert "evil" not in item
    assert set(item.keys()) == {"component", "level", "detail", "note"}


def test_manifest_markdown_has_degradation_section():
    m = manifest_mod.RedactionManifest("a.docx")
    m.add_degradation({"component": "doc_converter", "level": "critical",
                       "detail": "antiword", "note": "表格结构丢失"})
    md = m.to_markdown()
    assert "降级产物" in md
    assert "antiword" in md
    assert "表格结构丢失" in md


def test_manifest_markdown_without_degradation_has_no_section():
    m = manifest_mod.RedactionManifest("a.docx")
    md = m.to_markdown()
    assert "降级产物" not in md


# ---------------------------------------------------------------------------
# 3. 批量报告贯通
# ---------------------------------------------------------------------------

def _mk_result(name, degraded_events):
    return pipeline.RedactResult(
        input_path=str(Path("/tmp") / name),
        output_path=str(Path("/tmp") / (name + ".out")),
        status="complete",
        entities_count=3,
        degradations=degraded_events,
    )


def test_batch_report_has_degradation_section(tmpd):
    results = [
        _mk_result("a.docx", [{"component": "doc_converter", "level": "warn",
                                "detail": "textutil", "note": "格式可能丢失"}]),
        _mk_result("b.docx", []),
    ]
    report_path = pipeline.generate_markdown_report(results, str(tmpd))
    md = Path(report_path).read_text(encoding="utf-8")
    assert "降级产物清单" in md
    assert "textutil" in md
    assert "a.docx" in md


def test_batch_report_no_degradation_no_section(tmpd):
    report_path = pipeline.generate_markdown_report(
        [_mk_result("a.docx", [])], str(tmpd))
    md = Path(report_path).read_text(encoding="utf-8")
    assert "降级产物清单" not in md


def test_redact_result_is_degraded_flag():
    ok = _mk_result("a.docx", [])
    bad = _mk_result("b.docx", [{"component": "x", "level": "warn",
                                  "detail": "d", "note": "n"}])
    assert ok.is_degraded is False
    assert bad.is_degraded is True


# ---------------------------------------------------------------------------
# 4/5. 退出码语义与 .doc 元数据隔离
# ---------------------------------------------------------------------------

def _run_cli(args, cwd):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(cwd) / "scripts")
    p = subprocess.run([sys.executable] + args, cwd=str(cwd),
                       capture_output=True, text=True, env=env)
    return p


def test_exit_code_helper_semantics():
    """退出码口径：0 = 无降级；2 = 存在降级产物（须人工复核）。"""
    import redact_all

    degradation.clear_degradations()
    assert redact_all._exit_code_for_degradations() == 0

    degradation.record("doc_converter", degradation.LEVEL_WARN, detail="textutil")
    assert redact_all._exit_code_for_degradations() == 2

    degradation.clear_degradations()
    degradation.record("ppt_converter", degradation.LEVEL_CRITICAL, detail="copy")
    assert redact_all._exit_code_for_degradations() == 2


def test_cli_exit_code_0_for_clean_docx(tmpd):
    """无降级时退出码为 0。"""
    import docx  # python-docx

    src = Path(tmpd) / "clean.docx"
    d = docx.Document()
    d.add_paragraph("本文件不含任何敏感实体，用于验证退出码语义。")
    d.save(str(src))

    out = Path(tmpd) / "out"
    p = _run_cli(
        [str(REPO_ROOT / "scripts" / "redact_all.py"), str(src), "-o", str(out)],
        cwd=REPO_ROOT,
    )
    assert p.returncode == 0, p.stdout + p.stderr


def test_cli_exit_code_1_for_unconvertible_doc(tmpd):
    """转换全链失败的 `.doc` 属**失败**（退出码 1），不是降级（退出码 2）。

    R-④ 契约：全链失败时拒绝生成输出文件（防"假脱敏"泄漏路径），
    因此该场景必须归为失败而非降级。
    """
    src = Path(tmpd) / "broken.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200)
    out = Path(tmpd) / "out"
    p = _run_cli(
        [str(REPO_ROOT / "scripts" / "redact_all.py"), str(src), "-o", str(out)],
        cwd=REPO_ROOT,
    )
    assert p.returncode == 1, p.stdout + p.stderr


def test_doc_metadata_not_mixed_into_counts(tmpd):
    """`.doc` 转换元数据不得混入统计字典（否则 sum() 抛 TypeError）。

    构造一个**真实**的 .doc（textutil 生成）；macOS 之外的环境跳过。
    """
    import shutil as _sh
    if not _sh.which("textutil"):
        pytest.skip("当前环境无 textutil，无法生成真实 .doc 样本")

    src_txt = Path(tmpd) / "sample.txt"
    src_txt.write_text("负责人：张三\n联系电话：13812345678\n", encoding="utf-8")
    src = Path(tmpd) / "sample.doc"
    conv = subprocess.run(["textutil", "-convert", "doc", str(src_txt),
                           "-output", str(src)],
                          capture_output=True, text=True)
    if conv.returncode != 0 or not src.exists():
        pytest.skip("textutil 生成 .doc 失败")

    from redact_word import redact_word
    out = Path(tmpd) / "out.docx"
    try:
        result = redact_word(str(src), str(out))
    except TypeError as exc:  # 修复前的崩溃形态
        pytest.fail(f"`.doc` 路径因元数据混入 counts 而崩溃：{exc}")
    assert isinstance(result, dict)
    for k, v in result.items():
        assert isinstance(v, int), f"统计字典含非 int 值：{k}={v!r}"


# ---------------------------------------------------------------------------
# 6. 预脱敏覆盖面扩展
# ---------------------------------------------------------------------------

def test_premask_new_categories_covered():
    cases = [
        ("银行账号：6222 0202 1234 5678", "ACCOUNT_NO"),
        ("账号: 001234567890123456789", "ACCOUNT_NO"),
        ("护照号 E12345678", "DOC_NO"),
        ("社保号 310101199001011234", "DOC_NO"),
        ("地址：北京市朝阳区建国路88号", "ADDRESS"),
        ("家庭住址：广东省深圳市南山区科技园路1号", "ADDRESS"),
        ("随附身份证正反面.jpg", "CERT_IMAGE"),
    ]
    for text, expect in cases:
        masked, stats = entity_detector.pre_redact_for_llm(text)
        assert expect in stats["categories"], f"{text} 未命中 {expect}：{stats}"
        assert len(masked) == len(text), "掩码后长度变化，偏移语义会失效"


def test_premask_keeps_context_labels():
    """命名组掩码：保留「账号：」等上下文标签，只遮数字。"""
    text = "银行账号：6222021234567890"
    masked, stats = entity_detector.pre_redact_for_llm(text)
    assert masked.startswith("银行账号：")
    assert "6222021234567890" not in masked
    assert stats["categories"]["ACCOUNT_NO"] == 16


def test_premask_offsets_remain_valid():
    """掩码后被遮段的偏移仍指向原文同一位置。"""
    text = "户名张三，账号：6222021234567890，余额100元。"
    masked, stats = entity_detector.pre_redact_for_llm(text)
    start = text.index("6222021234567890")
    assert masked[start:start + 16] == "Ｘ" * 16
    assert stats["masked_chars"] == 16


def test_premask_address_not_over_eaten():
    """住址掩码不得吞掉后续人名上下文。"""
    text = "地址：北京市朝阳区建国路88号，联系人张三"
    masked, _ = entity_detector.pre_redact_for_llm(text)
    assert masked.endswith("联系人张三")
    assert "建国路88号" not in masked


def test_premask_address_keyword_required():
    """无关键词前缀的地址不遮蔽（避免误伤机构地址/正文地名）。"""
    text = "行址：北京市西城区某某路1号"
    masked, stats = entity_detector.pre_redact_for_llm(text)
    assert "ADDRESS" not in stats["categories"]


def test_premask_email_address_not_treated_as_residence():
    text = "邮箱地址 zhangsan@bank.com"
    masked, stats = entity_detector.pre_redact_for_llm(text)
    assert "ADDRESS" not in stats["categories"]
    assert "EMAIL" in stats["categories"]


def test_premask_stats_contain_no_raw_text():
    text = "身份证110101199001011234，手机13812345678"
    masked, stats = entity_detector.pre_redact_for_llm(text)
    blob = json.dumps(stats, ensure_ascii=False)
    assert "110101199001011234" not in blob
    assert "13812345678" not in blob
    assert masked.count("Ｘ") == stats["masked_chars"]


def test_premask_empty_input():
    masked, stats = entity_detector.pre_redact_for_llm("")
    assert masked == ""
    assert stats["pre_redacted"] is False


# ---------------------------------------------------------------------------
# 7. 端点合规自检
# ---------------------------------------------------------------------------

def test_endpoint_check_internal_allowed():
    r = endpoint_check.check_endpoint("http://llm-gw.corp.bank.local/v1", [], True)
    assert r["compliant"] is True
    assert r["private_host"] is True


def test_endpoint_check_public_rejected():
    r = endpoint_check.check_endpoint("https://api.openai.com/v1", [], True)
    assert r["compliant"] is False
    assert r["hint"]


def test_endpoint_check_whitelist_allowed():
    r = endpoint_check.check_endpoint("https://llm.example.com/v1",
                                       ["llm.example.com"], True)
    assert r["compliant"] is True
    assert r["whitelist_hit"] is True


def test_endpoint_check_suffix_whitelist_allowed():
    r = endpoint_check.check_endpoint("https://a.llm.example.com/v1",
                                       ["llm.example.com"], True)
    assert r["compliant"] is True


def test_endpoint_check_empty_url_rejected():
    r = endpoint_check.check_endpoint("", [], True)
    assert r["compliant"] is False


def test_endpoint_check_cli_exit_codes():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "scripts")
    script = str(REPO_ROOT / "scripts" / "endpoint_check.py")

    p = subprocess.run([sys.executable, script, "--base-url",
                        "http://llm-gw.corp.bank.local/v1"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stdout + p.stderr

    p = subprocess.run([sys.executable, script, "--base-url",
                        "https://api.openai.com/v1"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 2, p.stdout + p.stderr

    p = subprocess.run([sys.executable, script, "--base-url",
                        "https://api.openai.com/v1", "--json"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 2
    data = json.loads(p.stdout)
    assert data["compliant"] is False
    assert data["results"][0]["host"] == "api.openai.com"


def test_endpoint_check_cli_no_endpoint_exits_3():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "scripts")
    script = str(REPO_ROOT / "scripts" / "endpoint_check.py")
    p = subprocess.run([sys.executable, script, "--config", "/nonexistent.json"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 3
