"""
Issue #52 回归守卫：职务与称谓式人名识别通道。

覆盖上游 Issue #52 §四「验收标准 · 回归用例」8 条，以及本实现自带的
三条**防扩池式误伤**回归（#49 教训：称谓形态只在锚点附近生效）。

关键口径说明：
  · 姓 + 职务后缀（刘主任 / 王总 / 李工 / 张经理）→ 整体脱敏为 XXX；
  · 昵称形态（小李 / 老李）→ 整体脱敏为 XXX；
  · 单字姓 + 动词边界（会议由刘主持）→ 仅脱敏单字姓，动词保留；
  · 组织称谓与业务词（业务组长 / 产品组长 / 专项工作 / 任务总览）→ 保持原文。

运行：
    python3 -m pytest tests/test_issue52_title_name.py -v
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(REPO, "scripts"), os.path.join(REPO, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import common_rules  # noqa: E402


# ---- 上游 #52 §四 回归用例（输入, 期望输出）----
ISSUE52_CASES = [
    # ① 姓 + 职务后缀
    ("郑州银行总体组刘主任负责运营工作", "XX银行总体组XXX负责运营工作"),
    ("王总一行三人到会", "XXX一行三人到会"),
    ("请陈总审批", "请XXX审批"),
    ("工程部李工前来处理", "工程部XXX前来处理"),
    # ② 昵称形态
    ("小李负责接口联调", "XXX负责接口联调"),
    # ③ 单字姓 + 动词边界
    ("会议由刘主持", "会议由XXX主持"),
    # ④ 组织称谓不得误伤
    ("由业务组长牵头推进", "由业务组长牵头推进"),
    ("营运计财部提出需求", "营运计财部提出需求"),
]

# ---- #51 §4.1 收口用例（上游 #51 §五 回归用例）----
ISSUE51_41_CASES = [
    ("需求组张经理汇报进展", "需求组XXX汇报进展"),
]


@pytest.mark.parametrize("src,expected", ISSUE52_CASES)
def test_issue52_title_name_cases(src, expected):
    """上游 #52 §四 8 条回归用例必须全部命中。"""
    out, _counts = common_rules.apply_redactions_counted(src)
    assert out == expected, f"#52 验收用例未通过：{src!r} → {out!r}（期望 {expected!r}）"


@pytest.mark.parametrize("src,expected", ISSUE51_41_CASES)
def test_issue51_41_minimal_unit_cases(src, expected):
    """#51 §4.1「最小匹配单元 + 双侧词边界」用例收口。"""
    out, _counts = common_rules.apply_redactions_counted(src)
    assert out == expected, f"#51 §4.1 用例未通过：{src!r} → {out!r}（期望 {expected!r}）"


def test_title_pass_does_not_misfire_business_words():
    """称谓形态只在锚点附近生效：业务词不得被误伤（#49 教训）。

    注：本断言只针对**本通道**（`_apply_title_name_pass`），
    以便与既有通用姓名规则的独立行为解耦。
    """
    must_keep = [
        "专项工作推进计划已下达",       # 项工 + 推进
        "任务总览与复用识别总览如下",     # 务总 / 别总
        "信息进行深度加工与洞察输出",     # 加工
        "推算各阶段工作任务时长",         # 和工 / 段工
        "业务经理牵头推进业务组长负责",   # 业务经理 / 业务组长
        "由产品组长负责总体组组长安排",   # 产品组长 / 总体组组长
        "小于该阈值时小于号记录",         # 小 + 于
        "请老经理审批",                  # 老 + 非实证姓"经"
    ]
    for src in must_keep:
        out = common_rules._apply_title_name_pass(src, {})
        assert out == src, f"业务词被误伤：{src!r} → {out!r}"


def test_title_pass_respects_excluded_common_words():
    """独立出现的职务词仍受 EXCLUDED_COMMON_WORDS 保护（#52 §3.1 优先级）。

    「经理」「组长」「主任」单独出现时不是称谓形态，本通道不得脱敏。
    """
    src = "该业务由经理统筹，主任审批，组长汇总"
    out = common_rules._apply_title_name_pass(src, {})
    assert out == src, f"独立职务词被误伤：{src!r} → {out!r}"


def test_title_pass_counts_name_category():
    """称谓形态脱敏必须计入「姓名」类别统计。"""
    _out, counts = common_rules.apply_redactions_counted("请陈总审批")
    assert counts.get("姓名", 0) >= 1, counts
