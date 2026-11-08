"""
Issue #62 正文叙述型人名召回回归（D-10 / 方案 A）。

【背景】
Issue #62 §二 指出：程序召回人名依赖「D16 名单库」与「区域上下文
（名单区 / 角色槽位 / 签名落款区）」双闸门。两者在**正文叙述句**中同时失效：

    "孙海刚行长指示业务部门"  →  原文一字未动

该形态既不在 D16 精确全名库（孙海刚为库外姓名），也不在名单区。
§四 方案 A（推荐并已落地）：补充「**姓名 + 职务后缀**」形态识别——
命中「姓 + 名用字{1,3} + 职务后缀」时脱敏姓名部分，职务后缀整体保留。

【本文件的断言】
  1. 叙述型人名**必须**脱敏（含 issue 原文实例与「无左锚点」变体）
  2. 纯职务标签**必须**保留（防与 #59 槽位分型冲突）
  3. 通用词 / 业务构词**必须**保留（防 #49/#51 误伤口径回退）
  4. 通道自身的收紧口径固化（2 字名须尾字 hot、委员会类组织形式豁免）

运行：
    python3 -m pytest tests/test_issue62_narrative_name.py -v
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import common_rules  # noqa: E402


def _redact(text: str) -> str:
    return common_rules.apply_redactions(text)


# ---------------------------------------------------------------------------
# 1. 叙述型人名必须脱敏
# ---------------------------------------------------------------------------

# (输入, 期望输出) —— 期望输出 = 姓名整段替换为 XXX，职务后缀保留
NARRATIVE_NAME_CASES = [
    # —— Issue #62 §一 / §四 给出的原文实例 ——
    ("孙海刚行长指示业务部门", "XXX行长指示业务部门"),
    ("由孙海刚行长安排", "由XXX行长安排"),
    # —— 「李义申总」：无左锚点 + 单字职务后缀 + 尾字低频（字频闸门失效场景）
    ("由李义申总安排", "由XXX总安排"),
    ("李义申总提出", "XXX总提出"),
    ("李义申总安排", "XXX总安排"),
    # —— 其它叙述型变体 ——
    ("阮秋鸣行长", "XXX行长"),
    ("吴文静总安排", "XXX总安排"),
    ("孙海刚行长要求各条线落实", "XXX行长要求各条线落实"),
    # —— 2 字真名 + 职务后缀（尾字 hot 档）——
    ("王海行长", "XXX行长"),
    ("张明经理", "XXX经理"),
]


@pytest.mark.parametrize("src,expected", NARRATIVE_NAME_CASES)
def test_narrative_name_must_be_masked(src, expected):
    out = _redact(src)
    assert out == expected, f"叙述型人名未按预期脱敏：{src!r} -> {out!r}（期望 {expected!r}）"


def test_issue62_regression_example():
    """Issue #62 §六 复现方式：孙海刚必须脱敏，其余保持。"""
    assert _redact("孙海刚行长指示业务部门") == "XXX行长指示业务部门"
    assert _redact("各条线副行长、总监") == "各条线副行长、总监"
    assert _redact("金融同业") == "金融同业"


# ---------------------------------------------------------------------------
# 2. 纯职务标签必须保留（防与 #59 槽位分型冲突）
# ---------------------------------------------------------------------------

ROLE_LABEL_KEEP_CASES = [
    "各条线副行长、总监",
    "业务资深经理",
    "市场推广经理",
    "技术骨干",
    "董事长致辞",
    "外部专家",
    "项目负责人",
    "各业务部门",
]


@pytest.mark.parametrize("src", ROLE_LABEL_KEEP_CASES)
def test_role_labels_must_be_kept(src):
    out = _redact(src)
    assert out == src, f"职务标签被误脱敏：{src!r} -> {out!r}"


# ---------------------------------------------------------------------------
# 3. 通用词 / 业务构词必须保留（防 #49/#51 误伤口径回退）
# ---------------------------------------------------------------------------

GENERIC_KEEP_CASES = [
    "金融同业",
    "电子银行",
    "客户关系管理主管",
    "风险管理委员会",
    "综合部管理委员会",
    "方案经理",
    "预算主管",
    "资金主管",
    "成本经理",
    "业务资深经理负责产品设计",
]


@pytest.mark.parametrize("src", GENERIC_KEEP_CASES)
def test_generic_words_must_be_kept(src):
    out = _redact(src)
    assert out == src, f"通用词/业务构词被误脱敏：{src!r} -> {out!r}"


# ---------------------------------------------------------------------------
# 4. 通道收紧口径固化
# ---------------------------------------------------------------------------

def test_committee_form_is_exempt():
    """「委员会」是组织形式而非职务——"风险管理委员会"不得被拆成
    "风险XXX员会"。这是本通道在真实业务语料上实测到的唯一误伤点，
    必须在规则层兜住（而不是靠当前语料恰好没命中）。"""
    out = _redact("信息科技风险管理委员会会议（IT Risk Committee Meeting）：CBIRC")
    assert out == "信息科技风险管理委员会会议（IT Risk Committee Meeting）：CBIRC"


def test_single_char_title_suffix_requires_three_char_name():
    """单字职务后缀「总」误伤面大（"张总共"/"李总结"），只在姓名部分
    ≥ 3 字时生效。"""
    assert _redact("张总共") == "张总共"
    assert _redact("李总结") == "李总结"
    assert _redact("李义申总") == "XXX总"


def test_two_char_name_requires_hot_tail():
    """2 字形态与 S1 高危姓规则同口径：尾字必须 hot。
    「方案经理」（尾字「案」非 hot）保留，「张明经理」（尾字「明」hot）脱敏。"""
    assert _redact("方案经理") == "方案经理"
    assert _redact("张明经理") == "XXX经理"


def test_narrative_channel_is_deterministic_and_idempotent():
    src = "孙海刚行长指示业务部门，由李义申总安排，各条线副行长、总监"
    once = _redact(src)
    assert once == _redact(src)
    assert _redact(once) == once


def test_name_tail_plus_title_is_not_split():
    """「字尾 + 职务后缀」成词时不得拦腰切坏。

    真实语料实测点（G7）："各条线执行人员提供执行数据，张瑜汇总，在决策会…"
    ——「张瑜汇」+「总」不是"人名 + 职务"，而是"张瑜" + "汇总"，
    切错会得到 "XXX总"。"""
    src = "各条线执行人员提供执行数据，张瑜汇总，在决策会1、2上进行情况汇报"
    assert _redact(src) == (
        "各条线执行人员提供执行数据，XXX汇总，在决策会1、2上进行情况汇报"
    )
    assert _redact("李义申总结") == "XXX总结"


def test_narrative_channel_reports_name_category():
    """该通道必须把计数记到「姓名」类（bench_metrics 的姓名类误伤口径
    依赖该分类；记错分类会让门禁把真实误伤算成「非姓名类设计意图」）。"""
    _, counts = common_rules.apply_redactions_counted("孙海刚行长指示业务部门")
    assert counts.get("姓名", 0) >= 1
