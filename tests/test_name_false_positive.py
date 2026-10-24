"""
姓名检测误伤回归测试（Issue #49）

背景：
    Issue #45 将 surname_pool 196→415 字、name_pool 152→1285 字后，
    规则A（`姓 + [name_pool]{1,3}` 粗筛）的可匹配模式空间从 ≈3 万扩到 ≈53 万（18×），
    超出真实人名增量部分全部与日常汉语词汇高度重合，导致大面积误伤。

    实测（104 条通用业务语料）：
        - 扩池前规则A 误伤率 0.0%
        - 扩池后规则A 误伤率 54.8%
        - 真实业务文档回归：规则A 命中片段 42/42 不在 full_name_list 内（100% 误伤）

本文件为 CI 卡口（M4）：
    1. `test_false_positive_corpus`  —— 通用业务语料误伤率上限
    2. `test_d16_exact_layer_precision` —— D16 精确层精度
    3. `test_true_name_recall` —— 真实人名召回下限（防整改过度收紧）
    4. `test_high_risk_surnames_config` —— 高危姓配置完整性
    5. `test_name_pool_expansion_regression` —— 扩池前后行为差异守卫
    6. `test_real_doc_misfire_fragments` —— 真实文档误伤片段黑名单

运行：
    python3 -m pytest tests/test_name_false_positive.py -v
"""

import json
import os
import re
import sys
import zipfile

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import common_rules  # noqa: E402

CONFIG_PATH = os.path.join(SCRIPTS_DIR, "config.json")
REPO_ROOT = os.path.dirname(SCRIPTS_DIR)


def _load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 语料：通用业务词组（全部为非人名）
# ---------------------------------------------------------------------------

FALSE_POSITIVE_CORPUS = [
    # 业务管理类
    "成本控制", "渠道拓展", "流程优化", "日常运营", "业务发展", "项目进展",
    "风险可控", "市场推广", "合同条款", "产品发布", "数据统计", "系统测试",
    "需求变更", "培训安排", "人员名单", "部门调整", "预算执行", "质量检查",
    "团队建设", "重点工作", "年度总结", "客户服务", "技术架构", "客户经理",
    "产品经理", "技术总监", "项目计划", "风险控制", "方案评审", "成本节约",
    # 地名 / 机构名
    "宁波", "江苏", "和平", "田字", "米线", "甘泉", "尚品", "温州", "安徽",
    "方向", "平台", "水平", "平安", "康泰", "构成", "单元", "常规",
    # 姓氏 + 常用字构成的常用词
    "叶子", "子墨", "沙子", "孔明", "莫名", "明天", "白天", "白天鹅",
    "金色", "叶子涵", "常熟", "余姚", "安庆", "吉安", "车子", "车子保险",
    "孟子", "荀子", "孔子", "老子", "庄子", "墨子",
    # 单字成词 / 高频短语
    "成功", "成绩", "成本", "方案", "渠道", "日常", "业务", "流程",
    "宇宙", "元宇宙", "单元测试", "元素", "石家庄", "机制", "机理", "机动",
    "和气", "和平共处", "江苏卫视", "宁波银行", "甘肃银行", "甘泉资本",
    "尚品宅配", "车贷", "费用", "美好", "美元", "欧元", "梦想", "花呗",
    "余额", "客户端", "服务器",
]

# 真实人名样本（应被脱敏）
TRUE_NAME_SAMPLES = [
    "张伟", "艾成军", "邹阳", "陈黎", "王小红", "张明",
    "汪晶晶", "樊霖", "欧阳娜娜", "司马光", "李雷", "韩梅梅",
]

# 真实业务文档中实测到的误伤片段（Issue #49 实测黑名单）
REAL_DOC_MISFIRE_FRAGMENTS = [
    "付出", "全体成", "全部", "关联", "务功", "原则上", "周期",
    "平稳上线", "成后", "户为线", "户从开", "户开立", "支付", "方和",
    "时临时通", "来开展测", "来���进行测", "段至少测", "班前必", "程测",
    "管理", "管理和", "经理向尖", "能", "遇", "能全量测", "许在深圳", "陈述汇",
]

# 扩池前的基线配置（用于回归守卫）
BASELINE_SURNAME_COUNT = 196
BASELINE_NAME_COUNT = 152
CURRENT_SURNAME_COUNT = 415
CURRENT_NAME_COUNT = 1285


@pytest.fixture(scope="module")
def cfg():
    # Issue #49：确保不受其他测试文件的 AIKIT_CONFIG_PATH 环境影响，
    # 始终读取仓库内真实 config.json
    os.environ.pop("AIKIT_CONFIG_PATH", None)
    common_rules._CONFIG_CACHE = None
    common_rules._PATTERNS = None
    common_rules._PATTERNS_PRE = []
    common_rules._PATTERNS_NAME = []
    return _load_config()


@pytest.fixture(scope="module")
def patterns():
    """重建规则组（确保使用当前 config.json，且不受其他测试文件污染）"""
    os.environ.pop("AIKIT_CONFIG_PATH", None)
    common_rules._CONFIG_CACHE = None
    common_rules._PATTERNS = None
    common_rules._PATTERNS_PRE = []
    common_rules._PATTERNS_NAME = []
    common_rules._AGENT_DECISIONS_OVERRIDE = None
    pre, name = common_rules._get_pattern_groups()
    return pre + name


def _redact(text, patterns):
    out = text
    for pat, rep in patterns:
        out = pat.sub(rep, out)
    return out


def _rule_a_pattern(cfg):
    """单独构造规则A（姓 + [name_pool]{1,3}）用于分层归因"""
    surnames = common_rules.SURNAME_SET
    alt = "|".join(re.escape(s) for s in sorted(surnames))
    return re.compile(rf"(?<![a-zA-Z0-9])(?:{alt})[{cfg['name_pool']}]{{1,3}}")


def _d16_pattern(cfg):
    names = sorted(cfg["full_name_list"], key=len, reverse=True)
    body = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?<![a-zA-Z0-9])(?:{body})(?![a-zA-Z0-9])")


# ---------------------------------------------------------------------------
# 1. 通用语料误伤率上限
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=False,
    reason=(
        "Issue #49 P1 待实施：规则A 分级改造（低危姓{2,3} + 高危姓{3,3} + 上下文锚点）"
        "落地后，本测试将由 xfail 自动转为 XPASS，作为整改完成度验收信号。"
        "整改方案见 docs/Issue-049-姓名池误伤整改方案.md"
    ),
)
def test_false_positive_corpus(patterns):
    """端到端误伤率必须受控（当前基线 55.8%，整改目标 < 10%）"""
    hits = [c for c in FALSE_POSITIVE_CORPUS if _redact(c, patterns) != c]
    rate = len(hits) / len(FALSE_POSITIVE_CORPUS)
    assert rate < 0.10, (
        f"通用语料误伤率 {rate:.1%}（{len(hits)}/{len(FALSE_POSITIVE_CORPUS)}）超过 10% 上限。\n"
        f"误伤样本: {hits[:20]}\n"
        f"参照 Issue #49：扩池后规则A 误伤率 54.8%，分级整改后预期 8.7%（9/104）。"
    )


# ---------------------------------------------------------------------------
# 2. D16 精确层精度
# ---------------------------------------------------------------------------

def test_d16_exact_layer_precision(cfg):
    """D16 精确层应保持高精度（当前 99.0%，要求 >= 95%）"""
    d16 = _d16_pattern(cfg)
    hits = [c for c in FALSE_POSITIVE_CORPUS if d16.search(c)]
    precision = 1 - len(hits) / len(FALSE_POSITIVE_CORPUS)
    assert precision >= 0.95, (
        f"D16 精确层精度 {precision:.1%} 低于 95%。误伤: {hits}"
    )


# ---------------------------------------------------------------------------
# 3. 真实人名召回下限（防过度收紧）
# ---------------------------------------------------------------------------

def test_true_name_recall(patterns):
    """真实人名召回不得因收紧规则而显著下降（下限 8/12）"""
    missed = [n for n in TRUE_NAME_SAMPLES if _redact(n, patterns) == n]
    recall = (len(TRUE_NAME_SAMPLES) - len(missed)) / len(TRUE_NAME_SAMPLES)
    assert recall >= 8 / 12, (
        f"真实人名召回 {recall:.1%}（漏脱 {missed}）低于下限 8/12。\n"
        f"整改需保证「漏脱优先于误伤」的合规红线不被牺牲。"
    )


# ---------------------------------------------------------------------------
# 4. 高危姓配置完整性
# ---------------------------------------------------------------------------

def test_high_risk_surnames_config(cfg):
    """high_risk_surnames 必须已配置，且全部属于 surname_pool"""
    high = cfg.get("high_risk_surnames", "")
    assert high, (
        "config.json 缺少 high_risk_surnames 配置（Issue #49 M1）。"
    )
    pool = set(cfg["surname_pool"])
    missing = [c for c in high if c not in pool]
    assert not missing, f"high_risk_surnames 中有字符不在 surname_pool 内: {missing}"
    assert len(high) >= 100, f"high_risk_surnames 字数过少（{len(high)}），分级策略将失效"


# ---------------------------------------------------------------------------
# 5. 扩池回归守卫
# ---------------------------------------------------------------------------

def test_name_pool_expansion_regression(cfg):
    """守卫 Issue #45 的扩池成果不被回滚"""
    assert len(cfg["surname_pool"]) >= CURRENT_SURNAME_COUNT, (
        f"surname_pool 缩水至 {len(cfg['surname_pool'])}，应 >= {CURRENT_SURNAME_COUNT}"
    )
    assert len(cfg["name_pool"]) >= CURRENT_NAME_COUNT, (
        f"name_pool 缩水至 {len(cfg['name_pool'])}，应 >= {CURRENT_NAME_COUNT}"
    )
    assert len(cfg["full_name_list"]) >= 7000, (
        f"full_name_list 仅 {len(cfg['full_name_list'])} 条，Issue #45 成果丢失"
    )
    # 扩池前基线仅作记录，不作断言（扩池是 #45 的既定成果）
    assert BASELINE_SURNAME_COUNT < CURRENT_SURNAME_COUNT
    assert BASELINE_NAME_COUNT < CURRENT_NAME_COUNT


# ---------------------------------------------------------------------------
# 6. 真实文档误伤片段黑名单
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=False,
    reason=(
        "Issue #49 P1 待实施：规则A 分级改造落地后本测试将转为 XPASS。"
        "整改方案见 docs/Issue-049-姓名池误伤整改方案.md"
    ),
)
def test_real_doc_misfire_fragments(cfg):
    """真实业务文档中出现过的误伤片段，不应再被整体替换"""
    rule_a = _rule_a_pattern(cfg)
    names = set(cfg["full_name_list"])
    still_bad = []
    for frag in REAL_DOC_MISFIRE_FRAGMENTS:
        m = rule_a.fullmatch(frag)
        if m and frag not in names:
            still_bad.append(frag)
    # 允许少量残留（整改为渐进式），但不得过半
    assert len(still_bad) <= len(REAL_DOC_MISFIRE_FRAGMENTS) * 0.2, (
        f"仍有 {len(still_bad)}/{len(REAL_DOC_MISFIRE_FRAGMENTS)} 个真实误伤片段被规则A 捕获: {still_bad}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
