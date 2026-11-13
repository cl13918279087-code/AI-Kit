"""Issue #74：G3 残余 over 归属——目录页码/系统编号与通用业务术语被误遮盖。

【背景】
#64 验收第 2 条要求 G3 over_spans 由 171 收敛到 ≤52；D-7 关闭后实测 G3 over = 95，
差额 43 处。本 issue 承接残余 over 的**通用词类**与**银行链前缀类**两部分，
并以「通用规则」实现（不得针对单篇文档硬编码）。

【残余 over 归因（95 处）】
- 通用业务术语（anchor_expand / role_context / list_continuation / address / patterns）：约 46 处
- 银行链前缀误吞（`[汉字]{2,6}银行` 兜底 / `_BANK_BRAND_RE` 品牌词）：约 6 处
- 数字/IP/日期类结构化编号片段：约 33 处
- 机构类 / 真实姓名类残余：约 10 处

【本 issue 的两条通用规则】
1. **通用词保护表扩容**（`common_word_guards`）：
   把 G3/G6/G7 中被误遮的业务术语纳入整词保护表，命中即豁免。
   该表被姓名通道（G4 包含语义）、地址通道（G4-c 重叠语义）、
   锚点/角色/清单延续等泛用通道共享 → 一处扩容，多通道同时生效。
2. **银行链接入 G4 豁免**（D-12）：
   - `_bank_brand_repl`：品牌词命中 G4（重叠语义）→ 保留原文
     （「厦门银联」不再被切成「厦XXX联」）；
   - `_bank_name_repl`：银行名前缀命中 G4 → 整段保留
     （「发起开立银行」「自助银行」不再被吞成「XX银行」）。
   配置化银行名单（`bank_names`）为**专名**，显式不受该豁免影响。

【真名安全】
保护词入库前与 D16 全名库（7416）、#55 通用人名词典（117206）、
金标 `tests/benchmark/gold_names.json`、D15 注入名单**双向互斥**
（无任何包含关系）→ 豁免不会造成真名漏脱。
"""
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
import sys  # noqa: E402

sys.path.insert(0, str(REPO / "scripts"))

import common_rules as cr  # noqa: E402
from common_rules import apply_redactions  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 通用业务术语：不得被遮盖（#74 主体修复）
# ---------------------------------------------------------------------------

# G3/G6/G7 实测被误遮、且在多组语料中通用出现的业务术语
GENERIC_TERMS = [
    "统一支付",       # G3
    "工作台",         # G3
    "客户材料",       # G3
    "物业费代收",     # G3
    "报表查询",       # G3
    "运管操作",       # G3
    "模拟操作",       # G3
    "自助银行",       # G3
    "发起开立",       # G3
    "演练前",         # G3
    "演练开始",       # G3
    "上线方案",       # G3
    "回退",           # G3
    "批量任务",       # G3
    "案例交易链路",   # G3
    "联机接口链路",   # G3
    "厦门银联",       # G3
    "对外营业",       # G3
    "零售条线",       # G3
]


@pytest.mark.parametrize("term", GENERIC_TERMS)
def test_generic_business_term_not_masked(term):
    """通用业务术语必须原样保留（不得被遮成 XX/XXXX）。"""
    ctx = f"本环节需完成{term}的处理与确认。"
    out = apply_redactions(ctx)
    assert term in out, f"通用业务术语被误遮：{ctx} -> {out}"


@pytest.mark.parametrize("term", GENERIC_TERMS)
def test_generic_business_term_in_guard_table(term):
    """这些术语必须落在 `common_word_guards` 中（通用规则入口）。"""
    assert term in cr._get_common_word_guards(), f"未入整词保护表：{term}"


# ---------------------------------------------------------------------------
# 2. 银行链：前缀豁免（D-12）
# ---------------------------------------------------------------------------

BANK_PREFIX_KEEP = [
    ("配合发起开立银行承兑汇票的演练", "发起开立银行"),
    ("改造39A04自助银行ATM", "自助银行"),
    ("其中银行卡管理", "其中银行卡"),
    ("业部模拟活期银行卡", "模拟活期银行卡"),
    ("中本票签发及银行", "本票签发及银行"),
]


@pytest.mark.parametrize("text,keep", BANK_PREFIX_KEEP)
def test_bank_prefix_generic_not_swallowed(text, keep):
    """泛称/业务修饰前缀 + 「银行」不再被兜底规则整体吞掉。"""
    out = apply_redactions(text)
    assert keep in out, f"银行前缀被误吞：{text} -> {out}"
    assert "XX银行" not in out, f"银行前缀被误吞：{text} -> {out}"


def test_bank_brand_not_split_by_common_word():
    """「厦门银联」不被品牌词规则切成「厦XXX联」。"""
    text = "身份认证平台厦门银联接入三方测试环境"
    out = apply_redactions(text)
    assert "厦门银联" in out, f"品牌词被切开：{text} -> {out}"
    assert "厦XX" not in out, f"品牌词被切开：{text} -> {out}"


# ---------------------------------------------------------------------------
# 3. 防回归：银行专名仍必须被遮盖
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bank", ["海峡银行", "郑州银行", "中原银行"])
def test_config_bank_name_still_masked(bank):
    """D-12 豁免不得放过配置化银行专名。"""
    out = apply_redactions(f"该行名称为{bank}，位于福州。")
    assert bank not in out, f"银行专名泄漏：{bank} -> {out}"


def test_bank_suffix_context_still_masked():
    """非保护词的银行前缀仍应脱敏（如「宁波银行」）。"""
    out = apply_redactions("该账户开在宁波银行网点。")
    assert "宁波银行" not in out, f"银行专名泄漏 -> {out}"


# ---------------------------------------------------------------------------
# 4. 真名互斥：保护词不得与真名库产生包含关系
# ---------------------------------------------------------------------------

NEW_GUARD_WORDS = [
    "管理", "支付", "查询", "使用", "点击", "操作", "工作", "通过", "要求",
    "其中", "应在", "工作台", "客户材料", "统一支付", "自助银行", "网络",
    "报表查询", "模拟操作", "运管操作", "物业费代收", "演练前", "演练开始",
    "通过后", "代为", "发起开立", "零售条线", "陈述汇报", "安全稳定",
    "组成", "构成", "上线", "上线方案", "上线后", "回退", "对外营业",
    "集中办公", "担任", "完整", "规定", "汇总", "进行中",
    "批量任务", "向前线", "案例交易链路", "联机接口链路", "厦门银联",
    "模拟活期", "本票签发及",
]


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
    return d16, dict_names, gold


@pytest.mark.parametrize("word", NEW_GUARD_WORDS)
def test_new_guard_word_mutual_exclusive_with_real_names(word):
    """保护词与 D16 / #55 词典 / 金标 **双向互斥**（无任何包含关系）。"""
    d16, dict_names, gold = _name_pools()
    pools = {"D16": d16, "#55": dict_names, "gold": gold}
    for label, pool in pools.items():
        assert word not in pool, f"{label} 命中真名：{word}"
        assert not [n for n in pool if n in word], (
            f"{label} 含真名子串，保护词会造成真名漏脱：{word}"
        )
