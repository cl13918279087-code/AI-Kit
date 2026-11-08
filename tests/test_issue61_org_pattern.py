#!/usr/bin/env python3
"""
test_issue61_org_pattern.py - Issue #61-B 回归守卫（机构名通道的 L1/L4 守卫与语义拆分）

【缺陷现象】
组织名通道只有一条 G4-S「前缀成词整体保留」规则，语义含混，
两种相反的缺陷同时存在：

  ① **通用机构词误伤**——「科技部」独立出现时被当成机构实体脱敏为 XXXX，
     但「科技部」是通用通名，不含可定位到具体机构的信息；
     人工标准版对这类词**一律保留**。
  ② **机构实体漏脱**——「信息科技部」中命中的只是「科技部」，
     因前缀成词而整词保留，反而让「信息科技部」这个实体漏网。

【修复（D-4）】
把守卫语义拆成两张表（config.json → org_name_rules）：

  · `generic_words`（独立通用词）：本身即通用机构通名 → **保留**
    （科技部 / 作战部 / 运营中心 / 营运管理部 / 信息技术部 …）；
  · `composite_words`（前缀成词）：「通名 + 组织后缀」的机构实体
    → **整段脱敏**（信息科技部 → XXXX），杜绝「信息XXXX」混合态。

`strict_longer=True` 的 G4-S 兜底语义保持不变。

运行方式：
    python -m pytest tests/test_issue61_org_pattern.py -v
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


# 人工标准版口径：这些通用机构词一律保留
GENERIC_WORDS = [
    "运营部", "作战部", "营运部", "管理部", "营运管理部", "信息技术部",
    "营运计财部", "财务部", "科技部", "运营中心",
]


# ---------------------------------------------------------------------------
# 1. 配置
# ---------------------------------------------------------------------------

def test_org_name_rules_config():
    """两张表都落地，且互不重叠。"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    rules = cfg.get("org_name_rules") or {}
    generic = set(rules.get("generic_words") or [])
    composite = set(rules.get("composite_words") or [])
    assert generic, "缺少 generic_words 表"
    assert composite, "缺少 composite_words 表"
    assert not (generic & composite), "两张表语义重叠"
    for w in GENERIC_WORDS:
        assert w in generic, f"通用词表缺少 {w}"


# ---------------------------------------------------------------------------
# 2. 独立通用词 → 保留
# ---------------------------------------------------------------------------

def test_generic_org_words_kept():
    """通用机构词独立出现时保留原文。"""
    for w in GENERIC_WORDS:
        out = apply_redactions(w)
        assert out == w, f"{w!r} → {out!r}（通用机构词被误脱敏）"


def test_generic_org_words_in_context_kept():
    """通用机构词在句中同样保留。"""
    cases = [
        ("营运计财部提出需求", "营运计财部提出需求"),
        ("科技部推进相关工作", "科技部推进相关工作"),
        ("运营中心组织本次培训", "运营中心组织本次培训"),
    ]
    for src, want in cases:
        out = apply_redactions(src)
        assert out == want, f"{src!r} → {out!r}"


# ---------------------------------------------------------------------------
# 3. 前缀成词机构实体 → 整段脱敏
# ---------------------------------------------------------------------------

def test_composite_org_word_fully_redacted():
    """前缀成词机构实体整段脱敏，不产生「信息XXXX」混合态。"""
    out = apply_redactions("信息科技部")
    assert out == "XXXX", out
    assert "信息" not in out, "前缀成词机构实体出现前缀残留"


def test_no_mixed_org_form():
    """不得出现「前缀 + XXXX」的混合态。"""
    out = apply_redactions("信息科技部与运维中心联合")
    assert "信息" not in out, out
    assert "XXXX" in out, out
    assert out == "XXXX与运维中心联合", out


def test_generic_word_not_overshoot():
    """通用词不得被前缀成词表反向吞掉（独立出现仍保留）。"""
    for w in GENERIC_WORDS:
        out = apply_redactions(w)
        assert "XXXX" not in out, f"{w!r} → {out!r}"


def test_name_after_generic_word_still_masked():
    """通用机构词保留后，**紧邻的姓名仍须脱敏**（D-4 补丁）。

    「运营部」保留原文后，姓名通道的左边界（「前一字不得为汉字」）失效，
    会导致「运营部韩慧丽」中的真名漏脱（金标回归）。
    """
    out = apply_redactions("运营部韩慧丽向部门领导汇报")
    assert out == "运营部XXX向部门领导汇报", out
    assert "韩慧丽" not in out


def test_generic_word_following_text_kept():
    """通用机构词后接业务短语时不得误判为人名。"""
    for src in ("运营部配合完成差异分析工作", "科技部推进相关工作",
                "运营部提出需求", "信息技术部负责系统建设"):
        out = apply_redactions(src)
        assert out == src, f"{src!r} → {out!r}"


# ---------------------------------------------------------------------------
# 4. 零劣化
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
