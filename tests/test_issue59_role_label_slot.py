#!/usr/bin/env python3
"""
test_issue59_role_label_slot.py - Issue #59 回归守卫（L2 职务标签层 + 槽位内容分型）

【缺陷现象】
「角色：职务标签」这类槽位里的**职务词**被当成姓名整段抹掉：

    "副组长：各条线副行长、总监"            → "副组长：XXXXXX、XXX"   ❌
    "成员：业务和技术骨干、外部专家骨干"      → "成员：……、XXXXXX"       ❌

职务词（副行长/总监/骨干/外部专家）承载的是**岗位信息**而非自然人身份，
被抹掉之后文档失去可读性，且属可控的信息损失之外的误伤。

【修复（D-2）】
  ① 落地 L2 职务标签层：config.role_label_patterns 组合式声明
     （prefix_forms 通名前缀 + suffixes 职务后缀）；
  ② 锚点扩散通道改为**槽位内容分型**，逐条目判定：

      | 条目类型   | 判据                        | 动作     |
      |------------|-----------------------------|----------|
      | 纯人名     | L1 正向证据                | 脱敏     |
      | 人名+职务  | 职务词前是姓氏              | 脱敏人名 |
      | 纯职务标签 | L2 命中                     | 保留     |
      | 不可判定   | 无任何证据                  | 保留     |

  ③ 角色上下文通道（`_apply_role_context_pass`）同步接入同一分型判定，
     使「成员：技术骨干」这类单条目槽位也不再被抹除。

运行方式：
    python -m pytest tests/test_issue59_role_label_slot.py -v
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
# 1. L2 职务标签层
# ---------------------------------------------------------------------------

def test_role_label_patterns_config():
    """config 中落地组合式职务标签声明（prefix_forms + suffixes）。"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    pat = cfg.get("role_label_patterns") or {}
    assert pat.get("suffixes"), "缺少职务后缀表 suffixes"
    assert pat.get("prefix_forms"), "缺少通名前缀表 prefix_forms"
    for w in ("副行长", "副组长", "总经理", "总监", "骨干"):
        assert w in pat["suffixes"], f"职务后缀表缺少 {w}"
    for w in ("各条线", "外部专家", "业务", "技术"):
        assert w in pat["prefix_forms"], f"通名前缀表缺少 {w}"


def test_l2_role_label_recognition():
    """L2：职务标签命中；真实姓名不命中。"""
    for w in ("副行长", "总监", "骨干", "组长", "经理", "专家"):
        assert common_rules._is_role_label(w) is True, w
    for w in ("孙海刚", "毛月珍", "朱丽琼"):
        assert common_rules._is_role_label(w) is False, w


def test_l1_l2_separation():
    """L1 与 L2 互斥：职务标签不具人名证据，真实姓名有证据。"""
    for w in ("副行长", "总监", "骨干", "外部专家"):
        assert common_rules._has_name_evidence(w) is False, w
    for w in ("孙海刚", "毛月珍", "朱丽琼"):
        assert common_rules._has_name_evidence(w) is True, w


# ---------------------------------------------------------------------------
# 2. 槽位内容分型
# ---------------------------------------------------------------------------

def test_pure_role_label_slot_kept():
    """纯职务标签槽位：整条保留，不得被抹成 XXX。"""
    cases = [
        "副组长：各条线副行长、总监",
        "成员：业务和技术骨干、外部专家骨干",
        "成员：技术骨干",
        "副组长：外部专家",
    ]
    for src in cases:
        out = apply_redactions(src)
        assert out == src, f"{src!r} → {out!r}（职务标签被抹除）"


def test_pure_person_slot_redacted():
    """纯人名槽位：仍然脱敏（不得因职务标签识别而漏脱）。"""
    cases = [
        ("组长：孙海刚、毛月珍", "组长：XXX、XXX"),
        ("副组长：朱丽琼、李杰", "副组长：XXX、XXX"),
        ("核心组：蔡新发、张瑜、李明", "核心组：XXX、XXX、XXX"),
    ]
    for src, want in cases:
        out = apply_redactions(src)
        assert out == want, f"{src!r} → {out!r}（期望 {want!r}）"


def test_mixed_person_title_slot():
    """「人名 + 职务」混合槽位：人名部分脱敏，职务词保留。"""
    out = apply_redactions("组长：王组长")
    assert out == "组长：XXX", out
    assert "王" not in out


def test_indeterminate_slot_kept():
    """不可判定条目：保留（缺省倒向可追溯的一侧）。"""
    for src in ("副组长：各方代表", "成员：有关专家"):
        out = apply_redactions(src)
        assert out == src, f"{src!r} → {out!r}"


# ---------------------------------------------------------------------------
# 3. 零劣化：通用语料误伤不增加
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
