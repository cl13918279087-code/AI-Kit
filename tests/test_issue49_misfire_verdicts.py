"""
Issue #49 第三轮复评：**逐条处置口径**落地守卫（2026-11-02）

背景：
    #49「姓名池扩张误伤」第二轮整改后，通用语料仍有 8 条被改动：
        叶子 / 白天 / 白天鹅 / 叶子涵 / 江苏卫视 / 欧元 / 宁波银行 / 甘肃银行
    经复评逐条裁决（处置口径定稿）：
        · 「叶子」「白天」「白天鹅」 → **不做姓名脱敏**（通用词）
        · 「欧元」「江苏卫视」       → **不做姓名脱敏**（币种 / 频道名）
        · 「叶子涵」                 → **作为姓名，必须脱敏**
        · 「宁波银行」「甘肃银行」   → 维持「XX银行」（#53 机构名整体保护的
                                       **设计意图**，非姓名误伤）

    并同步定稿**分母口径**（见 scripts/bench_metrics.py）：
        · 分母恒为 102 条通用语料，**绝不因结果删样本**（避免自证式达标）；
        · 分子按类别拆分：**姓名类误伤**是 #49 的关闭口径（≤5 条即 ≤5%），
          **非姓名类改动**（机构名保护的设计意图）单列披露、不计入关闭口径。

本文件为 CI 卡口：
    1. test_verdict_leafs_and_daylight_not_redacted  —— 通用词豁免
    2. test_verdict_leafson_name_must_be_redacted   —— 真名优先（整体脱敏）
    3. test_verdict_currency_and_channel_not_redacted
    4. test_verdict_bank_names_keep_design_intent
    5. test_verdict_in_context                        —— 上下文一致
    6. test_corpus_placement                          —— 叶子涵 归入真名集
    7. test_metrics_split_and_denominator_constant   —— 口径可对表

运行：
    python3 -m pytest tests/test_issue49_misfire_verdicts.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import common_rules  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 通用词：叶子 / 白天 / 白天鹅 —— 不做姓名脱敏
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["叶子", "白天", "白天鹅"])
def test_verdict_leafs_and_daylight_not_redacted(text):
    assert common_rules.apply_redactions(text) == text, (
        f"通用词「{text}」经 #49 复评裁决为不做姓名脱敏，实际被改动为 "
        f"{common_rules.apply_redactions(text)!r}"
    )


# ---------------------------------------------------------------------------
# 2. 叶子涵 —— 作为姓名必须脱敏（且整体脱敏为 XXX，不得残留「涵」）
# ---------------------------------------------------------------------------
def test_verdict_leafson_name_must_be_redacted():
    out = common_rules.apply_redactions("叶子涵")
    assert out == "XXX", f"「叶子涵」确认为姓名，须整体脱敏为 XXX，实际 {out!r}"
    assert "涵" not in out, "「叶子涵」须整体脱敏，不得残留尾字"


# ---------------------------------------------------------------------------
# 3. 欧元 / 江苏卫视 —— 币种与频道名，不是人名
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["欧元", "江苏卫视"])
def test_verdict_currency_and_channel_not_redacted(text):
    assert common_rules.apply_redactions(text) == text, (
        f"「{text}」经 #49 复评裁决为不做姓名脱敏，实际被改动为 "
        f"{common_rules.apply_redactions(text)!r}"
    )


# ---------------------------------------------------------------------------
# 4. 宁波银行 / 甘肃银行 —— 机构名整体保护的设计意图（非姓名误伤）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["宁波银行", "甘肃银行"])
def test_verdict_bank_names_keep_design_intent(text):
    out = common_rules.apply_redactions(text)
    assert out == "XX银行", f"「{text}」按 #53 机构名整体保护应为 XX银行，实际 {out!r}"
    assert not out.startswith("XX") or text.endswith("银行")


# ---------------------------------------------------------------------------
# 5. 上下文一致性 —— 同一通用词在句中同样不得被误伤
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "采购的叶子已签收",
    "白天鹅在湖边",
    "欧元汇率上涨",
    "江苏卫视晚间节目",
    "老叶子和小叶子",
])
def test_verdict_in_context(text):
    assert common_rules.apply_redactions(text) == text, (
        f"上下文中的通用词不应被脱敏，实际 {common_rules.apply_redactions(text)!r}"
    )


def test_verdict_mixed_context_keeps_true_name_masked():
    """「叶子涵与叶子同时到场」：真名脱敏、通用词保留。"""
    out = common_rules.apply_redactions("叶子涵与叶子同时到场")
    assert out == "XXX与叶子同时到场", f"实际 {out!r}"


# ---------------------------------------------------------------------------
# 6. 语料归属 —— 「叶子涵」已从负样本集迁入真名集
# ---------------------------------------------------------------------------
def test_corpus_placement():
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
    from test_name_false_positive import FALSE_POSITIVE_CORPUS, TRUE_NAME_SAMPLES

    assert "叶子涵" not in FALSE_POSITIVE_CORPUS, (
        "「叶子涵」已确认为姓名，不应留在负样本（通用语料）集中"
    )
    assert "叶子涵" in TRUE_NAME_SAMPLES, (
        "「叶子涵」应进入真名样本集，作为必须脱敏的回归守卫"
    )
    assert len(FALSE_POSITIVE_CORPUS) == 102, (
        f"负样本分母须恒为 102 条（口径：分母不随结果变动），实际 {len(FALSE_POSITIVE_CORPUS)}"
    )


# ---------------------------------------------------------------------------
# 7. 指标口径可对表（#49 关闭口径 = 姓名类误伤）
# ---------------------------------------------------------------------------
def test_metrics_split_and_denominator_constant():
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import bench_metrics

    metrics = bench_metrics.compute_metrics()
    name_cls = metrics["biz_misfire_name_class"]
    other_cls = metrics["biz_misfire_other_class"]
    total = metrics["biz_misfire"]

    assert name_cls["total"] == 102, "姓名类口径分母必须恒为 102"
    assert total["total"] == 102, "总口径分母必须恒为 102"
    assert name_cls["value"] + other_cls["value"] == total["value"], (
        "分类拆分之和须等于总数"
    )
    assert name_cls["value"] == 0, (
        f"姓名类误伤应为 0，实际 {name_cls['value']}：{name_cls['samples']}"
    )
    assert other_cls["samples"] == ["宁波银行", "甘肃银行"], (
        f"非姓名类仅应剩机构名保护的设计意图，实际 {other_cls['samples']}"
    )
    assert metrics["gates"]["biz_misfire_name_class"], "姓名类误伤门禁应通过"
