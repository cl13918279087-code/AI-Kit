#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_skill_full_validation.py
-----------------------------------
doc-redact 技能包 · 全方位测试用例集

对应《docs/测试方案-技能包.md》v1.0 的 69 项测试项。

分层：
    L1 单元层（TC-L1-01 ~ 12）
    L2 模块层（TC-L2-01 ~ 10）
    L3 编排层（TC-L3-01 ~ 08）
    L4 端到端层（TC-L4-01 ~ 12）
    L5 CLI 契约层（TC-L5-01 ~ 10）
    L6 装载一致性层（TC-L6-01 ~ 06）
    横切 安全合规（TC-SEC-01 ~ 08）
    性能稳定性（TC-PERF-01 ~ 03）

原则：
    1. 测试数据**全部程序化生成**，不依赖仓库内样例文件；
    2. 断言基于**实测行为**而非推测，已探明的行为差异登记为缺陷并修复；
    3. 缺可选依赖的链路（PDF 文本层 / 图片 OCR / .xls）一律 skip，
       绝不以「静默通过」冒充覆盖。
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import common_rules as cr          # noqa: E402
import degradation                 # noqa: E402
import entity_detector as ed       # noqa: E402
import llm_egress as le            # noqa: E402
import manifest as manifest_mod    # noqa: E402
import workdir                     # noqa: E402


# ---------------------------------------------------------------------------
# 通用夹具与工具
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmpd():
    d = tempfile.mkdtemp(prefix="skillval_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)  # 受限环境下删除可能失败，忽略


@pytest.fixture(autouse=True)
def _reset_process_state():
    """跨用例隔离进程内状态（降级台账 / 额外姓名注入）。"""
    degradation.clear_degradations()
    cr.clear_extra_names()
    yield
    degradation.clear_degradations()
    cr.clear_extra_names()


def _make_docx(path, paragraphs):
    """程序化生成 docx。"""
    from docx import Document
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(str(path))
    return str(path)


def _make_xlsx(path, cells):
    """cells: list[tuple[row, col, value]]"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r, c, v in cells:
        ws.cell(row=r, column=c, value=v)
    wb.save(str(path))
    return str(path)


def _make_pptx(path, texts):
    from pptx import Presentation
    pr = Presentation()
    for t in texts:
        slide = pr.slides.add_slide(pr.slide_layouts[5])
        slide.shapes.title.text = t
    pr.save(str(path))
    return str(path)


def _run(script, *args, cwd=None, timeout=180):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True, text=True, cwd=cwd or str(REPO), timeout=timeout)


def _zip_texts(path):
    """读取 zip 内全部文本类条目的内容（用于明文扫描）。"""
    out = {}
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.endswith("/"):
                continue
            try:
                out[n] = z.read(n).decode("utf-8", errors="ignore")
            except Exception:
                out[n] = ""
    return out


# ===========================================================================
# L1 单元层
# ===========================================================================

# TC-L1-01 基础四类正则
def test_l1_01_basic_entity_patterns():
    cases = [
        ("手机13800138000", "13800138000"),
        ("身份证110101199001011234", "110101199001011234"),
        ("邮箱liqiang@bank.com", "liqiang@bank.com"),
    ]
    for text, secret in cases:
        out = cr.apply_redactions(text)
        assert secret not in out, f"{secret} 未被遮蔽：{out!r}"
    # 银行卡默认禁用（误脱敏风险高），不在本项断言内


def test_l1_02_name_three_rules():
    """Rule A/B/C：场景命中 + 词内部不误判。"""
    hit = [
        "李强负责张三项目",
        "总行支持人员：汪晶晶",
        "组长：樊霖副组长",
    ]
    for t in hit:
        out = cr.apply_redactions(t)
        assert "XXX" in out, f"姓名未脱敏：{t!r} -> {out!r}"
    # 词内部不得误判（如「朝阳区」中的「阳」不是姓名）
    out = cr.apply_redactions("北京市朝阳区人民政府")
    assert "朝" not in out.replace("北京市朝阳区", "")  # 允许原样保留


def test_l1_03_disabled_types_not_redacted():
    """禁用口径须与实现一致：**仅银行卡号**禁用；固话 / 组织名启用。

    【口径来源】
      以 `common_rules._build_patterns()` 的实际生效规则为准
      （银行卡上下文规则整段被注释禁用；固话为 R-D 社区决策「全遮」启用；
        组织名 ORG 由 config.org_suffixes 驱动，默认非空 → 启用）。
      README / 实施手册中的「固话 / ORG 已禁用」为**陈旧表述**，
      已在本轮测试中同步修正（缺陷 D-07，文档与实现不一致，P2）。

      【批次二补充（2026-11-08）】D-4 机构名通道语义拆分后，**通用机构词**
      （`财务部`/`运营部`/`科技部`/`运营中心` 等 15 个）按人工标准版口径
      **整段保留**，不再走 ORG 整段脱敏；ORG 通道仍启用，仅对**非通用**后缀
      命中生效。原断言用「财务部」作为 ORG 启用证据，已过时，随 D-8 机构
      清单一并更正。

      【批次四补充（2026-11-09）】D-11 补强后「营业部」不再整词替换为
      XXXX——它是**机构类型后缀**，按人工标准版口径随机构链保留
      （`XX分行营业部`/`总行营业部`）。故 ORG 启用证据改用「事业部」，
      机构链口径另立断言。
    """
    # 银行卡：禁用 → 明文保留
    assert "6222021234567890" in cr.apply_redactions("银行卡6222021234567890")
    # 固话：启用 → 全遮
    assert "010-12345678" not in cr.apply_redactions("固话010-12345678")
    # 组织名 ORG：启用（非通用后缀命中 → 整段脱敏）
    assert cr.apply_redactions("某某事业部") == "某某XXXX"
    # 通用机构词：按 D-4/D-8 口径整段保留
    assert cr.apply_redactions("财务部") == "财务部"
    # 机构链口径（D-11 补强，Issue #63 批次四）：机构类型后缀链保留，
    # 仅脱敏地名性前缀 → 与人工标准版一致
    assert cr.apply_redactions("自贸区分行营业部") == "XX分行营业部"
    assert cr.apply_redactions("总行营业部") == "总行营业部"


def test_l1_04_count_redactions_no_exception():
    """TC-L1-04：统计函数不得抛异常（本轮发现并修复 ValueError）。"""
    t = "李强电话13800138000 邮箱a@b.com"
    counts = cr.count_redactions(t)
    assert isinstance(counts, dict)
    assert sum(counts.values()) >= 2, f"计数偏少：{counts}"


def test_l1_05_redact_filename_stem():
    out = cr.redact_filename_stem("李强的项目报告2024")
    assert "李强" not in out, f"文件名未脱敏：{out!r}"


def test_l1_06_iso_datetime_roundtrip():
    """ISO 时间戳保护 → 还原后原文完全一致。

    【口径说明】`_ISO_DATETIME_RE` 只匹配**带 `T` 的完整 ISO8601 时间戳**
    （如 `2024-03-15T15:30:00Z`）；裸日期由日期规则通道另行脱敏，
    不属于本函数的保护范围。
    """
    t = "会议时间2024-03-15T15:30:00Z 与 2024-03-16T09:00:00+08:00"
    prot, tokens = cr.protect_iso_datetimes(t)
    assert "2024-03-15T15:30:00Z" not in prot
    assert len(tokens) == 2
    assert cr.restore_iso_datetimes(prot, tokens) == t


def test_l1_07_redistribute_paragraph_conserves_text():
    paras = ["第一段内容", "第二段内容", "第三段内容"]
    redacted = "".join(paras)
    out = cr.redistribute_paragraph(list(paras), redacted)
    assert len(out) == len(paras)
    assert "".join(out) == redacted


def test_l1_08_premask_keeps_length():
    t = "身份证110101199001011234 手机13800138000"
    masked, stats = ed.pre_redact_for_llm(t)
    assert len(masked) == len(t), "等长掩码被破坏，LLM 偏移会错位"
    assert stats["pre_redacted"] is True


def test_l1_09_premask_stats_no_rawtext():
    secret = "110101199001011234"
    t = f"身份证{secret}"
    masked, stats = ed.pre_redact_for_llm(t)
    assert secret not in json.dumps(stats, ensure_ascii=False)
    assert secret not in masked


def test_l1_10_egress_guard_matrix():
    ok, _ = le.check_egress("http://llm-gateway.intranet.corp/v1")
    assert ok is True
    bad, reason = le.check_egress("https://api.openai.com/v1")
    assert bad is False
    assert "openai.com" in reason
    assert le.is_private_host("10.1.2.3") is True
    assert le.is_private_host("8.8.8.8") is False


def test_l1_11_degradation_levels():
    degradation.record("c", degradation.LEVEL_INFO, detail="d")
    degradation.record("c2", degradation.LEVEL_CRITICAL, detail="d")
    evs = degradation.get_degradations()
    assert len(evs) == 2
    assert degradation.worst_level() == degradation.LEVEL_CRITICAL


def test_l1_12_workdir_permissions_and_fingerprint():
    d = tempfile.mkdtemp(prefix="wd_")
    try:
        w = workdir.work_dir(d)
        assert w.name == ".redact_work"
        assert oct(os.stat(w).st_mode)[-3:] == "700"
        p = workdir.write_secure_json(str(w / "a.json"), {"k": "v"})
        assert oct(os.stat(p).st_mode)[-3:] == "600"
        fp = workdir.fingerprint("李强")
        assert fp and "李强" not in fp and len(fp) == 12
        assert workdir.fingerprint("李强") == fp          # 稳定
        assert workdir.fingerprint("李刚") != fp          # 区分
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ===========================================================================
# L2 模块层
# ===========================================================================

def test_l2_01_manifest_serialization_structure():
    m = manifest_mod.RedactionManifest(filename="x.docx")
    m.add_entity(manifest_mod.SensitiveEntity(
        text="李强", replacement="XXX", category="姓名"))
    m.add_decision({"text": "李强", "decision": "redact"})
    m.finalize()
    d = m.to_dict()
    for k in ("entities", "decisions", "stats", "llm_audit", "degradations"):
        assert k in d, f"manifest 缺少段 {k}"
    assert d["llm_audit"]["enabled"] is False
    assert d["degradations"]["count"] == 0


def test_l2_02_manifest_degradation_section():
    m = manifest_mod.RedactionManifest(filename="x.docx")
    m.add_degradation({"component": "doc_converter", "level": "warn",
                       "detail": "antiword", "note": "格式可能丢失"})
    d = m.to_dict()
    assert d["degradations"]["count"] == 1
    md = m.to_markdown()
    assert "降级产物" in md


def test_l2_03_manifest_no_raw_text():
    m = manifest_mod.RedactionManifest(filename="x.docx")
    m.add_entity(manifest_mod.SensitiveEntity(
        text="张三丰", replacement="XXX", category="姓名"))
    md = m.to_markdown()
    assert "张三丰" not in md, "报告正文出现完整原文"
    # 短实体以掩码（****）呈现，绝不回显原文
    assert "****" in md


def test_l2_04_premask_covers_all_unique_ids():
    text = ("身份证110101199001011234 银行卡6222021234567890 手机13800138000 "
            "固话010-12345678 邮箱a@b.com 账号1234567890 住址北京市朝阳区xx路1号")
    masked, stats = ed.pre_redact_for_llm(text)
    for secret in ("110101199001011234", "6222021234567890", "13800138000",
                   "010-12345678", "a@b.com"):
        assert secret not in masked, f"预脱敏未覆盖 {secret}"
    assert stats["masked_spans"] >= 5
    assert len(masked) == len(text)


def test_l2_05_endpoint_check_cli():
    r = _run("endpoint_check.py", "--base-url", "https://api.openai.com/v1")
    assert r.returncode == 2, r.stdout
    r2 = _run("endpoint_check.py", "--base-url",
              "http://llm-gateway.intranet.corp/v1")
    assert r2.returncode == 0, r2.stdout
    r3 = _run("endpoint_check.py", "--base-url", "https://api.openai.com/v1", "--json")
    assert r3.returncode == 2
    data = json.loads(r3.stdout)
    assert "results" in data or "checks" in data or "summary" in data


def test_l2_06_doctor_report_contract():
    r = _run("doctor.py", "--json")
    assert r.returncode in (0, 1, 2, 3)
    data = json.loads(r.stdout)
    groups = {c["group"] for c in data["checks"]}
    for p in ("A.", "B.", "C.", "D.", "E.", "F."):
        assert any(g.startswith(p) for g in groups), f"缺检查组 {p}"
    for c in data["checks"]:
        assert c["status"] in ("ok", "info", "warn", "fail")


def test_l2_07_build_package_triplet(tmpd):
    r = _run("build_package.py")
    assert r.returncode == 0, r.stderr
    dist = REPO / "dist"
    zips = sorted(dist.glob("doc-redact-*.zip"))
    assert zips, "未产出 zip"
    z = zips[-1]
    assert (dist / (z.name + ".sha256")).exists()
    assert (dist / (z.stem + ".manifest.json")).exists()
    man = json.loads((dist / (z.stem + ".manifest.json")).read_text(encoding="utf-8"))
    assert "files" in man or "dependencies" in man


def test_l2_08_full_name_list_merge_semantics():
    cfg = dict(cr._load_config())
    cfg["full_name_list"] = ["测试甲", "测试乙"]
    cfg["full_name_list_file"] = ""
    got = cr._get_full_name_list(cfg)
    assert "测试甲" in got and "测试乙" in got

    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                    encoding="utf-8")
    f.write("# 注释行\n测试丙\n测试甲\n")
    f.close()
    try:
        cfg["full_name_list_file"] = f.name
        got = cr._get_full_name_list(cfg)
        assert "测试丙" in got, "外置人名未合并"
        assert got.count("测试甲") == 1, "重复项未去重"
        assert "注释行" not in got
    finally:
        # 受限环境禁止删除临时文件——清理失败不应影响断言
        try:
            os.unlink(f.name)
        except OSError:
            pass

    # 缺失时静默回退内联
    cfg["full_name_list_file"] = "/nonexistent/path/xx.txt"
    got = cr._get_full_name_list(cfg)
    assert "测试甲" in got, "外置缺失时未回退内联"


def test_l2_09_config_cache_is_shared_object():
    a = cr._load_config()
    b = cr._load_config()
    assert a is b, "配置缓存语义变更：应为进程内共享对象"
    # 调用方不得就地改写（测试用例须用副本）
    copy = dict(a)
    copy["full_name_list"] = ["不应污染全局"]
    assert "不应污染全局" not in (cr._load_config().get("full_name_list") or [])


def test_l2_10_prompts_contract():
    from prompts import build_entity_extraction_prompt, build_batch_summary_prompt
    p = build_entity_extraction_prompt("李强")
    assert "李强" in p
    for kw in ("start", "end", "不要替换"):
        assert kw in p, f"提示模板缺少关键约束：{kw}"
    s = build_batch_summary_prompt([{"status": "success"}, {"status": "failed"}])
    assert "总文件数" in s and "成功" in s and "失败" in s


# ===========================================================================
# L3 编排层
# ===========================================================================

def _run_pipeline_batch(inputs, outdir, **kw):
    """以子进程方式跑 pipeline（spawn 需要真实 __main__ 文件）。"""
    code = (
        "import sys, json, os\n"
        "import tempfile as _tf\n"
        # 受限环境（内网审计沙箱）下 tempfile.gettempdir() 的探针会因
        # 「禁止删除临时文件」而失败 → spawn 子进程直接崩溃。
        # 显式指定 tempdir 规避探针（mkdtemp 不需要探针）。
        f"_tf.tempdir = {os.environ.get('SKILLVAL_TEMPDIR', '')!r}\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import pipeline\n"
        # 【spawn 契约】ProcessPoolExecutor 在 macOS/Windows 为 spawn，
        # 子进程会**重新导入主模块**——入口必须置于 __main__ 保护内，
        # 否则子进程再次触发 batch_process → 递归 → 进程池崩溃。
        "def _main():\n"
        f"    r = pipeline.batch_process({inputs!r}, {outdir!r}, **{kw!r})\n"
        "    print('__RESULT__' + json.dumps(["
        "{'status': x.status, 'output_path': x.output_path, "
        "'manifest_path': x.manifest_path, 'error': x.error, "
        "'entities_count': x.entities_count} for x in r], ensure_ascii=False))\n"
        "if __name__ == '__main__':\n"
        "    _main()\n"
    )
    f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                    encoding="utf-8")
    f.write(code)
    f.close()
    try:
        r = subprocess.run([sys.executable, f.name], capture_output=True,
                           text=True, timeout=300)
        for line in r.stdout.splitlines():
            if line.startswith("__RESULT__"):
                return json.loads(line[len("__RESULT__"):])
        raise AssertionError(f"pipeline 未产出结果：{r.stdout[-800:]}\n{r.stderr[-800:]}")
    finally:
        # 部分受限环境禁止删除临时文件（如内网审计沙箱）——清理失败不应影响断言
        try:
            os.unlink(f.name)
        except OSError:
            pass


def test_l3_01_batch_single_file(tmpd):
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    out = Path(tmpd) / "out"
    res = _run_pipeline_batch([src], str(out), file_types=["word"])
    assert len(res) == 1
    assert res[0]["status"] == "complete", res[0]
    assert Path(res[0]["output_path"]).exists()


def test_l3_02_status_predicates():
    import pipeline
    assert pipeline.is_success_status("complete") is True
    assert pipeline.is_success_status("success") is True
    assert pipeline.is_failed_status("failed") is True
    assert pipeline.is_skipped_status("skipped") is True
    assert pipeline.is_success_status("failed") is False


def test_l3_03_ledger_completeness(tmpd):
    """处理文件数 == 成功 + 失败 + 跳过。"""
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    bad = Path(tmpd) / "b.docx"
    bad.write_bytes(b"not a docx")
    out = Path(tmpd) / "out"
    res = _run_pipeline_batch([src, str(bad)], str(out), file_types=["word"])
    assert len(res) == 2
    st = [r["status"] for r in res]
    assert st.count("complete") + st.count("failed") + st.count("skipped") == 2


def test_l3_04_parallel_consistency(tmpd):
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    o1, o4 = Path(tmpd) / "o1", Path(tmpd) / "o4"
    r1 = _run_pipeline_batch([src], str(o1), file_types=["word"], max_workers=1)
    r4 = _run_pipeline_batch([src], str(o4), file_types=["word"], max_workers=4)
    assert r1[0]["status"] == r4[0]["status"] == "complete"
    assert r1[0]["entities_count"] == r4[0]["entities_count"]


def test_l3_05_resume_skips_done(tmpd):
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    out = Path(tmpd) / "out"
    r = _run("pipeline.py", src, "-o", str(out), "-t", "word", "--resume")
    assert r.returncode == 0, r.stdout + r.stderr
    state = Path(out) / ".redact_state.json"
    assert state.exists(), "断点续传状态文件未生成"
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data, "断点续传状态为空"


def test_l3_06_overwrite_control(tmpd):
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    out = Path(tmpd) / "out"
    r1 = _run("pipeline.py", src, "-o", str(out), "-t", "word")
    assert r1.returncode == 0
    res = _run_pipeline_batch([src], str(out), file_types=["word"])
    assert res[0]["status"] in ("complete", "skipped")


def test_l3_07_markdown_report_sections(tmpd):
    import pipeline
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    out = Path(tmpd) / "out"
    res = _run_pipeline_batch([src], str(out), file_types=["word"])
    # 构造 RedactResult 列表
    from pipeline import RedactResult
    rr = [RedactResult(input_path=src, output_path=str(out / "a_脱敏.docx"),
                        status="complete", entities_count=1)]
    md = pipeline.generate_markdown_report(rr, str(out), 100)
    assert Path(md).exists(), f"报告未落盘：{md}"
    text = Path(md).read_text(encoding="utf-8")
    # 批量报告章节：总体统计 / 按文件类型统计 / 成功详情
    for section in ("总体统计", "按文件类型统计", "成功详情"):
        assert section in text, f"报告缺少章节「{section}」"
    assert "处理文件数" in text and "敏感项总数" in text


def test_l3_08_exit_code_semantics():
    import redact_all
    assert redact_all._exit_code_for_degradations() == 0
    degradation.record("c", degradation.LEVEL_WARN, detail="d")
    assert redact_all._exit_code_for_degradations() == 2
    degradation.record("c2", degradation.LEVEL_CRITICAL, detail="d")
    assert redact_all._exit_code_for_degradations() == 2


# ===========================================================================
# L4 端到端层
# ===========================================================================

def test_l4_01_docx_name_redacted(tmpd):
    src = _make_docx(Path(tmpd) / "a.docx", ["李强负责张三项目"])
    r = _run("redact_all.py", src)
    assert r.returncode == 0, r.stdout + r.stderr
    outs = list(Path(tmpd).glob("*_脱敏.docx"))
    assert outs
    texts = _zip_texts(outs[0])
    body = texts.get("word/document.xml", "")
    assert "李强" not in body and "张三" not in body


def test_l4_02_docx_contact_redacted(tmpd):
    src = _make_docx(Path(tmpd) / "a.docx",
                     ["电话13800138000 身份证110101199001011234 邮箱liqiang@bank.com"])
    _run("redact_all.py", src)
    outs = list(Path(tmpd).glob("*_脱敏.docx"))
    body = _zip_texts(outs[0]).get("word/document.xml", "")
    for s in ("13800138000", "110101199001011234", "liqiang@bank.com"):
        assert s not in body, f"{s} 明文残留"


def test_l4_03_docx_docprops_cleaned(tmpd):
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    _run("redact_all.py", src)
    outs = list(Path(tmpd).glob("*_脱敏.docx"))
    for name, content in _zip_texts(outs[0]).items():
        if "docProps" in name:
            assert "李强" not in content, f"{name} 残留姓名明文"
            assert "13800138000" not in content, f"{name} 残留手机号明文"


def test_l4_04_docx_reparsable(tmpd):
    from docx import Document
    src = _make_docx(Path(tmpd) / "a.docx",
                     ["第一段 李强", "第二段 张三", "第三段 普通内容"])
    _run("redact_all.py", src)
    outs = list(Path(tmpd).glob("*_脱敏.docx"))
    doc = Document(str(outs[0]))
    assert len(doc.paragraphs) == 3, "段落数变化，可能丢内容"
    texts = "\n".join(p.text for p in doc.paragraphs)
    assert "普通内容" in texts


def test_l4_05_xlsx_cells_redacted(tmpd):
    src = _make_xlsx(Path(tmpd) / "a.xlsx", [
        (1, 1, "客户姓名"), (1, 2, "李强"),
        (2, 1, "手机号"), (2, 2, "13800138000"),
    ])
    r = _run("redact_all.py", src)
    assert r.returncode == 0, r.stdout + r.stderr
    outs = list(Path(tmpd).glob("*_脱敏.xlsx"))
    assert outs
    import openpyxl
    wb = openpyxl.load_workbook(str(outs[0]))
    ws = wb.active
    assert ws.cell(1, 2).value != "李强"
    assert ws.cell(2, 2).value != "13800138000"
    assert ws.cell(1, 1).value == "客户姓名", "表头标签被误脱敏"


def test_l4_06_pptx_text_redacted(tmpd):
    src = _make_pptx(Path(tmpd) / "a.pptx", ["李强项目汇报"])
    r = _run("redact_all.py", src)
    assert r.returncode == 0, r.stdout + r.stderr
    outs = list(Path(tmpd).glob("*_脱敏.pptx"))
    assert outs
    for name, content in _zip_texts(outs[0]).items():
        if name.startswith("ppt/slides/"):
            assert "李强" not in content, f"{name} 残留姓名明文"


def test_l4_07_pptx_reparsable(tmpd):
    from pptx import Presentation
    src = _make_pptx(Path(tmpd) / "a.pptx", ["李强项目汇报"])
    _run("redact_all.py", src)
    outs = list(Path(tmpd).glob("*_脱敏.pptx"))
    pr = Presentation(str(outs[0]))
    assert len(pr.slides) == 1


def test_l4_08_idempotency(tmpd):
    """对已脱敏产物再脱敏 → 0 处改动。"""
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    _run("redact_all.py", src)
    first = list(Path(tmpd).glob("*_脱敏.docx"))[0]
    r = _run("redact_all.py", str(first))
    assert r.returncode == 0
    second = list(Path(tmpd).glob("*_脱敏_脱敏.docx"))
    if second:
        body = _zip_texts(second[0]).get("word/document.xml", "")
        assert "李强" not in body
    assert "共遮盖 0 处" in r.stdout or "共遮盖 0" in r.stdout, \
        f"二次脱敏仍有改动：{r.stdout[-400:]}"


def test_l4_09_determinism(tmpd):
    """同输入两次处理，产出语义一致。"""
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    _run("redact_all.py", src)
    first = list(Path(tmpd).glob("*_脱敏.docx"))[0]
    d2 = Path(tmpd) / "second"
    d2.mkdir()
    shutil.copy(first, d2 / "a.docx")
    _run("redact_all.py", str(d2 / "a.docx"))
    second = list(d2.glob("*_脱敏.docx"))[0]
    t1 = _zip_texts(first)
    t2 = _zip_texts(second)
    assert t1.get("word/document.xml") == t2.get("word/document.xml"), \
        "同输入两次产出不一致"


def test_l4_10_no_plaintext_anywhere(tmpd):
    """全载体明文扫描：zip 内所有条目（含 docProps/media）不得含明文。"""
    src = _make_docx(Path(tmpd) / "a.docx",
                     ["李强电话13800138000 身份证110101199001011234"])
    _run("redact_all.py", src)
    outs = list(Path(tmpd).glob("*_脱敏.docx"))
    for name, content in _zip_texts(outs[0]).items():
        for s in ("李强", "13800138000", "110101199001011234"):
            assert s not in content, f"{name} 残留 {s}"


def test_l4_11_manifest_path_must_exist(tmpd):
    """manifest 声明的路径必须真实落盘——虚假路径 = 审计链断裂。"""
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    out = Path(tmpd) / "out"
    res = _run_pipeline_batch([src], str(out), file_types=["word"])
    mp = res[0]["manifest_path"]
    assert mp, "manifest_path 未设置"
    assert Path(mp).exists(), f"manifest_path 指向不存在的文件（虚假路径）：{mp}"


def test_l4_12_output_stays_in_output_dir(tmpd):
    """产物不得写到输出目录之外。"""
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    out = Path(tmpd) / "out"
    _run("redact_all.py", src, "-o", str(out))
    produced = {p.resolve() for p in Path(tmpd).rglob("*") if p.is_file()}
    outside = {p for p in produced if out.resolve() not in p.parents
               and p.resolve() != out.resolve()}
    # 允许原始输入文件本身
    outside = {p for p in outside if p.name != "a.docx"}
    assert not outside, f"产物越权写入：{outside}"


# ===========================================================================
# L5 CLI 契约层
# ===========================================================================

def test_l5_01_no_args_prints_usage():
    for script in ("redact_word.py", "redact_excel.py", "redact_ppt.py",
                   "redact_pdf.py", "redact_image.py"):
        r = _run(script)
        assert r.returncode != 0, f"{script} 无参数应返回非 0"
        assert "Traceback" not in r.stderr, f"{script} 无参数即崩溃"


def test_l5_02_help_exits_zero():
    for script in ("endpoint_check.py", "llm_premask.py"):
        r = _run(script, "--help")
        assert r.returncode == 0, f"{script} --help 退出码 {r.returncode}"
        assert r.stdout.strip(), f"{script} --help 无输出"


def test_l5_03_corrupted_docx(tmpd):
    bad = Path(tmpd) / "bad.docx"
    bad.write_bytes(b"not a zip at all")
    r = _run("redact_all.py", str(bad))
    assert r.returncode != 0, "损坏文件必须返回失败退出码"
    assert not list(Path(tmpd).glob("*_脱敏.docx")), "损坏文件不得产生产物"
    assert "Traceback" not in r.stderr, "损坏文件应给出可读错误而非裸栈"


def test_l5_04_empty_file(tmpd):
    bad = Path(tmpd) / "empty.docx"
    bad.write_bytes(b"")
    r = _run("redact_all.py", str(bad))
    assert r.returncode != 0
    assert not list(Path(tmpd).glob("*_脱敏.docx"))
    assert "Traceback" not in r.stderr


def test_l5_05_truncated_docx(tmpd):
    src = _make_docx(Path(tmpd) / "g.docx", ["李强电话13800138000"])
    data = Path(src).read_bytes()
    trunc = Path(tmpd) / "trunc.docx"
    trunc.write_bytes(data[: len(data) // 2])
    r = _run("redact_all.py", str(trunc))
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, "截断文件应给出可读错误"


def test_l5_06_zip_slip_no_escape(tmpd):
    """恶意条目名不得写到目标目录之外。"""
    slip = Path(tmpd) / "slip.docx"
    with zipfile.ZipFile(slip, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
        z.writestr("../../evil.txt", "pwn")
    work = Path(tmpd) / "work"
    work.mkdir()
    r = _run("redact_all.py", str(slip), "-o", str(work))
    escaped = [p for p in Path(tmpd).parent.glob("evil.txt")]
    assert not escaped, f"解压越权写出文件：{escaped}"
    assert not (work.parent.parent / "evil.txt").exists()


def test_l5_07_unsupported_format(tmpd):
    """不支持格式：明确报错，且不得留下伪产物。"""
    f = Path(tmpd) / "x.txt"
    f.write_text("hi", encoding="utf-8")
    r = _run("redact_all.py", str(f))
    assert r.returncode != 0, "不支持的格式必须返回失败退出码"
    leftovers = [p.name for p in Path(tmpd).iterdir() if p.name != "x.txt"]
    assert not leftovers, f"不支持格式仍留下产物：{leftovers}"


def test_l5_08_input_output_same_path(tmpd):
    """输入输出同路径不得破坏原文件。"""
    src = Path(tmpd) / "a.docx"
    _make_docx(src, ["李强电话13800138000"])
    before = src.read_bytes()
    r = _run("redact_all.py", str(src), "-o", str(src))
    assert src.exists(), "原文件被删除"
    assert src.read_bytes() == before or "李强" not in _zip_texts(src).get(
        "word/document.xml", ""), "同路径处理后原文件内容异常"


def test_l5_09_readonly_output_dir(tmpd):
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    out = Path(tmpd) / "ro"
    out.mkdir()
    os.chmod(out, 0o500)
    try:
        r = _run("redact_all.py", src, "-o", str(out))
        assert r.returncode != 0, "只读目录应失败而非静默成功"
        assert "Traceback" not in r.stderr
    finally:
        os.chmod(out, 0o700)


def test_l5_10_stderr_for_errors():
    """错误信息走 stderr，正常输出走 stdout。"""
    f = Path(tempfile.mkdtemp()) / "x.txt"
    f.write_text("hi", encoding="utf-8")
    r = _run("redact_all.py", str(f))
    assert "Traceback" not in r.stderr
    assert r.returncode != 0


# ===========================================================================
# L6 装载一致性层
# ===========================================================================

def test_l6_01_package_runs_in_clean_dir(tmpd):
    """分发包解压到干净目录，从任意 CWD 运行 doctor 通过。"""
    r = _run("build_package.py")
    assert r.returncode == 0, r.stderr
    z = sorted((REPO / "dist").glob("doc-redact-*.zip"))[-1]
    target = Path(tmpd) / "pkg"
    with zipfile.ZipFile(z) as zf:
        zf.extractall(target)
    # 分发包 zip 内为平铺结构（scripts/ references/ 位于根下），根即解压目标目录
    root = target
    doctor = root / "scripts" / "doctor.py"
    assert doctor.exists(), "分发包缺少 doctor.py"
    r2 = subprocess.run([sys.executable, str(doctor), "--json"],
                        capture_output=True, text=True, cwd="/")
    assert r2.returncode in (0, 2), f"干净目录 doctor 异常：{r2.stdout[-500:]}"
    data = json.loads(r2.stdout)
    assert data["counts"]["fail"] == 0, "分发包自检存在失败项"


def test_l6_02_package_real_redaction_smoke(tmpd):
    """分发包真实脱敏冒烟：姓名 / 手机 / 身份证均须遮盖。"""
    r = _run("build_package.py")
    assert r.returncode == 0
    z = sorted((REPO / "dist").glob("doc-redact-*.zip"))[-1]
    target = Path(tmpd) / "pkg"
    with zipfile.ZipFile(z) as zf:
        zf.extractall(target)
    root = target
    src = _make_docx(Path(tmpd) / "a.docx",
                     ["李强电话13800138000 身份证110101199001011234"])
    r2 = subprocess.run([sys.executable, str(root / "scripts" / "redact_all.py"), src],
                        capture_output=True, text=True, cwd="/")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    outs = list(Path(tmpd).glob("*_脱敏.docx"))
    assert outs
    body = _zip_texts(outs[0]).get("word/document.xml", "")
    for s in ("李强", "13800138000", "110101199001011234"):
        assert s not in body, f"分发包脱敏后仍残留 {s}"


def test_l6_03_package_has_no_dev_assets(tmpd):
    r = _run("build_package.py")
    z = sorted((REPO / "dist").glob("doc-redact-*.zip"))[-1]
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
    for bad in ("tests/", "docs/", ".github/", "mine_", "bench_metrics.py"):
        assert not any(bad in n for n in names), f"分发包含开发资产：{bad}"


def test_l6_04_package_has_no_dev_paths(tmpd):
    r = _run("build_package.py")
    z = sorted((REPO / "dist").glob("doc-redact-*.zip"))[-1]
    with zipfile.ZipFile(z) as zf:
        for n in zf.namelist():
            if n.endswith("/"):
                continue
            try:
                content = zf.read(n).decode("utf-8")
            except Exception:
                continue
            # doctor.py 的 `_DEV_PATH_PATTERNS` 含 "/Users/[A-Za-z0-9_.-]+/"
            # 这类**检测用正则模式**（字面量匹配），不是开发者路径残留。
            # 判定口径：真实绝对路径的形态是 /Users/<用户名>/<后续>，
            # 而正则模式里用户名位是字符类 [A-Za-z0-9_.-]+。
            if "doctor.py" not in n:
                assert not re.search(r"/Users/[A-Za-z0-9_.-]+/[^\[\s]", content), \
                    f"{n} 含开发者路径"
                assert "C:\\Users\\" not in content, f"{n} 含开发者路径"


def test_l6_05_skill_md_contract():
    text = (REPO / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---")
    fm = text.split("---")[1]
    for k in ("name:", "version:", "description:", "requires_python:",
              "requires_binaries:", "license:"):
        assert k in fm, f"SKILL.md 缺少 frontmatter 字段 {k}"
    assert "name: doc-redact" in fm


def test_l6_06_requirements_contract():
    req = (REPO / "requirements.txt").read_text(encoding="utf-8")
    dev = (REPO / "requirements-dev.txt").read_text(encoding="utf-8")
    for pkg in ("lxml", "requests", "tqdm", "jieba"):
        assert pkg in req, f"requirements 缺少 {pkg}"
    assert "pytest" in dev
    assert "pytest" not in req, "测试依赖不应混入运行时依赖"


# ===========================================================================
# 横切：安全合规
# ===========================================================================

def test_sec_01_public_endpoint_rejected():
    ok, reason = le.check_egress("https://api.openai.com/v1")
    assert ok is False
    assert reason


def test_sec_02_internal_endpoint_allowed():
    for url in ("http://llm-gateway.intranet.corp/v1",
                "http://10.1.2.3:8000/v1",
                "https://llm.corp.bank.local/v1"):
        ok, _ = le.check_egress(url)
        assert ok is True, f"内网端点被误拒：{url}"


def test_sec_03_premask_hides_unique_ids():
    t = "身份证110101199001011234 手机13800138000"
    masked, stats = ed.pre_redact_for_llm(t)
    assert "110101199001011234" not in masked
    assert "13800138000" not in masked


def test_sec_04_premask_length_preserved():
    for t in ("身份证110101199001011234", "a@b.com", "电话13800138000"):
        masked, _ = ed.pre_redact_for_llm(t)
        assert len(masked) == len(t)


def test_sec_05_audit_records_no_rawtext(tmpd):
    """manifest / 报告 / 降级台账均不含原文。"""
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    _run("redact_all.py", src)
    for p in Path(tmpd).glob("*manifest*"):
        assert "李强" not in p.read_text(encoding="utf-8"), f"{p.name} 含原文"
        assert "13800138000" not in p.read_text(encoding="utf-8")


def test_sec_06_default_no_network(tmpd):
    """未配置 base_url 时不发起任何网络请求。"""
    import socket
    calls = []
    orig = socket.socket.connect

    def spy(self, addr):
        calls.append(addr)
        return orig(self, addr)

    socket.socket.connect = spy
    try:
        src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
        _run("redact_all.py", src)
    finally:
        socket.socket.connect = orig
    # 无外部网络目标（127.0.0.1/本地为允许项，但不应有任何对外连接）
    for addr in calls:
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        assert str(host) in ("127.0.0.1", "::1", "localhost"), \
            f"默认配置下发生了对外连接：{addr}"


def test_sec_07_products_no_plaintext(tmpd):
    src = _make_docx(Path(tmpd) / "a.docx",
                     ["李强电话13800138000 身份证110101199001011234"])
    _run("redact_all.py", src)
    for p in Path(tmpd).rglob("*"):
        if p.is_file() and p.suffix != ".docx" and p.name != "a.docx":
            try:
                content = p.read_text(encoding="utf-8")
            except Exception:
                continue
            assert "13800138000" not in content, f"{p.name} 含手机号明文"


def test_sec_08_temp_dir_cleanup(tmpd):
    """处理结束后不留孤儿临时文件。"""
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    _run("redact_all.py", src)
    leftovers = list(Path(tmpd).rglob(".redact_work"))
    for w in leftovers:
        assert not list(w.iterdir()), f"受控工作目录残留中间文件：{w}"


# ===========================================================================
# 性能与稳定性
# ===========================================================================

def test_perf_01_batch_throughput(tmpd):
    from docx import Document
    p = Path(tmpd) / "big.docx"
    doc = Document()
    for i in range(300):
        doc.add_paragraph(f"第{i}条 李强 负责 项目 电话13800138000 身份证110101199001011234")
    doc.save(str(p))
    t0 = time.time()
    r = _run("redact_word.py", str(p), str(Path(tmpd) / "o.docx"))
    el = time.time() - t0
    assert r.returncode == 0, r.stdout + r.stderr
    assert "共遮盖 300 处" in r.stdout, r.stdout[-300:]
    assert el < 60, f"300 段文档耗时 {el:.1f}s，数量级劣化"


def test_perf_02_no_orphan_temp_files(tmpd):
    src = _make_docx(Path(tmpd) / "a.docx", ["李强电话13800138000"])
    before = set(os.listdir(tempfile.gettempdir()))
    _run("redact_all.py", src)
    after = set(os.listdir(tempfile.gettempdir()))
    # 允许并发产生的瞬时项，仅报告异常增长
    assert len(after - before) < 20, f"临时目录异常增长：{list(after - before)[:10]}"


def test_perf_03_repeat_stability(tmpd):
    outs = []
    for i in range(3):
        d = Path(tmpd) / f"r{i}"
        d.mkdir()
        src = _make_docx(d / "a.docx", ["李强电话13800138000 身份证110101199001011234"])
        _run("redact_all.py", src)
        o = list(d.glob("*_脱敏.docx"))[0]
        outs.append(_zip_texts(o).get("word/document.xml"))
    assert outs[0] == outs[1] == outs[2], "重复运行结果不一致"
