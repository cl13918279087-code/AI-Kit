#!/usr/bin/env python3
"""
test_issue56_d16_truncation.py - D16 二字名层截断缺陷回归守卫（Issue #56）

【缺陷现象】
D16 的 2 字名层与 3 字及以上层是**两个独立正则 + 顺序 sub**（非单层 alternation），
因此后执行的层无法回收已被前一层替换掉的片段。层序原为「2 字层在前」，
导致**库内确有更长全名**时被 2 字名抢先命中、输出残缺：

    「郑安琪」→「郑XXX」   （而「郑安琪」就在 D16 库里）
    「孙安琪」→「孙XXX」
    「杨成文」→「杨XXX」

实测 D16 库内共 **33 个 2 字名是更长库内名的后缀**（安琪←郑安琪、成文←杨成文、
明辉←张明辉、李军←郭李军…），这 33 个二字名全部存在该截断。

【修复】
层序改为「长名层优先」（3 字及以上层先执行），两层各自的边界语义保持不变：
  · 2 字名层左边界 `(?<![a-zA-Z0-9])` 严格 —— #49 P1-I 实测依据，防「组余翔」类误命中
  · 3 字及以上层左边界 `(?<![\u4e00-\u9fa5])` 放宽 —— 保住 ASCII/数字前缀的召回

【本文件守卫的内容】
  1. 33 个被覆盖二字名对应的更长全名 **不再被截断**（逐条参数化断言）
  2. #49 P1-I 的边界语义守卫场景 **行为不变**（ASCII/数字前缀召回、2 字层严格左边界）
  3. 通用语料 / 真实语料 / 金标 **零劣化**
  4. 「韩梅梅」类**词典缺口**明确区分 —— 属 #55 通用人名词典层范围，不在本 issue 修复口径

运行方式：
    python -m pytest tests/test_issue56_d16_truncation.py -v
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common_rules  # noqa: E402
from common_rules import apply_redactions  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, "scripts", "config.json")


def _load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _covered_short_names():
    """库内「被更长全名覆盖后缀」的 2 字名 → 更长全名列表。

    这 33 个二字名是截断缺陷的精确发生集合。

    注意：库内存在个别**含分隔符的异常条目**（如「曹超、胡波」——实为两个人名
    被误并为一条），它们不是合法的「更长短名」，须排除，否则断言口径失真。
    """
    names = [n for n in _load_config()["full_name_list"] if isinstance(n, str) and n != "姓名"]
    short = {n for n in names if len(n) == 2}
    covered = {}
    for n in names:
        if len(n) >= 3 and n[-2:] in short and re.fullmatch(r"[一-鿿]+", n):
            covered.setdefault(n[-2:], []).append(n)
    return covered


# ---------------------------------------------------------------------------
# 1. 截断缺陷修复 —— 逐条参数化
# ---------------------------------------------------------------------------


def test_no_truncation_for_library_longer_names():
    """库内有更长全名时，不得被 2 字名抢先命中导致残缺输出。"""
    covered = _covered_short_names()
    # 32 = 原始 33 中排除「曹超、胡波」——该条目实为两个人名被误并为一条（库污染），
    # 不是合法的「更长短名」，不属于截断缺陷集合。
    assert len(covered) == 32, f"覆盖集规模变化：{len(covered)}（预期 32）"

    failures = []
    for short, longs in covered.items():
        for long_name in longs:
            out = apply_redactions(long_name)
            # 期望：整名被替换为 XXX，不残留首字
            if out != "XXX":
                failures.append(f"{long_name} → {out}（2 字名「{short}」抢先命中）")
    assert not failures, "以下库内全名仍被 2 字名截断：\n  " + "\n  ".join(failures)


def test_truncation_defect_scope_covered():
    """覆盖集本身的自检 —— 确认守卫集有效（非空且构成真实截断关系）。"""
    covered = _covered_short_names()
    assert covered, "覆盖集不应为空"
    # 每个二字名都应确实存在于库中
    names = set(_load_config()["full_name_list"])
    for short in covered:
        assert short in names, f"二字名「{short}」不在 D16 库中"


# ---------------------------------------------------------------------------
# 2. #49 P1-I 边界语义守卫 —— 修复不得回退既有能力
# ---------------------------------------------------------------------------


def test_ascii_prefix_long_names_still_matched():
    """3 字层左边界放宽的设计目标：ASCII/数字前缀的长名仍须命中。"""
    cases = [
        ("研发组11张海翔", "研发组11XXX"),
        ("IDATA-BDS季海涛", "IDATA-BDSXXX"),
    ]
    for src, want in cases:
        assert apply_redactions(src) == want, f"{src!r} → {apply_redactions(src)!r}（期望 {want!r}）"


def test_short_name_left_boundary_still_strict():
    """2 字层严格左边界的守卫场景：紧跟汉字前缀时行为不变。"""
    assert apply_redactions("组余翔") == "组XXX"


def test_library_two_char_name_still_matched():
    """库内 2 字名在独立出现时仍须整体脱敏（不被层序调整误伤）。"""
    # 注意：不能用「白天」——它在 standalone_exempt_words 豁免表内（#49 裁决），
    # 本就不脱敏，与本次层序调整无关。
    for src in ("王丽", "李明", "张伟"):
        out = apply_redactions(src)
        assert out == "XXX", f"{src!r} → {out!r}（期望 'XXX'）"


def test_unknown_isolated_name_not_redacted():
    """库外孤立 2 字名的脱敏口径。

    【历史】本用例原断言「王明」不脱敏（#50 §4.8 已知边界）。
    【现状】Issue #55 通用人名词典层上线后，「王明」在词典层词表内
    → 已被精确匹配脱敏。本用例口径随之更新为 #55 的新基准。

    注：#56 修复本身**不改变**该行为——层序调整只影响 D16 内部两层，
    「王明」从来不在 D16 库中，其脱敏与否取决于是否接入词典层。
    """
    assert apply_redactions("王明") == "XXX", (
        "「王明」在通用人名词典层内，应脱敏（Issue #55 口径）")


# ---------------------------------------------------------------------------
# 3. 零劣化守卫
# ---------------------------------------------------------------------------


def test_biz_corpus_no_new_misfire():
    """102 条通用业务语料：姓名类误伤须维持 0（复用门禁同源判定口径）。"""
    from test_name_false_positive import FALSE_POSITIVE_CORPUS

    name_class = []
    other_class = []
    for seg in FALSE_POSITIVE_CORPUS:
        out, cnt = common_rules.apply_redactions_counted(seg)
        if out == seg:
            continue
        if cnt.get("姓名"):
            name_class.append(seg)
        else:
            other_class.append(seg)
    assert not name_class, f"通用语料出现姓名类误伤 {len(name_class)} 条：{name_class[:3]}"
    # 非姓名类改动为 #53 机构名整体保护的设计意图（宁波银行 / 甘肃银行 → XX银行），
    # 口径与门禁一致：允许，但须 ≤ 5 条。
    assert len(other_class) <= 5, f"非姓名类改动 {len(other_class)} 条，超过门禁上限"


def test_real_corpus_zero_misfire():
    """1200 条真实业务语料：零误伤（复用门禁同源豁免判定）。"""
    import bench_metrics

    bench = os.path.join(REPO_ROOT, "tests", "benchmark", "biz_corpus.json")
    with open(bench, encoding="utf-8") as f:
        real = json.load(f)

    misfires = []
    for idx, seg in enumerate(real):
        out = apply_redactions(seg)
        if out == seg:
            continue
        for old, rep in bench_metrics._diff_segments(seg, out):
            if any(p in old for p in bench_metrics._PLACEHOLDER_NAMES):
                continue
            if bench_metrics._ORG_SUFFIX_RE.fullmatch(old.strip()) and rep.strip() == "XXXX":
                continue
            if bench_metrics._is_date_intent(seg, old, rep):
                continue
            misfires.append((idx, old, rep, seg[:50]))
    assert not misfires, f"真实语料出现 {len(misfires)} 处误伤，例如：{misfires[:3]}"


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
        probe = f"负责人：{g}，负责整体推进。"
        if g in apply_redactions(probe):
            missed.append(g)
    assert not missed, f"金标漏脱 {len(missed)} 个：{missed[:5]}"


# ---------------------------------------------------------------------------
# 4. 词典缺口与本 issue 的边界（#55 范围，显式记录）
# ---------------------------------------------------------------------------


def test_dictionary_gap_is_separate_from_truncation():
    """「韩梅梅」不在 D16 库 → 属词典缺口（#55），**不是**截断缺陷。

    本 issue 的修复口径只覆盖「库内有更长全名却被 2 字名截断」；
    「整体不在库」的姓名不在此列，其修复路径是 #55 通用人名词典层。

    【#55 上线后的现状】「韩梅梅」在通用人名词典层词表内 → 已整体脱敏。
    词典层置于 D16 **之前**执行，故不会再出现「韩XXX」这类残缺输出。
    """
    names = set(_load_config()["full_name_list"])
    assert "韩梅梅" not in names, "前提失效：「韩梅梅」已在 D16 库中，本用例需重估"
    # 通用人名词典层覆盖 → 整体脱敏，不残留首字
    assert apply_redactions("韩梅梅") == "XXX"
    # 带上下文时同样整体脱敏
    assert apply_redactions("联系人：韩梅梅") == "联系人：XXX"


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
