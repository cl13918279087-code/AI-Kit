#!/usr/bin/env python3
"""
test_issue53_org_institution_rules.py - Issue #53 回归测试（组织名与机构词整体保护）

覆盖上游 Issue #53《组织名与机构词整体保护规则集》的四块能力：

  1. **§3.1 org_suffixes 扩充**：组织后缀集合覆盖处/科/室/所/站/小组/科室/
     事业部/职能部门/分室/中心组。
  2. **§3.2 前缀成词整体保留**：命中片段若只是「名词 + 组织后缀」复合实体的
     一部分（前缀成词），则整词豁免，保留原文——
     「信息科技部与运维中心联合」不得被切成「信息XXXX与运维中心联合」。

     【D-4（Issue #61-B）口径更新】该条按**语义拆分**为两张表：
       · 独立通用词（科技部 / 作战部 / 运营中心 …）→ 保留；
       · 前缀成词机构实体（信息科技部）→ **整段脱敏**（XXXX），
         杜绝「信息XXXX」混合态。
     故本文件中「信息科技部」相关断言按新口径重写，其余保持不变。
  3. **§3.3 机构 + 地名链整体收拢**：「深圳高新北支行网点」整体脱敏为
     「XX支行网点」（保留机构类型后缀），不得从中间切开。
  4. **§3.4 通道互斥断言**：银行名/机构名判定链命中后，区间锁定，
     姓名通道与地址通道不得再切内部——「工商银行北京分行」→「XX银行北京分行」。

同时锁定三条零劣化红线：
  · 上游 7 条验收用例逐条一致；
  · #14 场景「关于郑州银行」→「关于XX银行」不退化；
  · 通用业务语料误伤不增加。

运行方式：
    python3 -m pytest tests/test_issue53_org_institution_rules.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

import common_rules  # noqa: E402


# ---------------------------------------------------------------------------
# 上游 Issue #53 §四 验收标准（逐条）
# ---------------------------------------------------------------------------

ACCEPTANCE_CASES = [
    # (§3.2 前缀成词整体保留 / 角色短语保护)
    ("营运计财部提出需求", "营运计财部提出需求"),
    # D-4：前缀成词机构实体整段脱敏（不再是「信息科技部与运维中心联合」）
    ("信息科技部与运维中心联合", "XXXX与运维中心联合"),
    ("由业务组长牵头推进", "由业务组长牵头推进"),
    ("医务室通知全体员工体检", "医务室通知全体员工体检"),
    # (§3.3 机构 + 地名链整体收拢)
    ("深圳高新北支行网点", "XX支行网点"),
    # (§3.4 通道互斥断言)
    ("工商银行北京分行开业", "XX银行北京分行开业"),
    # (#14 不退化)
    ("关于郑州银行", "关于XX银行"),
]


@pytest.mark.parametrize("src,expected", ACCEPTANCE_CASES,
                         ids=[c[0] for c in ACCEPTANCE_CASES])
def test_issue53_acceptance_cases(src, expected):
    """上游 7 条验收用例逐条一致。"""
    assert common_rules.apply_redactions(src) == expected


# ---------------------------------------------------------------------------
# §3.1 org_suffixes 扩充
# ---------------------------------------------------------------------------

def test_org_suffixes_expanded():
    """组织后缀集合覆盖 Issue #53 §3.1 要求的全部后缀。"""
    cfg = common_rules._load_config()
    suffixes = set(cfg.get("org_suffixes") or [])
    required = {"部", "中心", "办公室", "处", "科", "室", "所", "站",
                "小组", "科室", "事业部", "职能部门", "分室", "中心组"}
    assert required <= suffixes, f"org_suffixes 缺少：{required - suffixes}"


def test_org_suffix_recognized():
    """扩充后的后缀能被组织名通道识别（不再漏检科室/小组/中心组等）。"""
    for src in ("科室", "小组", "中心组", "分室", "职能部门"):
        assert common_rules.apply_redactions(src) == "XXXX", src


# ---------------------------------------------------------------------------
# §3.2 前缀成词整体保留
# ---------------------------------------------------------------------------

def test_prefix_compound_kept_whole():
    """D-4：前缀成词机构实体整段脱敏（不产生「信息XXXX」混合态）。"""
    assert common_rules.apply_redactions("信息科技部") == "XXXX"
    assert common_rules.apply_redactions("信息科技部与运维中心联合") == \
        "XXXX与运维中心联合"


def test_prefix_compound_not_overshoot():
    """独立通用机构词按 D-4 口径保留——通用词豁免不得把通道整体废掉，
    也不得反向把前缀成词实体放过。"""
    assert common_rules.apply_redactions("科技部") == "科技部"
    assert common_rules.apply_redactions("信息科技部") == "XXXX"


def test_role_phrases_kept():
    """角色/组织短语进保护表后不再被当人名切走。"""
    for src in ("由业务组长牵头推进", "由牵头部门推进",
                "产品组长负责", "医务室通知全体员工体检"):
        assert common_rules.apply_redactions(src) == src


# ---------------------------------------------------------------------------
# §3.3 机构 + 地名链整体收拢
# ---------------------------------------------------------------------------

def test_institution_chain_collapsed():
    """地名 + 机构链整体收拢，保留机构类型后缀，不从中间切开。"""
    assert common_rules.apply_redactions("深圳高新北支行网点") == "XX支行网点"
    assert common_rules.apply_redactions("上海浦东支行网点") == "XX支行网点"


def test_institution_chain_keeps_suffix_type():
    """收拢后仍保留机构类型后缀（支行/网点/营业部/分行）。"""
    out = common_rules.apply_redactions("深圳高新北支行网点")
    assert out.endswith("支行网点")


# ---------------------------------------------------------------------------
# §3.4 通道互斥断言
# ---------------------------------------------------------------------------

def test_bank_chain_not_split():
    """银行判定链命中后，机构链区间锁定，不得被地址/姓名通道再切。"""
    assert common_rules.apply_redactions("工商银行北京分行开业") == "XX银行北京分行开业"
    assert common_rules.apply_redactions("中国银行深圳分行") == "XX银行深圳分行"
    assert common_rules.apply_redactions("招商银行深圳分行") == "XX银行深圳分行"


def test_issue14_no_regression():
    """#14 场景不退化：关于郑州银行 → 关于XX银行。"""
    assert common_rules.apply_redactions("关于郑州银行") == "关于XX银行"


def test_literal_bank_branch_still_collapsed():
    """非「已脱敏银行名」锚定的机构链仍照常收拢（不得过度保留地名）。"""
    assert common_rules.apply_redactions("银行金水支行") == "银行XX支行"
    assert common_rules.apply_redactions("杭州西湖支行") == "XX支行"


# ---------------------------------------------------------------------------
# 零劣化：通用业务语料误伤不增加
# ---------------------------------------------------------------------------

def test_no_new_business_word_misfire():
    """§3.2 豁免只做「保留」，不得引入新的业务词误伤。"""
    from test_name_false_positive import FALSE_POSITIVE_CORPUS
    misfires = [c for c in FALSE_POSITIVE_CORPUS
                if common_rules.apply_redactions(c) != c]
    assert len(misfires) <= 12, f"通用业务语料误伤 {len(misfires)} 超过上限；{misfires[:10]}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
