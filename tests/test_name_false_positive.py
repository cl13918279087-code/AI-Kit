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
# M9（Issue #49）：原值 1285 含 2 个标点污染字符（`、` `/`），
# 实际有效字数为 1283。守卫口径改为「有效汉字数」，
# 既防 #45 扩池成果回滚，又不因 M9 的污染清理而误报。
CURRENT_NAME_COUNT = 1283


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


# ---------------------------------------------------------------------------
# 7. M9 字池清洗守卫（Issue #49）
# ---------------------------------------------------------------------------

def test_name_pool_punctuation_removed(cfg):
    """M9：config.json 的 name_pool 不得含标点/符号类污染字符。

    根因：#45 扩池把 `、` `/` 误采进 name_pool，规则A 的名字字符类
    `姓 + [name_pool]{1,3}` 会把「李思远、」当成 4 字姓名，
    导致名单整段吞并。
    """
    bad = [ch for ch in cfg["name_pool"] if common_rules._is_name_pool_pollution(ch)]
    assert not bad, (
        f"name_pool 含标点/符号类污染字符 {bad}（Issue #49 M9）。\n"
        f"这些字符会被规则A 当作「名字用字」，造成名单整段吞并。"
    )


def test_name_pool_sanitizer_isolates_punctuation():
    """M9：载入期清洗器必须剔除标点，保留汉字与占位符字母。

    这是**结构性防护**：字池由人工维护，后续任何一次扩池都可能再次
    混入标点。只改 config.json 等于把「下次再犯」留给未来维护者。
    """
    clean = common_rules._clean_name_pool
    # 标点必须被剔除
    assert clean("伟、/张") == "伟张"
    assert clean("A·B") == "AB"
    assert clean("【】{}") == ""
    # 汉字与占位符大写字母必须保留
    assert clean("测试X光") == "测试X光"
    assert clean("正常汉字") == "正常汉字"
    # 占位符集只有大写，小写字母不属于占位符字符集
    assert clean("abc-def") == ""
    # 去重且保持原顺序
    assert clean("王王李李张") == "王李张"


def test_name_pool_cleaner_applied_in_rules():
    """M9：规则层实际使用的名字字符类必须已清洗（防接入点遗漏）。

    守卫口径：直接检查「规则层取用的池字符串」本身已清洗，
    而非从编译后的正则里反推字符类 —— 后者会与 D16 的
    边界守卫类 `[a-zA-Z0-9]` 混淆，误报。
    """
    cleaned = common_rules.get_name_pool_chars()
    bad = [c for c in cleaned if common_rules._is_name_pool_pollution(c)]
    assert not bad, f"规则层取用的 name_pool 仍含污染字符: {bad!r}"

    # 规则A 的名字字符类必须完全来自清洗后的池
    pool = set(cleaned)
    assert pool, "清洗后的 name_pool 为空，配置异常"
    # 编译后的规则能正常工作（字符类未被破坏成非法正则）
    pre, name = common_rules._get_pattern_groups()
    assert name, "名字规则组为空，守卫失效"


def test_list_swallow_not_regression():
    """M9：名单顿号场景不得再整段吞并（修复「负责需求梳理」被吃掉）。"""
    text = "项目组成员如下：李思远、周慧敏、王根利负责需求梳理与方案设计"
    out = common_rules.apply_redactions(text)
    assert "负责需求梳理" in out, (
        f"名单顿号场景仍整段吞并，后续业务词被误吃: {out}"
    )
    # 三个名单项应各自脱敏，且顿号保留
    assert out.count("XXX") == 3, f"名单项数不符: {out}"


def test_empty_name_pool_fallback_no_indexerror():
    """空 name_pool 不得触发 IndexError（连词规则分组编号被破坏）。

    回归背景：name_pool 为空时名字字符类退化为 `[]`，Python re 把 `]`
    当作字符类首字符处理，字符类一路吞到下一个 `]`，导致
    `(surname+names)(连词)(surname+names)` 的 3 个分组塌成 1 个，
    `_conj_repl` 调用 `m.group(2)` 抛 `IndexError: no such group`。
    """
    cfg = common_rules._load_config()
    original = cfg.get("name_pool")
    original_cache = common_rules._role_conj_re_cache
    try:
        cfg["name_pool"] = ""
        common_rules._role_conj_re_cache = None

        # 兜底后仍须有可用名字池
        chars = common_rules._get_name_pool_chars(cfg)
        assert chars, "空池未回退到内置兜底池"

        # 名字字符类必须是合法正则（非空字符集）
        cls = common_rules._build_name_char_class(chars)
        assert cls not in ("[]", ""), f"名字字符类退化: {cls!r}"

        # 分组数必须保持 3，且替换回调不得抛 IndexError
        pattern = common_rules._get_role_conjunction_re()
        assert pattern.groups == 3, (
            f"连词正则分组数应为 3，实际 {pattern.groups}（字符类未正确闭合）"
        )

        counts = {}
        text = "项目组成员如下：李思远、周慧敏负责需求梳理"
        out = common_rules.apply_redactions(text)
        assert "负责需求梳理" in out, f"空池下业务词被吞: {out}"
    finally:
        if original is None:
            cfg.pop("name_pool", None)
        else:
            cfg["name_pool"] = original
        common_rules._role_conj_re_cache = original_cache


def test_name_char_class_escapes_metacharacters():
    """名字字符类必须转义正则元字符，污染字符不得破坏字符类边界。

    核心不变量：即使池中混入 `]`、`^`、`-`、反斜杠，字符类仍须是
    **单一合法字符集**（不多不少一个闭合方括号），否则会像空池的 `[]`
    那样一路吞并后续正则文本、破坏分组编号。
    """
    import re
    cls = common_rules._build_name_char_class("伟]张-^李\\")
    compiled = re.compile(cls)
    # 正常汉字仍可匹配
    assert compiled.match("伟"), "转义后无法匹配正常汉字"
    # 关键：字符类未被污染字符撑开 —— 池外字符不得被误纳入
    assert not compiled.match("A"), f"字符类被污染撑开，纳入了池外字符: {cls}"
    # 字符集大小应等于池字符数（去重后），说明未吞入额外字符
    assert compiled.pattern.count("[") == 1 and compiled.pattern.count("]") == 2, (
        f"字符类括号不配对: {compiled.pattern}"
    )


# ---------------------------------------------------------------------------
# 9. Issue #49 P1 分层规则回归（2026-10-26 实证确认）
# ---------------------------------------------------------------------------

def test_d16_exact_layer_allows_following_digit():
    """D16 精确层右边界不得因「名字后紧跟数字」而漏脱。

    回归背景：原右边界为 `(?![a-zA-Z0-9])`，把「强峰4.3业务准备」这类
    「名字+数字」形态整类拦掉——119 份真实文档中导致 60/824 条金标漏脱。
    数字并非英文，应允许其跟随；字母仍需排除（库内 15 条带字母后缀的条目
    如 刘磊CL/李丹JYEB 已按长名优先整体匹配）。
    """
    cfg = _load_config()
    names = cfg.get("full_name_list") or []
    assert names, "config.json 缺少 full_name_list"
    # 挑一个库内真名（排除字段标签类条目），构造「真名+数字」形态
    target = None
    for n in names:
        if (len(n) == 2 and all("一" <= c <= "鿿" for c in n)
                and n not in common_rules._D16_NON_NAME_ENTRIES):
            target = n
            break
    assert target, "库内无可用的双字中文全名"
    out = common_rules.apply_redactions(f"{target}4.3业务准备")
    assert target not in out, (
        f"「{target}4.3」未被脱敏——D16 右边界错误排除了数字跟随: {out}"
    )


def test_list_block_rescue_keeps_separator_and_masks_all():
    """名单块救援必须保留顿号，且块内各项**全部**脱敏。

    回归背景：S2 名单块救援的回调有两个缺陷——
      1. `re.split` 未保留分隔符 → 顿号被吞掉；
      2. 只替换非 D16 项、保留 D16 项 → 替换占位符 `XXX`（ASCII 字母）
         紧贴保留的 D16 名，使 D16 层右边界 `(?![a-zA-Z0-9])` 失效，
         该 D16 名永远匹配不到（实测金标保留率从 88.7% 掉到 63.7%）。
    现改为「块内各项统一替换」，与 D16 层的执行顺序彻底解耦。
    """
    text = "项目组成员如下：郑云、杨瑜"
    out = common_rules.apply_redactions(text)
    assert "、" in out, f"名单顿号被吞掉: {out}"
    assert "郑云" not in out, f"块内确认真名未脱敏: {out}"
    assert "杨瑜" not in out, f"块内非确认真名未脱敏: {out}"


def test_list_block_rescue_no_false_positive_on_business_words():
    """名单块救援不得把业务词并列误判为名单。"""
    text = "本次调整涉及成本控制、需求变更两项内容"
    out = common_rules.apply_redactions(text)
    assert "成本控制" in out, f"业务词被误脱敏: {out}"
    assert "需求变更" in out, f"业务词被误脱敏: {out}"


def test_d16_non_name_entries_filtered():
    """D16 库中的字段标签类条目（如「姓名」）不得被当作人名。

    回归背景：库中混入了「姓名」这一字段标签，导致「请填写姓名」→「请填写XXX」。
    修复方式为显式清单过滤（_D16_NON_NAME_ENTRIES），而非改数据文件——
    上游修正 full_name_list 后可自动恢复。
    """
    assert "姓名" in common_rules._D16_NON_NAME_ENTRIES, (
        "D16 非人名条目过滤清单丢失「姓名」"
    )
    out = common_rules.apply_redactions("请填写姓名")
    assert "请填写姓名" in out, f"字段标签被误脱敏: {out}"


def test_name_rule_mode_graded_is_default():
    """默认必须走 graded（分层）模式，strict 仅作应急回退。"""
    cfg = _load_config()
    assert cfg.get("name_rule_mode") == "graded", (
        f"name_rule_mode 应为 graded，实际 {cfg.get('name_rule_mode')!r}"
    )


def test_list_block_regex_captures_whole_block():
    """名单块正则必须**真正捕获整块**，而不是只捕获「第一项+分隔符」。

    回归背景：S2/S4 的块正则原写作 `(?:sep(?=item))+`——用**前瞻**表达
    「分隔符 + 项」，导致捕获组 `block` 只含「第一项 + 分隔符」，
    整个块并未被捕获。后果是本规则赖以成立的**块内一致性证据整体失效**：
      S2「块内含 D16 确认真名」退化为「该单项是否在 D16」
      S4「块内每一项都符合严格形态」退化为「该单项符合即可」
    实测症状：真实文档「王松/吴学亮/叶新林/马娟」中，
    块只捕获到「叶新林/」，叶新林反而漏脱。

    本测试直接断言块正则捕获到的是**整块**。
    """
    text = "王松/吴学亮/叶新林/马娟"
    out = common_rules.apply_redactions(text)
    # 整块脱敏：四项全部消失，斜杠保留
    for name in ("王松", "吴学亮", "叶新林", "马娟"):
        assert name not in out, f"名单块整块未脱敏（{name} 残留）: {out}"
    assert "/" in out, f"分隔符被吞掉: {out}"


def test_d16_left_boundary_allows_ascii_prefix():
    """D16 精确层左边界必须允许 ASCII/数字/标点前缀（3 字及以上）。

    回归背景：原左边界 `(?<![a-zA-Z0-9])` 把「ASCII/数字 + 姓名」形态
    整类拦掉。真实文档里这类形态极常见（表格里姓名紧贴系统代号/人数）：
      IDATA-BDS季海涛 / FB-APPSVR卫海啸 / 研发组11张海翔
    实测导致 119 份文档中多条**库内确认真名**漏脱。

    修正为按长度分层：2 字名保持严格左边界（短名易与常用词碰撞），
    3 字及以上放宽为 `(?<![\\u4e00-\\u9fa5])`（只排除紧跟汉字）。
    """
    # 3 字库内名 + ASCII 前缀 → 必须被脱敏
    out = common_rules.apply_redactions("IDATA-BDS季海涛")
    assert "季海涛" not in out, f"ASCII 前缀导致库内真名漏脱: {out}"
    # 数字前缀同样放行
    out = common_rules.apply_redactions("研发组11张海翔")
    assert "张海翔" not in out, f"数字前缀导致库内真名漏脱: {out}"


def test_d16_short_name_keeps_strict_left_boundary():
    """2 字库内名必须保持严格左边界，不得因放宽而误伤常用词。"""
    # 「白天鹅」中的「白天」——若 D16 有「白天」则不应误伤
    cfg = _load_config()
    fl = cfg.get("full_name_list") or []
    if "白天" not in fl:
        pytest.skip("D16 库中无「白天」，跳过该断言")
    out = common_rules.apply_redactions("白天鹅")
    assert "白天鹅" in out, f"2 字库内名边界过松导致误伤: {out}"


# ---------------------------------------------------------------------------
# 9. P2 业务词保护守卫（Issue #49 剩余 17 处业务词误伤）
# ---------------------------------------------------------------------------

# #49 实测复现出的业务词误伤场景，逐条锁定不得回滚。
# 每条形如 (原文, 必须保留的片段)。
BIZ_GUARD_CASES = [
    # 跨词边界切词误伤：业务词被从中间切开
    ("支付结算规则", "支付"),
    ("上述所有账户", "上述"),
    ("以下为白名单", "以下"),
    ("全部余额信息", "全部"),
    ("其他相关材料", "其他"),
    # 名单块纯形态误判：业务枚举被当成人员名单
    ("白名单、单位", "白名单"),
    ("活期、定期", "活期"),
    ("定期、活期", "定期"),
    ("票据池、商票保贴", "票据池"),
    # 银行渠道名误判
    ("电子银行", "电子银行"),
    ("电子银行部", "电子银行部"),
    # 组织后缀误判（授信管理部 → 管理部）
    ("授信管理部", "授信"),
]


def test_biz_guard_words_config_exists():
    """config.json 必须提供 biz_guard_words 业务词保护表。"""
    cfg = _load_config()
    words = cfg.get("biz_guard_words")
    assert words, "config.json 缺少 biz_guard_words（Issue #49 P2）"
    assert isinstance(words, list) and all(isinstance(w, str) for w in words)
    assert len(words) >= 50, f"业务词保护表规模异常小：{len(words)}"


def test_biz_guard_table_has_no_real_names():
    """业务词保护表**不得含任何真名**。

    这是守卫的安全性前提：若表里混入了真名，守卫会把它当业务词放过，
    直接造成漏脱（合规红线）。故须与 D16 全名库双向无交集。
    """
    cfg = _load_config()
    words = set(cfg.get("biz_guard_words") or [])
    d16 = set(cfg.get("full_name_list") or [])
    overlap = words & d16
    assert not overlap, f"业务词保护表与 D16 全名库有交集 {sorted(overlap)}（会造成真名漏脱）"


def test_biz_guard_blocks_business_word_misfire():
    """业务词不得被误脱敏（#49 剩余 17 处误伤的回归守卫）。"""
    for text, keep in BIZ_GUARD_CASES:
        out = common_rules.apply_redactions(text)
        assert keep in out, f"业务词被误脱敏（应保留「{keep}」）: {text!r} -> {out!r}"


def test_biz_guard_does_not_hide_real_names():
    """业务词守卫不得连带放过真实姓名。

    反向守卫：守卫只拦「命中片段本身/跨界落在业务词上」的情况，
    真名不在业务词表内，必须照常脱敏。
    """
    for name in ("王素楠", "李思远", "周慧敏", "窦磊", "王根利"):
        out = common_rules.apply_redactions(f"负责人：{name}")
        assert name not in out, f"真名被业务词守卫连带放过: {name} -> {out}"


def test_biz_guard_indicator_words_preserved():
    """指示词/代词不得被当作姓名切出。"""
    for text in ("上述所有", "以下如下", "相关所有", "其他其他"):
        out = common_rules.apply_redactions(text)
        assert out == text, f"指示词被误脱敏: {text!r} -> {out!r}"


def test_biz_guard_perf_not_degraded():
    """业务词守卫不得造成显著性能退化（窗口扫描须生效）。"""
    import time
    text = "客户经理张伟提交了授信审批材料，涉及金额与账户余额。" * 40
    t0 = time.time()
    common_rules.apply_redactions(text)
    elapsed = time.time() - t0
    assert elapsed < 3.0, f"业务词守卫导致性能退化：{elapsed:.2f}s"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
