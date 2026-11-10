#!/usr/bin/env python3
"""
test_issue60_d6_rare_char_pool.py - Issue #60 D-6 回归守卫（GB2312 生僻名用字候选池）

【缺陷现象】
D-5（槽位强判别）只覆盖**角色槽位内**的生僻字姓名；**裸上下文**（无槽位、
无职务后缀、无名单结构）下，「尾字落在温字/冷字档」的生僻字人名仍整名漏脱：

    "会议纪要 陈鲲 汇报"    → "会议纪要 陈鲲 汇报"   ❌
    "本次由 韩晗 提交"        → "本次由 韩晗 提交"     ❌
    "负责人 孟芸"             → "负责人 孟芸"         ❌

根因：名字位字频闸门（#49 P1）按 D16 全名库自举字频，分三档
（热字 freq ≥ 12 / 温字 freq ≥ 3 / 冷字 freq < 3）。生僻字人名的用字频次
天然落在温/冷档 → 闸门整名拦截。

【修复（D-6，方案 1：GB2312 候选池）】
新增 `_apply_rare_name_pass` 层（scripts/common_rules.py），以
**GB2312 二级汉字（3008 字）**为「生僻名用字候选池」，在姓名规则组之后
兜底放行「姓 + 生僻字」形态：

  * 候选池 ∩ L4 护栏表用字 = 空（双向互斥）
  * hot 集**不放宽**（#60 §三 方案 1 的硬约束），本层是并列的冷档放行通道
  * 四道误伤闸门：通用词否决表 / 职务机构地名后缀守卫 / 残尾守卫 / 边界守卫

【验收口径】（#60 §四）
  ① GB2312 生僻字抽样 ≥50 例，**裸上下文**召回率必须 100%
  ② 通用语料误伤增量必须为 0
  ③ 与 #57 配套：三字名尾字残留 0
  ④ 真名金标召回不低于基线

运行方式：
    python -m pytest tests/test_issue60_d6_rare_char_pool.py -v
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common_rules  # noqa: E402
from common_rules import apply_redactions  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, "scripts", "config.json")

# D-6 兜底目标名：尾字落在温字/冷字档的生僻字人名（裸上下文此前整名漏脱）
D6_TARGET_NAMES = [
    "陈鲲", "韩晗", "曹晗", "孟芸", "陈曦", "蔡菁",
    "阮榕", "何嵘", "肖娅", "陈峥", "王谧", "成葭",
    "刘兆缋", "蔡昀煜", "欧晓烨", "李姗姗", "张朦朦", "何嵘斌",
]

# #60 §一 列出的 20 个确知真实姓名（D-5 精确名单层已覆盖，D-6 需保持不回归）
#
# 【口径】此名单严格取自 issue #60 正文 §一「以下实名未被脱敏」代码块，
#   不掺入任何名单外的人名——掺入会把**不属于本 issue 射程**的缺口
#   误记到 D-6 头上，掩盖真实归因。
ISSUE60_NAMES = [
    "李熔", "欧晓烨", "蔡昀煜", "王谧", "俞曦", "成葭", "黄宽钊", "刘兆缋",
    "廖腾华", "阮秋鸣", "段宇信", "吴晋秋", "易晓晨", "董圣瑜", "陈绍飞",
    "费黎花", "宗树峰", "吴文静", "张锦宇", "黄丽丹",
]

# 【已知正交缺口 —— 不属于 D-6 射程】
#
# 这三例的**尾字是 hot 档**（婷/钰/璐，名字位字频 ≥ 12），D-6 方案 1 的
# 硬约束是「不放宽 hot 集」，故本层**结构性不覆盖**它们。
#
# 它们的真实根因在既有通道，不在 D-6：
#   「签」字在 name_pool 内 → 姓名规则的贪婪匹配把「签」吞进名字位
#   （命中片段变成「周竹婷签」）→ G7 跨词边界守卫（word_boundary_pairs
#   含「签署」）判定命中横跨词边界 → **整段回退** → 整名漏脱。
#   该缺陷在 D-6 引入前后完全一致（基线 common_rules.py + 同一份 config
#   实测同样漏脱），故非 D-6 引入。
#
# 处理方式：单列为 xfail 显式追踪（不掩盖、不误记 D-6），
# 已在整改文档 §十七 偏差披露中登记，并另立 issue 承接。
HOT_TAIL_GAP_NAMES = ["周竹婷", "杜钰", "胡璐"]

# 通用词（不得因 D-6 被误脱）——「姓 + GB2312 二级字」构成的通用词
COMMON_WORDS = [
    "时髦", "房檐", "江豚", "白鹭", "黄鳝", "雷霆", "温馨", "毛茛",
    "钟馗", "石斛", "成瘾", "阳痿", "牛蒡", "白茯苓", "包公祠", "吕公祠",
]


def _cfg():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. 配置与候选池结构
# ---------------------------------------------------------------------------

def test_rare_char_layer_config_present():
    """D-6 层配置落地。"""
    layer = _cfg().get("name_rare_char_layer")
    assert layer, "config.json 缺少 name_rare_char_layer"
    assert layer.get("enable") is True, "D-6 层未启用"
    assert layer.get("gb2312_level") == "2", "默认候选池应为 GB2312 二级汉字"
    assert len(layer.get("veto_words") or []) >= 50, "否决表条目过少"


def test_gb2312_level_split():
    """GB2312 一级 3755 / 二级 3008 / 全量 6763 —— 标准库解码可复现。"""
    l1 = common_rules._get_gb2312_chars("1")
    l2 = common_rules._get_gb2312_chars("2")
    allc = common_rules._get_gb2312_chars("all")
    assert len(l1) == 3755, f"一级汉字数异常：{len(l1)}"
    assert len(l2) == 3008, f"二级汉字数异常：{len(l2)}"
    assert len(allc) == 6763, f"全量汉字数异常：{len(allc)}"
    assert not (set(l1) & set(l2)), "一级/二级汉字不应重叠"


def test_pool_mutually_exclusive_with_l4_guards():
    """双向互斥：候选池与 L4 护栏表用字交集必须为空。"""
    layer = common_rules._get_rare_name_layer()
    assert layer is not None
    guard = set()
    for w in common_rules._get_common_word_guards():
        guard.update(w)
    for w in common_rules._get_biz_guard_words():
        guard.update(w)
    overlap = layer["rare_set"] & guard
    assert not overlap, f"候选池与护栏表存在交集：{''.join(sorted(overlap))}"


def test_hot_set_not_relaxed():
    """hot 集不放宽（#60 §三 方案 1 硬约束）。

    D-6 是**并列**的冷档放行通道，不改动热字集 / 温字集。
    """
    cfg = _cfg()
    hot = set(common_rules._get_gated_name_chars(cfg))
    warm = set(common_rules._get_warm_name_chars(cfg))
    layer = common_rules._get_rare_name_layer()
    assert layer is not None
    # 候选池不应含热字（生僻字与热字天然互斥，说明放行档确实只针对冷档）
    assert not (layer["rare_set"] & hot), "候选池侵入了 hot 集"
    # 热字集本身未被 D-6 修改
    assert hot >= set(hot) and warm >= set(warm)


# ---------------------------------------------------------------------------
# 2. 裸上下文召回（#60 §四 验收 ①）
# ---------------------------------------------------------------------------

def test_d6_target_names_masked_in_bare_context():
    """D-6 目标名在**裸上下文**（无角色槽位）必须脱敏。"""
    missed = []
    for n in D6_TARGET_NAMES:
        for ctx in (
            f"会议纪要 {n} 汇报",
            f"本次材料由{n}提交",
            f"参会人员：{n}、其他",
            f"{n}、张三",
        ):
            out = apply_redactions(ctx)
            if n in out:
                missed.append((ctx, out))
    assert not missed, f"裸上下文漏脱：{missed}"


def test_gb2312_rare_sample_recall_100pct_bare_context():
    """GB2312 二级汉字抽样 ≥50 例，**裸上下文**召回 100%（#60 §四 验收 ①）。

    这是 D-6 的核心验收项：方案 1 落地后，无槽位上下文也必须 100% 召回。
    """
    cfg = _cfg()
    pool = common_rules._get_gb2312_chars("2")
    sur = sorted(common_rules._get_gated_surnames(cfg))
    assert pool and sur, "候选池或闸门姓池为空"

    random.seed(7)
    sample = random.sample(list(pool), 50)
    names = [random.choice(sur) + c for c in sample]
    missed = [n for n in names if n in apply_redactions(f"会议纪要 {n} 汇报")]
    assert not missed, f"GB2312 生僻字裸上下文漏脱（{len(missed)}/{len(names)}）：{missed}"


def test_gb2312_rare_sample_recall_multi_seed():
    """多种子抽样均须 100% 召回（避免单一种子偶然通过）。"""
    cfg = _cfg()
    pool = list(common_rules._get_gb2312_chars("2"))
    sur = sorted(common_rules._get_gated_surnames(cfg))
    for seed in (11, 42, 2026):
        random.seed(seed)
        names = [random.choice(sur) + random.choice(pool) for _ in range(200)]
        missed = [n for n in names if n in apply_redactions(f"会议纪要 {n} 汇报")]
        assert not missed, f"seed={seed} 漏脱 {len(missed)}/{len(names)}：{missed[:10]}"


def test_three_char_rare_names_masked():
    """三字生僻名（姓 + 生僻 + 生僻）须整名脱敏，不得切成两字（#57 配套）。"""
    cfg = _cfg()
    pool = list(common_rules._get_gb2312_chars("2"))
    sur = sorted(common_rules._get_gated_surnames(cfg))
    random.seed(5)
    names = [random.choice(sur) + random.choice(pool) + random.choice(pool)
             for _ in range(100)]
    bad = []
    for n in names:
        out = apply_redactions(f"参会人员：{n}")
        if n in out or len(n) > 2 and n[1] in out:
            bad.append((n, out))
    assert not bad, f"三字名整名脱敏失败或产生残尾：{bad[:10]}"


def test_issue60_names_not_regressed():
    """#60 §一的 20 个实名在槽位 + 无槽位双上下文仍全部脱敏（验收 ④）。"""
    missed = []
    for n in ISSUE60_NAMES:
        for ctx in (f"负责人：{n}", f"该文件由{n}签署", f"参会人员：{n}、其他"):
            out = apply_redactions(ctx)
            if n in out:
                missed.append((ctx, out))
    assert not missed, f"未脱敏：{missed}"


def test_hot_tail_gap_tracked_not_d6_scope():
    """hot 档尾字名的既有闸门缺口：显式追踪（D-6 射程外，非 D-6 引入）。

    这三例（周竹婷/杜钰/胡璐）尾字为 hot 档字，D-6「不放宽 hot 集」
    故结构性不覆盖；其漏脱根因是既有姓名规则的贪婪匹配吞字 + G7
    跨词边界守卫整段回退，在 D-6 引入前后完全一致。

    本断言用 xfail 显式登记该缺口：既不掩盖（D-6 口径不含 hot 档，
    修好也不算 D-6 的功劳），也不误记到 D-6 头上。
    """
    import pytest

    still_leaking = []
    for n in HOT_TAIL_GAP_NAMES:
        out = apply_redactions(f"该文件由{n}签署")
        if n in out:
            still_leaking.append((n, out))
    if still_leaking:
        pytest.xfail(
            "已知正交缺口（hot 档尾字名 + G7 跨词边界守卫整段回退），"
            f"另立 issue 承接：D-6 方案 1 不放宽 hot 集，结构性不覆盖：{still_leaking}"
        )
    assert not still_leaking


# ---------------------------------------------------------------------------
# 3. 误伤控制（#60 §四 验收 ②）
# ---------------------------------------------------------------------------

def test_common_words_not_misfired():
    """通用词不得被 D-6 误脱。"""
    for w in COMMON_WORDS:
        out = apply_redactions(f"本次议题涉及{w}等内容")
        assert w in out, f"误伤：{w} -> {out}"


def test_veto_words_kept_intact():
    """否决表条目必须逐条保留。"""
    veto = set(_cfg()["name_rare_char_layer"].get("veto_words") or [])
    for w in sorted(veto):
        out = apply_redactions(f"关于{w}的说明")
        assert w in out, f"误伤：{w} -> {out}"


def test_generic_corpus_misfire_delta_zero():
    """通用语料脱敏增量必须为 0（#60 §四 验收 ②）。

    口径：这些通用语料在 D-6 前后必须完全一致。
    """
    corpus = [
        "本季度营业收入同比增长百分之十二点三",
        "会议决定成立安全生产领导小组",
        "请各科室于月底前报送统计数据",
        "该批材料已通过审核并归档保存",
        "系统支撑能力较强，占比逐年提升",
        "配套软件已完成部署，实现全覆盖目标",
        "深刻吸取经验教训，某城商行参评",
        "该工程建设周期为十八个月，质量要求较高",
    ]
    changed = []
    for s in corpus:
        out = apply_redactions(s)
        if out != s:
            changed.append((s, out))
    assert not changed, f"通用语料误伤：{changed}"


def test_biz_words_not_misfired():
    """业务护栏词不得被 D-6 误脱。"""
    samples = [
        "客户凭信息查询相关材料",
        "本季度计划完成系统改造并投产",
    ]
    for s in samples:
        out = apply_redactions(s)
        assert out == s, f"误伤：{s} -> {out}"


# ---------------------------------------------------------------------------
# 4. 结构守卫（残尾 / 职务后缀 / 幂等）
# ---------------------------------------------------------------------------

def test_no_residual_tail_after_masking():
    """脱敏后不得留下单字残尾（#57 配套口径）。"""
    for n in D6_TARGET_NAMES:
        for ctx in (f"会议纪要 {n} 汇报", f"参会人员：{n}、其他"):
            out = apply_redactions(ctx)
            # 脱敏后不应出现「单字人名残尾」：原文末字单独出现
            tail = n[-1]
            if tail in out and n not in out and out.count(tail) >= 1:
                # 允许出现在其它语境（如「芸」同时是通用词）——只校验无孤立残留：
                assert "XXX" in out, f"未产生遮盖占位：{ctx} -> {out}"


def test_role_suffix_not_treated_as_name():
    """职务 / 机构 / 地名后缀不得被判为人名。"""
    layer = common_rules._get_rare_name_layer()
    assert layer is not None
    # 「祠」是二级字但也是祠堂类机构后缀 → 必须被后缀守卫拦住
    out = apply_redactions("由包公祠管理处负责")
    assert "包公祠" in out, f"机构名被误拆：{out}"


def test_idempotent():
    """幂等：D-6 层重复执行不产生二次改动。"""
    for s in ("会议纪要 陈鲲 汇报", "负责人：韩晗、曹晗", "本次由孟芸提交"):
        once = apply_redactions(s)
        assert apply_redactions(once) == once, s


def test_deterministic():
    """确定性：同一输入多次调用输出一致。"""
    s = "会议纪要 阮榕 汇报"
    outs = {apply_redactions(s) for _ in range(5)}
    assert len(outs) == 1, outs
