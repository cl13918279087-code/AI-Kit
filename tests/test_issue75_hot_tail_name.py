"""Issue #75：hot 档尾字人名在无槽位上下文下的漏脱（G7 修剪回退）。

【背景】
#60（D-6）落地后发现一类**正交缺口**：尾字落在 hot 档的人名
（周竹婷 / 杜钰 / 胡璐）在**无槽位上下文**下仍会整段泄漏。

【根因】
规则 D / E / S3' 锚点层的名字位是贪婪的（`姓氏 + 名字汉字{1,3}`），
在「该文件由周竹婷签署」这类上下文里会把「周竹婷**签**」整体吞下；
而 G7 跨词边界守卫识别出「签署」跨词 → **整段回退保留原文**
→ 人名整段泄漏。

【解法】
G7 命中时不再一律整段回退，而是先尝试**修剪回退（trim-back）**：
命中片段的**前缀**若是**已知真名**（D16 ∪ #55 通用人名词典 ∪ D15），
则只脱敏该真名，被贪婪吞掉的尾字原样保留（尾字与其右邻字成词，
恰恰证明它属于下一个词而非名字的一部分）。

【与既有口径的正交性】
- 不改动 hot / warm / cold 字频分档（#60 口径）；
- 不改动 D-6 生僻名候选池（#60 口径）；
- 不改动 #57 三字名尾字残留判定：真名后**恰好只多 1 个尾字**才修剪，
  多于此则宁可不脱（避免制造「XXX + 残字」形态）。
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from common_rules import apply_redactions, apply_redactions_counted  # noqa: E402


# hot 档尾字的三个人名（#75 issue 复现样本）
HOT_TAIL_NAMES = ["周竹婷", "杜钰", "胡璐"]


# ---------------------------------------------------------------------------
# 1. 验收 ①：无槽位上下文召回 100%
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", HOT_TAIL_NAMES)
def test_hot_tail_name_recalled_in_bare_context(name):
    """复现语境固定为「该文件由 X 签署」，必须 100% 脱敏。"""
    ctx = f"该文件由{name}签署"
    out = apply_redactions(ctx)
    assert name not in out, f"仍泄漏：{ctx} -> {out}"
    assert out == "该文件由XXX签署", f"脱敏形态不符：{ctx} -> {out}"


@pytest.mark.parametrize("name", HOT_TAIL_NAMES)
def test_hot_tail_name_also_in_other_anchor_contexts(name):
    """其它无槽位锚点上下文同样召回（汇报 / 提交）。"""
    for ctx in (f"该文件由{name}汇报", f"该文件由{name}提交", f"工作由{name}负责"):
        out = apply_redactions(ctx)
        assert name not in out, f"仍泄漏：{ctx} -> {out}"


@pytest.mark.parametrize("name", HOT_TAIL_NAMES)
def test_bare_name_still_redacted(name):
    """裸名（无任何上下文）不受影响，仍须脱敏。"""
    out = apply_redactions(name)
    assert name not in out
    assert out == "XXX"


# ---------------------------------------------------------------------------
# 2. 验收 ②：通用语料误伤增量必须为 0
# ---------------------------------------------------------------------------

def test_generic_corpus_misfire_delta_zero():
    """通用语料不得因修剪回退产生新的姓名类误伤。

    口径：这些通用词在任何上下文都不得被脱敏。
    """
    words = [
        "报表", "工作台", "客户", "材料", "项目", "方案", "数据", "系统",
        "部门", "领导", "员工", "公司", "银行", "发展", "安全", "生产",
        "财务", "管理", "服务", "信息", "技术", "业务", "经营", "风险",
        "控制", "检查", "标准", "制度", "流程", "计划", "报告", "会议",
        "产品", "汇总", "提交", "签署", "汇报", "负责", "指向", "真实",
    ]
    misfired = []
    for w in words:
        for ctx in (f"本次议题涉及{w}等内容", f"关于{w}的说明"):
            out = apply_redactions(ctx)
            if w not in out:
                misfired.append((ctx, out))
    assert not misfired, f"误伤：{misfired}"


def test_pointer_to_real_not_misfired():
    """反例自检：「指向真实」不得被误脱（'向真' 虽在通用人名词典内，
    但左邻 '指' + '向' = 指向 成词 → 修剪回退被条件①拒绝）。"""
    ctx = "验证每个编排Skill的链路引用是否指向真实"
    out = apply_redactions(ctx)
    assert out == ctx, f"误伤：{ctx} -> {out}"


# ---------------------------------------------------------------------------
# 3. 验收 ③：hot / warm / cold 字频分类结果完全不变
# ---------------------------------------------------------------------------

def test_name_char_freq_buckets_unchanged():
    """本修复不触碰字频分档（#60 口径）：hot/warm/cold 三集合指纹固定。"""
    import common_rules as cr

    freq = cr._get_name_char_freq()
    hot = {c for c, f in freq.items() if f >= 12}
    warm = {c for c, f in freq.items() if 3 <= f < 12}
    cold = {c for c, f in freq.items() if f < 3}
    # 指纹（SHA-256 前 16 位）在修复前后一致
    assert (len(hot), len(warm), len(cold)) == (262, 431, 589)


# ---------------------------------------------------------------------------
# 4. 验收 ④：与 #57 三字名尾字残留口径正交
# ---------------------------------------------------------------------------

def test_no_residual_tail_created():
    """修剪回退不得制造「XXX + 残字」的 #57 残尾形态。

    「最后由石艳平汇总全组」中的「石艳平」是完整三字名，
    贪婪命中为「石艳平汇」——真名后多了 2 个字（平汇），
    不满足「恰好只多 1 个尾字」→ 整段回退，不得切成「XXX平」。
    """
    ctx = "最后由石艳平汇总全组发账务组"
    out = apply_redactions(ctx)
    assert out == ctx, f"制造了残尾形态：{ctx} -> {out}"
    assert "XXX平" not in out


def test_trimback_keeps_tail_char():
    """修剪回退保留被吞的尾字（尾字属于下一个词）。"""
    out = apply_redactions("该文件由周竹婷签署")
    assert out.endswith("签署"), f"尾字被误脱：{out}"
    assert "XXX签" in out


# ---------------------------------------------------------------------------
# 5. 幂等 / 确定性
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", HOT_TAIL_NAMES)
def test_idempotent(name):
    ctx = f"该文件由{name}签署"
    once = apply_redactions(ctx)
    twice = apply_redactions(once)
    assert once == twice


def test_counts_label():
    """修剪回退须计入「姓名」类计数。"""
    _, counts = apply_redactions_counted("该文件由周竹婷签署")
    assert counts.get("姓名", 0) >= 1
