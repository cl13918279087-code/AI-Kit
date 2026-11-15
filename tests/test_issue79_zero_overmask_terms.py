"""Issue #79：零星过度脱敏治理——D16 全名库污染与业务术语被姓名规则误吞。

【背景】
七组回归的残余过度脱敏（over）中有一批属于「业务术语 / 系统名被姓名通道误吞」，
人工标准版一律保留：

| 词 | 上下文 | 标准版 | 整改前 |
|---|---|---|---|
| `司法` | 改造13A128**司法查控**平台 | 保留 | 脱敏 |
| `付方` | 交易信息（**付方**账户信息及交易金额等） | 保留 | 脱敏 |
| `门总` | 各业务部**门总**经理 | 保留 | 脱敏 |
| `加强` | 敏捷处理、**加强**引导、每日报告 | 保留 | 脱敏 |

`司法` 同时存在于 D16 全名库（`full_name_list`），「司法查控平台」是**系统名**
而非人名——属全名库污染。为避免移除库条目引入真名漏脱，本 issue **不改全名库**，
改用 G4 整词保护表做**语境级**豁免（包含语义：表内词完整覆盖命中片段才豁免）。

运行：
    python3 -m pytest tests/test_issue79_zero_overmask_terms.py -v
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import common_rules as cr  # noqa: E402
from common_rules import apply_redactions  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 系统名 / 业务术语：语境内必须原样保留
# ---------------------------------------------------------------------------

KEEP_CASES = [
    ("改造13A128司法查控平台强峰营运管理部", "司法查控平台"),
    ("原因：网点柜面系统中输入的交易信息（付方账户信息及交易金额等）", "付方账户信息"),
    ("成员：各业务部门总经理", "部门总经理"),
    ("投诉，平安银行将遵循提前告知、舆情监测、敏捷处理、加强引导、每日报告的五大原则",
     "加强引导"),
    ("领域负责人加强投产规范和纪律宣导，确保", "加强投产规范"),
    ("及时识别问题处理优先级和资源，加强问题快速响应和处理的调度指挥；",
     "加强问题快速响应"),
]


@pytest.mark.parametrize("text,keep", KEEP_CASES)
def test_term_kept_in_context(text, keep):
    """系统名 / 业务术语在语境中必须原样保留（不得被姓名通道吞掉）。"""
    out = apply_redactions(text)
    assert keep in out, f"业务术语被误遮：{text} -> {out}"


# ---------------------------------------------------------------------------
# 2. 保护词必须落在 G4 整词保护表（通用规则入口）
# ---------------------------------------------------------------------------

NEW_GUARD_WORDS = [
    "司法查控", "付方账户", "部门总经理",
    "加强引导", "加强投产规范", "加强问题快速响应",
]


@pytest.mark.parametrize("word", NEW_GUARD_WORDS)
def test_new_guard_word_in_table(word):
    assert word in cr._get_common_word_guards(), f"未入整词保护表：{word}"


# ---------------------------------------------------------------------------
# 3. 真名安全：豁免不得波及其它语境的人名
# ---------------------------------------------------------------------------

def test_standalone_name_still_masked():
    """独立出现的库内姓名仍须脱敏（豁免只在特定语境生效）。"""
    out = apply_redactions("负责人：司法")
    assert "司法" not in out, f"真名漏脱：{out}"


def test_standalone_two_char_name_still_masked():
    """未被列入保护表的 2 字姓名在角色上下文下仍须脱敏。"""
    out = apply_redactions("负责人：加强")
    assert "加强" not in out, f"真名漏脱：{out}"


def test_name_with_guard_word_suffix_not_over_protected():
    """保护词只覆盖**整词**语境；「XX加强引导」这类独立人名不得被连带放过。"""
    out = apply_redactions("负责人：张加强引导项目")
    assert "张加强" not in out, f"人名漏脱：{out}"


# ---------------------------------------------------------------------------
# 4. 入库审计：GB2312 二级生僻字 / 真名池互斥
# ---------------------------------------------------------------------------

def test_new_guard_words_have_no_rare_chars():
    """新保护词不得含 GB2312 二级生僻字——否则会连带把该字剔出 D-6 冷档池。"""
    rare = set(cr._get_gb2312_chars(2))
    for word in NEW_GUARD_WORDS:
        hit = sorted({c for c in word if c in rare})
        assert not hit, f"保护词含二级生僻字 {hit}（会污染 D-6 冷档池）：{word}"


def _name_pools():
    d16 = cr.get_full_name_set()
    cfg = cr._load_config()
    dict_names = set()
    for p in cr._resolve_data_paths(cfg.get("common_name_dict_file")):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    dict_names.add(line)
    gold = set()
    gp = REPO / "tests" / "benchmark" / "gold_names.json"
    if gp.exists():
        data = json.loads(gp.read_text(encoding="utf-8"))
        for v in data.values():
            gold.update(v if isinstance(v, list) else [])
    return {"D16": d16, "#55": dict_names, "gold": gold}


def test_new_guard_words_not_whole_names_in_pools():
    """新保护词本身不得是任何真名池中的**整名**（否则该姓名永远无法脱敏）。"""
    pools = _name_pools()
    for word in NEW_GUARD_WORDS:
        for label, pool in pools.items():
            assert word not in pool, f"{label} 含保护词整名 {word}（该姓名将永远漏脱）"


def test_judicial_term_excluded_from_d16_exact_layer():
    """`司法`（系统名）已从 D16 **精确层**剔除——但语料派生库本身保持原样。

    排除集由配置 `d16_non_name_entries` 驱动（默认内置「姓名」），
    部署方的本行花名册若确有「司法」此人，删掉该配置项即可恢复。
    """
    cfg = cr._load_config()
    assert "司法" in (cfg.get("d16_non_name_entries") or []), (
        "配置未登记「司法」为非人名条目")
    pools = _name_pools()
    assert "司法" not in pools["D16"], "「司法」仍在 D16 精确层（系统名污染未清除）"
    out = apply_redactions("改造13A128司法查控平台")
    assert "司法查控平台" in out, f"系统名未被保留：{out}"
    out2 = apply_redactions("本次改造由司法负责")
    assert "司法" not in out2, f"姓名在其它语境下漏脱：{out2}"


def test_non_name_entries_configurable():
    """排除集可配置——把「司法」从配置移除后，精确层应重新覆盖该条目。"""
    import json as _json
    cfg = cr._load_config()
    cfg = dict(cfg)
    cfg["d16_non_name_entries"] = []
    old = cr._CONFIG_CACHE
    try:
        cr._CONFIG_CACHE = cfg
        cr.reset_patterns()
        assert "司法" in cr.get_full_name_set(), (
            "配置移除后「司法」应重新进入 D16 精确层（部署方可恢复该姓名）")
    finally:
        cr._CONFIG_CACHE = old
        cr.reset_patterns()
