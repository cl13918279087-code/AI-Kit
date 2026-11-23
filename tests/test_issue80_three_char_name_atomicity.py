"""Issue #80：3 字人名的原子化遮盖（不留「遮前 2 字留 1 字」残尾）。

【背景】
3 字真名曾被**部分遮盖**：

| 语料 | 原文 | 人工标准版 | 整改前 | 缺陷 |
|---|---|---|---|---|
| G7 | `组员：张莉华、皇甫义楠…` | `XXX`（整 3 字） | `XX华` | 名单块 3 字项只遮前 2 字 |
| G3 | `…彭圣草…` | `XXX`（整 3 字） | `XX草` | 同上 |
| G7 | `科技运营：赤帅超` | 整段保留 | `赤XX` | 罕见姓氏被反向切分 |
| G3 | `…数据仓库高项韬…` | 整 3 字遮盖 | `XX`（只遮 `项韬`） | D16 3 字层左边界过严 |

【整改要点】
- 新增**姓名原子化通道**（`_apply_name_atomic_pass`）：
  ① 顿号姓名块整块原子化（允许非池字尾，如 `草`/`淋`）；
  ② 机构后缀 / 角色锚点后的单点槽位原子化（尾字须在池内）；
- D16 3 字及以上层左边界放宽（允许汉字左邻），2 字层不变；
- 通用人名词典层左邻姓氏守卫补齐罕见姓氏集（`赤` 等）；
- 名单块类规则（S2 / S4）豁免**块级** G7 跨词边界判定，改为**逐项**判定。

【原则】
3 字姓名的判定单元是**整串 3 字**：要么整串遮盖，要么整段保留，
不允许出现「遮 2 字留 1 字」的半遮盖形态（#57 口径：残尾可反查）。

运行：
    python3 -m pytest tests/test_issue80_three_char_name_atomicity.py -v
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import common_rules as cr  # noqa: E402
from common_rules import apply_redactions  # noqa: E402


def _assert_no_half_mask(src: str, out: str, name: str) -> None:
    """断言不存在「遮 2 字留 1 字」的半遮盖：name 的前 2 字 / 后 2 字
    不得以明文形式残留在输出中。"""
    for cut in (name[:2], name[-2:]):
        assert cut not in out, (
            f"检测到半遮盖残尾：{name} 的片段 {cut!r} 残留\n"
            f"  src={src!r}\n  out={out!r}")


# ---------------------------------------------------------------------------
# 1. 验收 ①：3 字真名整串遮盖，无残尾
# ---------------------------------------------------------------------------

# 「机构后缀 + 3 字真名」形态
SLOT_CASES = [
    ("数据仓库高项韬计划财务部改造", "高项韬"),
]

# 「顿号姓名块」形态：含 3 字真名（尾字可在池内，也可在池外）
BLOCK_CASES = [
    ("组员：张莉华、牛文沛、曹元", ["张莉华"]),
    ("组员：方之昕、程仁资、皇甫义楠、丁骏", ["皇甫义楠"]),
    ("副组长：张斌、俞如忠、储量", ["俞如忠"]),
    ("计划财务部陈韬、林秀萍改造", ["陈韬", "林秀萍"]),
]


@pytest.mark.parametrize("src,name", SLOT_CASES)
def test_three_char_name_in_org_slot_fully_masked(src, name):
    """机构后缀后的 3 字真名必须整串遮盖。"""
    out = apply_redactions(src)
    assert name not in out, f"3 字真名未遮盖：{src} -> {out}"
    _assert_no_half_mask(src, out, name)


@pytest.mark.parametrize("src,names", BLOCK_CASES)
def test_three_char_name_in_list_block_fully_masked(src, names):
    """名单块内的 3 字真名必须整串遮盖（含 4 字复姓全名）。"""
    out = apply_redactions(src)
    for name in names:
        assert name not in out, (
            f"3 字真名未整串遮盖：{name}\n  src={src!r}\n  out={out!r}")
        _assert_no_half_mask(src, out, name)


def test_compound_surname_name_not_truncated():
    """复姓 4 字全名（皇甫义楠）不得被截成 3 字 + 残尾。"""
    out = apply_redactions("组员：方之昕、皇甫义楠、丁骏")
    assert "义楠" not in out, f"复姓全名被截断：{out!r}"
    assert "楠" not in out, f"复姓全名留下单字残尾：{out!r}"


def test_atomic_block_requires_closed_run():
    """名单块（含 3 字项）整块遮盖，块内各项均不留残尾。"""
    src = "组员：陈曦、李雅丽、叶助茂、徐禅"
    out = apply_redactions(src)
    for name in ("叶助茂", "徐禅", "陈曦", "李雅丽"):
        assert name not in out, f"名单块内 {name} 残尾：{out!r}"
        _assert_no_half_mask(src, out, name)


def test_atomic_pass_keeps_surrounding_business_words():
    """原子化通道不得吞掉姓名块后面的业务词。"""
    src = "计划财务部陈韬、林秀萍改造76B74数据仓库"
    out = apply_redactions(src)
    assert "改造" in out, f"上下文被误伤：{src!r} -> {out!r}"
    assert "数据仓库" in out, f"上下文被误伤：{src!r} -> {out!r}"


# ---------------------------------------------------------------------------
# 2. 验收 ②：罕见姓氏不被反向切分（赤帅超 整段保留，与标准版一致）
# ---------------------------------------------------------------------------

def test_rare_surname_name_not_reverse_split():
    """「赤帅超」不得被切成「赤 + 帅超」。

    人工标准版对该真名**整段保留**（不在标准版人名库内），故整改后我方
    同样整段保留；若出现「赤XX」即属反向切分（罕见姓氏 `赤` 残留）。
    """
    src = "科技运营：赤帅超"
    out = apply_redactions(src)
    assert out == src, f"罕见姓氏被反向切分（应为整段保留）：{src!r} -> {out!r}"


def test_rare_surname_guard_set_contains_chi():
    """罕见姓氏集必须含 `赤`（#80 根因 3）。"""
    assert "赤" in cr._RARE_SURNAME_SET
    assert "赤" in cr.SURNAME_SET or "赤" in cr._RARE_SURNAME_SET


# ---------------------------------------------------------------------------
# 3. 通道存在性：原子化通道必须已接入主链路
# ---------------------------------------------------------------------------

def test_atomic_pass_is_wired():
    """`_apply_name_atomic_pass` 必须存在于主链路（否则整改未生效）。"""
    assert hasattr(cr, "_apply_name_atomic_pass")
    assert hasattr(cr, "_atomic_block_pass")
    assert hasattr(cr, "_atomic_slot_pass")
    src = "副组长：张斌、俞如忠、储量"
    assert "俞如忠" not in apply_redactions(src)


# ---------------------------------------------------------------------------
# 4. 护栏：不得因原子化通道造成业务词误遮
# ---------------------------------------------------------------------------

KEEP_CASES = [
    "改造13A128司法查控平台强峰营运管理部",
    "科技运营：赤帅超",
]


@pytest.mark.parametrize("src", KEEP_CASES)
def test_no_new_false_positive(src):
    """原子化通道不得遮盖业务短语/机构名。"""
    out = apply_redactions(src)
    for word in ("改造", "平台", "科技运营"):
        if word in src:
            assert word in out, f"业务词被误遮：{src!r} -> {out!r}"
