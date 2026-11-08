#!/usr/bin/env python3
"""
test_issue58_list_evidence_gate.py - Issue #58 回归守卫（L1 姓名证据层 + 名单通道门控）

【缺陷现象】
名单通道（`_apply_plain_list_pass`）只做**形态**判定——
「整行是 2-4 字 CJK 块 + 顿号」即当名单，于是页脚密级声明、
业务并列术语被整段抹除：

    "内部资料，禁止外传"   → "XXXX，XXXX"   ← 密级声明失效（合规要素丢失）
    "各方代表、专家骨干"   → "XXXX、XXXX"   ← 业务表述被抹
    "报告生成、会议纪要"   → "XXXX、XXXX"

【修复（D-3）】
  ① 落地 L1 姓名证据层：判据四合一
     （D16 全名 ∪ 通用人名词典 ∪ 姓+名用字+尾字字频 hot∪warm ∪ 非护栏词）；
  ② 名单通道接入证据门控 `list_evidence_gate {enable, mode, min_evidence_ratio}`，
     strict 档下证据不足 → **整行保留**。

    「名单的判定应由『名单里的真名』来证明自己，
      而不是由『它长得像名单』来证明自己。」（Issue #58 §三）

运行方式：
    python -m pytest tests/test_issue58_list_evidence_gate.py -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common_rules  # noqa: E402
from common_rules import apply_redactions  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, "scripts", "config.json")


# ---------------------------------------------------------------------------
# 1. L1 姓名证据层
# ---------------------------------------------------------------------------

def test_l1_evidence_on_real_names():
    """真实姓名具备 L1 正向证据。"""
    for w in ("孙海刚", "毛月珍", "朱丽琼", "李杰", "蔡昀煜", "阮秋鸣"):
        assert common_rules._has_name_evidence(w) is True, w


def test_l1_evidence_absent_on_generic_words():
    """通用词 / 职务标签 / 业务术语不具备 L1 证据。"""
    for w in ("内部资料", "禁止外传", "各方代表", "专家骨干", "报告生成",
              "会议纪要", "副行长", "总监", "技术骨干"):
        assert common_rules._has_name_evidence(w) is False, w


def test_l1_evidence_respects_guard_words():
    """护栏词即便形似人名也不得被当作人名证据。"""
    cfg = common_rules._load_config()
    guards = set(cfg.get("standalone_exempt_words") or [])
    if guards:
        sample = sorted(guards)[:20]
        for w in sample:
            if 2 <= len(w) <= 4:
                assert common_rules._has_name_evidence(w) is False, w


# ---------------------------------------------------------------------------
# 2. 名单通道证据门控
# ---------------------------------------------------------------------------

def test_gate_config():
    """门控配置落地且为 strict 档。"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    gate = cfg.get("list_evidence_gate") or {}
    assert gate.get("enable") is True
    assert gate.get("mode") == "strict"
    assert 0 < float(gate.get("min_evidence_ratio", 0)) <= 1


def test_non_name_list_kept():
    """无姓名证据的并列短语：整行保留（密级声明 / 业务术语）。"""
    for src in ("内部资料，禁止外传",
                "各方代表、专家骨干",
                "报告生成、会议纪要"):
        out = apply_redactions(src)
        assert out == src, f"{src!r} → {out!r}（非名单被误抹）"


def test_real_name_list_redacted():
    """确为名单的行：仍然逐块脱敏（门控不得把召回一起关掉）。"""
    cases = [
        ("窦磊、王富生、王根利", "XXX、XXX、XXX"),
        ("郑云、杨瑜", "XXX、XXX"),
    ]
    for src, want in cases:
        out = apply_redactions(src)
        assert out == want, f"{src!r} → {out!r}（期望 {want!r}）"


def test_footer_confidentiality_kept():
    """页脚密级标识保持率 100%（合规要素不得丢失）。"""
    footers = [
        "内部资料，禁止外传",
        "内部资料,禁止外传",
        "内部资料，请勿外传",
        "保密文件，禁止外传",
        "本文档仅供内部使用",
    ]
    for src in footers:
        out = apply_redactions(src)
        assert out == src, f"{src!r} → {out!r}"


def test_evidence_ratio_helper():
    """证据比例计算：命中数 / 总块数。"""
    hit, total = common_rules._list_evidence_ratio("窦磊、王富生、王根利")
    assert total == 3
    assert hit >= 2
    hit, total = common_rules._list_evidence_ratio("内部资料，禁止外传")
    assert total == 2
    assert hit == 0


# ---------------------------------------------------------------------------
# 3. 零劣化
# ---------------------------------------------------------------------------

def test_biz_corpus_no_name_misfire():
    """通用业务语料：姓名类误伤维持 0。"""
    from test_name_false_positive import FALSE_POSITIVE_CORPUS

    name_class = []
    for seg in FALSE_POSITIVE_CORPUS:
        out, cnt = common_rules.apply_redactions_counted(seg)
        if out == seg:
            continue
        if cnt.get("姓名"):
            name_class.append(seg)
    assert not name_class, f"通用语料出现姓名类误伤 {len(name_class)} 条：{name_class[:3]}"


def test_gold_names_still_recalled():
    """真名金标召回不得劣化。"""
    bench = os.path.join(REPO_ROOT, "tests", "benchmark", "gold_names.json")
    with open(bench, encoding="utf-8") as f:
        gold_raw = json.load(f)
    gold = set()
    for v in gold_raw.values():
        if isinstance(v, list):
            gold.update(v)

    missed = []
    for g in gold:
        if not g:
            continue
        if g in apply_redactions(f"负责人：{g}，负责整体推进。"):
            missed.append(g)
    assert not missed, f"金标漏脱 {len(missed)} 个：{missed[:5]}"


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
