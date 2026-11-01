"""
Issue #51 真实业务语料零误伤回归（Issue #51 整改成果固化）。

【背景】
Issue #51 用真实业务语料（公司 Agent 规划文档，1200 条长文本片段）实测出
**39 处**业务词误伤。逐阶段 diff 归因后，根因有两类：
  (a) `common_word_guards`（G4 整词保护表）缺少真实整词
      （编排链路/灰区/待定清单/关键利益相关方/全量/超期/决策/最终判断/依据/
        自行解决/监管报送/报告生成/会议纪要/总经理/财务/审批人…）
  (b) G4/G7 守卫未接入 anchor / plain_list / english 三个通道，
      且英文通道与 PRE19 短斜杠日期缺少业务词/上下文守卫

【本文件的断言】
  1. 上述每一类误伤都必须在**规则层**被兜住（不只是当前语料恰好没命中）
  2. 真名零泄漏：占位名「张三/李四」必须脱敏，且不得截断更长真名「张三丰」
  3. 保护表与 D16 全名库 / 评测金标**双向互斥**——保证不会因保护而漏脱真名

运行：
    python3 -m pytest tests/test_issue51_real_corpus_misfire.py -v
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
for p in (SCRIPTS,):
    if p not in sys.path:
        sys.path.insert(0, p)

import common_rules  # noqa: E402

BENCH = os.path.join(REPO, "tests", "benchmark")


@pytest.fixture(scope="module")
def config():
    with open(os.path.join(SCRIPTS, "config.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def gold_names():
    with open(os.path.join(BENCH, "gold_names.json"), encoding="utf-8") as f:
        gold = json.load(f)
    out = set()
    for v in gold.values():
        out |= set(v)
    return out


# ---------------------------------------------------------------------------
# 1. 业务词零误伤：每一类都必须在规则层被兜住
# ---------------------------------------------------------------------------

# (输入, 期望输出)——期望输出中的业务词必须原样保留
BUSINESS_TERM_CASES = [
    # 地址通道：「编排链路」被当成「XX路」（G4-c 重叠豁免）
    ("技能包索引，含编排链路定义（chain字段）", "编排链路"),
    ("针对所有编排链路接口开展全量测试", "编排链路"),
    ("14个编排链路", "编排链路"),
    ("整体编排链路和Agent", "编排链路"),
    # 地址通道：「灰区」被当成「XX区」
    ("Step 1: 识别灰区 → 移入灰区待定清单", "灰区"),
    # 地址通道：「监管报送路径」被当成「XX路径」
    ("含完整COI管理和监管报送路径", "监管报送"),
    # 地址通道：「设计转型路径」被当成「XX路径」
    ("评估数字化能力、设计转型路径并推动落地实施", "设计转型"),
    # 姓名主通道：「关键利益相关方」被切成「关键利益」
    ("### 1.3 关键利益相关方", "关键利益"),
    # 姓名主通道：「项目群管理全视图」
    ("新核心系统建设项目群管理全视图", "管理全"),
    # 姓名主通道：「超期1-2天」
    ("银行业行动项升级矩阵：超期1-2天→项目经理", "超期"),
    # 姓名主通道：「全量37个Skill」
    ("并对全量37个Skill执行章节", "全量"),
    # role 通道：「不替代架构师决策」
    ("做战略决策、不替代架构师决策", "决策"),
    ("做战略决策、不替代架构师最终判断", "最终判断"),
    # role 通道：「编制依据」
    ("**编制依据**：统一接口规范标准", "依据"),
    # role 通道：「各项目组自行解决」
    ("一般问题 → 各项目组自行解决（L1，24小时内）", "自行解决"),
    # role 连词通道：「银行业监管和高管报告」
    ("在此基础上，新增3类银行业监管和高管报告", "监管"),
    # anchor 通道：「负责人：X（总经理）」
    ("负责人：李四（总经理）", "总经理"),
    ("负责人：李四（总经）", "总经"),
    # anchor 通道：「成员：业务/财务/IT」
    ("成员：业务/财务/IT", "财务"),
    # anchor 通道：「审批人：」
    ("审查人：____ 测试负责人：____ 审批人：", "审批人"),
    # plain_list 通道：「报告生成、会议纪要」
    ("报告生成、会议纪要", "报告生成"),
    ("报告生成、会议纪要", "会议纪要"),
    # 英文通道：Agent / Skill 是产品术语
    ("步骤协调（Agent协同工作的\"中枢\"）", "Agent"),
    ("依据：Agent Skill验收测试报告v2.0", "Agent"),
    ("依据：Agent Skill验收测试报告v2.0", "Skill"),
    # PRE19 短斜杠日期：分节编号 / 分数不是日期
    ("项目总体管理（1/2）-新核心系统建设项目群管理全视图", "（1/2）"),
    ("结果：10/10 全部通过 ✅", "10/10"),
]


@pytest.mark.parametrize("text,keep", BUSINESS_TERM_CASES)
def test_business_term_not_misfired(text, keep):
    """业务术语必须原样保留——不得被当成姓名/地址/机构脱敏。"""
    out = common_rules.apply_redactions(text)
    assert keep in out, f"业务词 {keep!r} 被误伤：{text!r} → {out!r}"


# ---------------------------------------------------------------------------
# 2. 真名零泄漏：占位名必须脱敏，且不得截断更长真名
# ---------------------------------------------------------------------------

PLACEHOLDER_NAME_CASES = [
    ("张三负责系统建设", "张三"),
    ("李四负责系统建设", "李四"),
    ("张三(CIO)", "张三"),
    ("李四(总经理)", "李四"),
    ("组长张三", "张三"),
    ("王五负责", "王五"),
    ("赵六负责", "赵六"),
]


@pytest.mark.parametrize("text,name", PLACEHOLDER_NAME_CASES)
def test_placeholder_name_redacted(text, name):
    """占位名承担真实人名语义位 → 必须脱敏（占位名不脱 = 人名位泄漏）。"""
    out = common_rules.apply_redactions(text)
    assert name not in out, f"占位名 {name!r} 漏脱：{text!r} → {out!r}"


LONGER_NAME_CASES = [
    ("张三丰负责系统建设", "张三丰"),
    ("李四明负责系统建设", "李四明"),
    ("王建设负责系统建设", "王建设"),
]


@pytest.mark.parametrize("text,name", LONGER_NAME_CASES)
def test_longer_name_not_truncated(text, name):
    """更长真名不得被占位名层截断（张三丰 ≠ 张三 + 残留一字）。"""
    out = common_rules.apply_redactions(text)
    assert name not in out, f"真名 {name!r} 漏脱：{text!r} → {out!r}"
    assert out.startswith("XXX"), f"真名 {name!r} 被截断：{text!r} → {out!r}"


# ---------------------------------------------------------------------------
# 3. 保护表与真名库双向互斥——保证「保护」不会造成真名漏脱
# ---------------------------------------------------------------------------

def test_common_word_guards_mutually_exclusive_with_real_names(config, gold_names):
    """G4 整词保护表与 D16 全名库 / 评测金标**双向互斥**。

    互斥（无任何包含关系）是 G4 不会造成真名漏脱的**充分条件**：
    保护词既不是真名的子串、也不包含任何真名，
    因此任何真名都不可能被保护表豁免。
    """
    guards = set(config.get("common_word_guards") or [])
    names = set(config.get("full_name_list") or []) | gold_names
    bad = []
    for g in guards:
        for n in names:
            if len(g) >= 2 and len(n) >= 2 and (g in n or n in g):
                bad.append((g, n))
    assert not bad, f"保护表与真名库存在包含关系（会造成真名漏脱）：{bad[:10]}"


def test_common_word_guards_no_placeholder_names(config):
    """保护表不得含占位人名——占位名需要脱敏，不能被豁免。"""
    guards = set(config.get("common_word_guards") or [])
    placeholders = set(config.get("placeholder_names") or [])
    overlap = guards & placeholders
    assert not overlap, f"占位名被写入保护表（会导致漏脱）：{overlap}"


def test_placeholder_names_not_in_full_name_list(config):
    """占位名不得混入 D16 全名库——否则会被 2 字名层优先匹配，截断更长真名。"""
    full = set(config.get("full_name_list") or [])
    placeholders = set(config.get("placeholder_names") or [])
    overlap = full & placeholders
    assert not overlap, f"占位名混入 full_name_list（会截断更长真名）：{overlap}"


def test_english_term_whitelist_excludes_common_names(config):
    """英文术语白名单不得含常见英文人名——否则真名会被豁免。"""
    whitelist = set(config.get("english_term_whitelist") or [])
    en_names = {
        "Lisa", "David", "Michael", "John", "James", "Robert", "William",
        "Richard", "Thomas", "Charles", "Daniel", "Matthew", "Anthony",
        "Mark", "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kevin",
        "Brian", "George", "Edward", "Ronald", "Timothy", "Jason", "Jeffrey",
        "Ryan", "Jacob", "Gary", "Nicholas", "Eric", "Jonathan", "Stephen",
        "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara",
        "Susan", "Jessica", "Sarah", "Karen", "Nancy", "Lisa", "Betty",
        "Helen", "Sandra", "Donna", "Carol", "Ruth", "Sharon", "Michelle",
        "Laura", "Emma", "Olivia", "Sophia", "Isabella", "Ava", "Mia",
        "Charlotte", "Amelia", "Harper", "Evelyn", "Abigail", "Emily",
        "Ashley", "Wang", "Lee", "Zhang", "Liu", "Chen", "Yang", "Huang",
        "Zhao", "Wu", "Zhou", "Xu", "Sun", "Ma", "Zhu", "Hu", "Guo", "Lin",
        "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
        "Davis", "Rodriguez", "Martinez",
    }
    bad = whitelist & en_names
    assert not bad, f"英文术语白名单含英文人名（会造成真名漏脱）：{bad}"


def test_short_slash_date_still_redacted():
    """PRE19 短斜杠守卫不得把**真实**日期放行。"""
    out = common_rules.apply_redactions("截止9/1上报")
    assert "MM/DD" in out, f"真实短日期未脱敏：{out!r}"
    out2 = common_rules.apply_redactions("验收日期3/4")
    assert "MM/DD" in out2, f"真实短日期未脱敏：{out2!r}"
