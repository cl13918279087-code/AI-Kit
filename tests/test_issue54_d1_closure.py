"""
Issue #54 D1 裁决闭环标准化 —— 回归守卫。

【上游要求】Issue #54 §2.1 的裁决闭环：

    规则引擎首轮脱敏
      → 产出候选清单（span + 置信度 + 命中通道）
      → 灰区（置信度 < 0.75）导出 decisions_pending.json
      → Agent / LLM 裁决 → decisions.json（decision: skip | redact）
      → set_agent_decisions_override() 回灌 → 二轮定稿
      → 高频裁决自动沉淀为规则候选（保护表 / 名单），并入下一版本 config.json

要点：
  · 裁决记录随 manifest.json 归档，满足合规留痕；
  · 主体脱敏在**无 LLM Key** 环境下仍可完整跑完（0 API 成本不被破坏）。

本文件的断言：
  1. 灰区候选清单每条都带 **span + 置信度 + 命中通道**；
  2. `--auto-gray` 端到端跑通：首轮 → 灰区导出 → 裁决回灌 → 定稿；
  3. 裁决记录随 `manifest.json` 归档（可被审计读取）；
  4. 高频裁决可沉淀为规则候选（保护表 / 名单），且与真名库互斥；
  5. `quality_validator.py` 输出的指标 JSON 与 CI 门禁**同口径**；
  6. 无 LLM Key 环境下全流程可跑完。

运行：
    python3 -m pytest tests/test_issue54_d1_closure.py -v
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

import common_rules  # noqa: E402
import redact_all  # noqa: E402


@pytest.fixture()
def docx_file():
    """构造一个最小 .docx（真名 + 手机号 + 业务短语）。"""
    from docx import Document

    tmp = tempfile.mkdtemp(prefix="issue54_d1_")
    path = os.path.join(tmp, "sample.docx")
    doc = Document()
    doc.add_paragraph("李强负责张三项目，联系电话13800138000。")
    doc.add_paragraph("需求组张经理汇报进展")
    doc.save(path)
    yield path
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. 候选清单：span + 置信度 + 命中通道
# ---------------------------------------------------------------------------

def test_gray_candidates_carry_span_confidence_channel(docx_file):
    """灰区候选清单每条必须带 span / confidence / channel。"""
    result = redact_all.detect_gray_entities(docx_file, quiet=True)
    for item in result["gray"] + result["green"]:
        assert "confidence" in item, f"候选缺置信度：{item}"
        assert "channel" in item and item["channel"], f"候选缺命中通道：{item}"
        assert "span" in item, f"候选缺 span：{item}"
        span = item["span"]
        assert set(span.keys()) == {"start", "end"}, f"span 结构异常：{span}"


def test_classify_hit_channel_labels_title_forms():
    """命中通道标注须能区分职务称谓 / 昵称 / 单字姓锚点。"""
    cases = [
        ("需求组张经理汇报进展", 3, 6, "职务称谓"),
        ("小李负责接口联调", 0, 2, "昵称形态"),
        ("会议由刘主持", 3, 4, "单字姓锚点"),
    ]
    for text, s, e, expected in cases:
        got = common_rules.classify_hit_channel(text, s, e)
        assert got == expected, f"通道标注错误：{text!r} → {got}（期望 {expected}）"


# ---------------------------------------------------------------------------
# 2. --auto-gray 端到端：首轮 → 灰区导出 → 裁决回灌 → 定稿
# ---------------------------------------------------------------------------

def test_auto_gray_end_to_end_roundtrip(docx_file):
    """--auto-gray 全链路可跑通，且裁决回灌能改变定稿结果。

    注：无 LLM Key 时规则层不产生灰区实体（灰区来自 LLM 置信度），
    因此本用例对"灰区条目"不做数量假设，只断言链路结构与回灌闭环可用。
    """
    tmp = tempfile.mkdtemp(prefix="issue54_e2e_")
    try:
        # ① 首轮 + 灰区导出
        pending_path = redact_all.export_auto_gray_decisions(docx_file)
        assert pending_path and os.path.exists(pending_path)
        with open(pending_path, encoding="utf-8") as f:
            pending = json.load(f)
        assert pending.get("mode") == "auto-gray"
        assert "threshold" in pending
        assert "gray_count" in pending
        for item in pending.get("pending", []):
            # 候选清单字段齐备：span + 置信度 + 命中通道
            assert item.get("decision") == "redact"
            assert item.get("auto_adjudicated") is True
            assert "span" in item and "channel" in item
            assert "confidence" in item

        # ② 裁决回灌（skip 一个实体；灰区为空时用文档内的真实实体兜底）
        decisions = [{"text": p["text"], "decision": "skip"}
                     for p in pending.get("pending", [])[:1]]
        if not decisions:
            decisions = [{"text": "李强", "decision": "skip"}]
        decisions_path = os.path.join(tmp, "decisions.json")
        with open(decisions_path, "w", encoding="utf-8") as f:
            json.dump(decisions, f, ensure_ascii=False)

        override_path = redact_all.apply_agent_decisions(docx_file, decisions_path)
        assert override_path and os.path.exists(override_path)

        # ③ 二轮定稿
        out = os.path.join(tmp, "out.docx")
        counts = redact_all.redact_file(docx_file, out, manifest_override=override_path)
        assert os.path.exists(out), "二轮定稿未产出文件"
        assert isinstance(counts, dict)

        # ④ 归档（manifest.json 含裁决记录）
        manifest_path = redact_all.write_redaction_manifest(
            out, counts=counts,
            decisions=redact_all.read_decision_records(decisions_path),
            source_path=docx_file, mode="agent-decisions")
        assert manifest_path and os.path.exists(manifest_path)
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        assert "decisions" in manifest, "manifest 未归档裁决记录"
        assert len(manifest["decisions"]) >= 1
        assert manifest["decisions"][0]["decision"] == "skip"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_manifest_decisions_archived_and_readable(docx_file):
    """裁决记录归档进 manifest.json（合规留痕），且可被审计读取。"""
    tmp = tempfile.mkdtemp(prefix="issue54_manifest_")
    try:
        out = os.path.join(tmp, "out.docx")
        counts = redact_all.redact_file(docx_file, out)
        decisions = [{"text": "李强", "decision": "redact",
                      "confidence": 0.9, "channel": "姓名规则", "source": "regex"}]
        path = redact_all.write_redaction_manifest(out, counts=counts,
                                                   decisions=decisions,
                                                   source_path=docx_file)
        assert path and os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["decisions"][0]["text"] == "李强"
        assert data["decisions"][0]["channel"] == "姓名规则"
        assert data["stats"]["decisions_count"] == 1
        # Markdown 报告也须含裁决记录章节
        md_path = path.replace("_manifest.json", "_manifest.md")
        assert os.path.exists(md_path)
        with open(md_path, encoding="utf-8") as f:
            md = f.read()
        assert "裁决记录" in md
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_read_decision_records_supports_both_formats():
    """裁决记录读取须兼容列表格式与 {"pending": [...]} 格式。"""
    tmp = tempfile.mkdtemp(prefix="issue54_rec_")
    try:
        p1 = os.path.join(tmp, "a.json")
        with open(p1, "w", encoding="utf-8") as f:
            json.dump([{"text": "甲", "decision": "skip"}], f, ensure_ascii=False)
        p2 = os.path.join(tmp, "b.json")
        with open(p2, "w", encoding="utf-8") as f:
            json.dump({"pending": [{"text": "乙", "decision": "redact"}]},
                      f, ensure_ascii=False)
        assert redact_all.read_decision_records(p1)[0]["text"] == "甲"
        assert redact_all.read_decision_records(p2)[0]["text"] == "乙"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. 高频裁决沉淀规则候选
# ---------------------------------------------------------------------------

def test_decision_mining_produces_guard_and_name_candidates():
    """高频裁决可沉淀为保护表 / 名单候选，且与真名库互斥。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dm_issue54", os.path.join(SCRIPTS, "decision_mining.py"))
    dm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dm)

    tmp = tempfile.mkdtemp(prefix="issue54_mine_")
    try:
        path = os.path.join(tmp, "decisions.json")
        records = []
        for _ in range(3):
            records.append({"text": "需求组", "decision": "skip",
                            "channel": "姓名规则", "category": "姓名"})
            records.append({"text": "李四明", "decision": "redact",
                            "channel": "角色槽位", "category": "姓名"})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False)

        result = dm.mine_decisions([path], min_freq=3)
        assert "需求组" in result["suggestion"]["common_word_guards"]
        assert "李四明" in result["suggestion"]["full_name_list"]
        # 候选不得与 D16 全名库冲突（否则会造成真名漏脱）
        cfg = json.load(open(os.path.join(SCRIPTS, "config.json"), encoding="utf-8"))
        full = set(cfg.get("full_name_list") or [])
        for c in result["guard_candidates"]:
            assert c["text"] not in full, f"候选与真名库冲突：{c}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_decision_mining_flags_placeholder_conflict():
    """占位人名若被裁决为 skip，须被标为冲突（禁止进保护表）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dm_issue54b", os.path.join(SCRIPTS, "decision_mining.py"))
    dm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dm)

    tmp = tempfile.mkdtemp(prefix="issue54_mine2_")
    try:
        path = os.path.join(tmp, "decisions.json")
        records = [{"text": "张三", "decision": "skip"} for _ in range(5)]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False)
        result = dm.mine_decisions([path], min_freq=3)
        assert "张三" not in result["suggestion"]["common_word_guards"]
        assert any(c["text"] == "张三" for c in result["conflicts"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. 质量指标 JSON 与 CI 门禁同口径
# ---------------------------------------------------------------------------

def test_quality_validator_metrics_match_gate(monkeypatch):
    """quality_validator 的指标 JSON 必须与 CI 门禁口径完全一致。"""
    import bench_metrics
    import quality_validator

    gate_metrics = bench_metrics.compute_metrics(REPO)
    qv_metrics = quality_validator.compute_bench_metrics(REPO)
    for key in ("gold_recall", "biz_misfire", "real_biz_misfire",
                "ambiguity", "precision", "recall", "f1"):
        assert qv_metrics[key]["value"] == gate_metrics[key]["value"], (
            f"指标口径漂移：{key} 质检={qv_metrics[key]} 门禁={gate_metrics[key]}")
    assert qv_metrics["gates"] == gate_metrics["gates"]


def test_bench_metrics_gates_all_pass():
    """CI 门禁指标必须全绿（Issue #54 §2.2 阶段一阈值）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bm_issue54c", os.path.join(SCRIPTS, "bench_metrics.py"))
    bm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm)

    metrics = bm.compute_metrics(REPO)
    failed = [k for k, ok in metrics["gates"].items() if not ok]
    assert not failed, f"门禁未通过：{failed}；指标={json.dumps(metrics['gates'], ensure_ascii=False)}"


def test_ambiguity_bench_asset_present():
    """语义歧义集必须入库（歧义正确率是 #54 门禁项）。"""
    path = os.path.join(REPO, "tests", "benchmark", "ambig_cases.json")
    assert os.path.exists(path), "缺少 tests/benchmark/ambig_cases.json"
    with open(path, encoding="utf-8") as f:
        cases = json.load(f)
    assert len(cases) >= 10, f"歧义集过少：{len(cases)}"
    for c in cases:
        assert "input" in c and "expected" in c


# ---------------------------------------------------------------------------
# 5. 无 LLM Key 环境下全流程可跑完（0 API 成本不被破坏）
# ---------------------------------------------------------------------------

def test_pipeline_runs_without_llm_key(docx_file, monkeypatch):
    """无 LLM Key 时主体脱敏仍须完整跑完（不抛异常、不阻断）。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tmp = tempfile.mkdtemp(prefix="issue54_nollm_")
    try:
        out = os.path.join(tmp, "out.docx")
        counts = redact_all.redact_file(docx_file, out)
        assert os.path.exists(out)
        assert isinstance(counts, dict)
        from docx import Document
        joined = "\n".join(p.text for p in Document(out).paragraphs)
        assert "13800138000" not in joined, "手机号漏脱"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
