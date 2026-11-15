"""Issue #77：D-4 前缀成词层 ORG 占位符相邻人名边界漏脱。

【背景】
D-4「前缀成词层」（`org_name_rules.composite_words`）整段脱敏时直写 ASCII 占位符
`XXXX`，其首字符 `X` 是 ASCII 字母，会**打断紧邻其前的人名的右边界**
（`(?![a-zA-Z])` / `(?![a-zA-Z0-9])`）：

    「张三信息科技部」 → 「张三XXXX」   ← 人名漏脱（真实姓名泄漏）

D-7（机构实体白名单）已用私用区哨兵（`\\ue004 … \\ue005`）包裹占位符规避同类问题，
但 D-4 前缀成词层与通用机构名兜底分支未纳入，遗留本缺陷。

【解法】
机构名通道的 ORG 占位符一律以私用区哨兵包裹，流水线末尾由
`_restore_org_entity_layer` 统一还原为 `XXXX`——哨兵首字符非 ASCII 字母，
相邻人名的左右边界均保持成立。

运行：
    python3 -m pytest tests/test_issue77_org_placeholder_boundary.py -v
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import common_rules as cr  # noqa: E402
from common_rules import apply_redactions  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 人名 + 前缀成词机构实体：两者都必须脱敏
# ---------------------------------------------------------------------------

NAME_BEFORE_ORG = [
    "张三信息科技部",
    "李四信息科技部",
    "王五信息科技部",
    "郑侃信息科技部",
]


@pytest.mark.parametrize("text", NAME_BEFORE_ORG)
def test_name_before_org_entity_fully_masked(text):
    """「人名 + 前缀成词机构实体」→ 人名不得漏脱（不得出现 `张三XXXX` 形态）。"""
    out = apply_redactions(text)
    assert text[:2] not in out, f"人名漏脱：{text} -> {out}"
    assert "XXXX" in out, f"机构实体未脱敏：{text} -> {out}"
    # 不得残留「未脱敏的人名 + 占位符」混合态
    assert not any(text[:2] in out and "XXXX" in out for _ in [0]), (
        f"出现 `人名 + XXXX` 混合残态：{text} -> {out}")


def test_no_mixed_name_and_org_placeholder():
    """回归断言：输出中不得同时出现原人名与 ASCII 占位符。"""
    out = apply_redactions("张三信息科技部")
    assert out == "XXXXXXX", f"占位符形态异常：{out}"


# ---------------------------------------------------------------------------
# 2. 机构实体右侧紧邻人名：同样不得漏脱
# ---------------------------------------------------------------------------

def test_name_after_org_entity_masked():
    """机构实体脱敏后**右侧**紧邻的人名照常脱敏。"""
    out = apply_redactions("信息科技部王五")
    assert "王五" not in out, f"右侧人名漏脱：{out}"
    assert out == "XXXXXXX", f"占位符形态异常：{out}"


# ---------------------------------------------------------------------------
# 3. 通用机构名兜底分支同样哨兵化
# ---------------------------------------------------------------------------

def test_generic_org_branch_also_sentinel_wrapped():
    """通用机构名兜底分支（不在通用词表/组合表内的机构名）同样不得打断人名边界。"""
    out = apply_redactions("张三医务部")
    assert "张三" not in out, f"人名漏脱：{out}"


# ---------------------------------------------------------------------------
# 4. 幂等性与哨兵不泄漏
# ---------------------------------------------------------------------------

def test_idempotent():
    """二次脱敏不得改变结果（哨兵已在流水线末尾还原）。"""
    once = apply_redactions("张三信息科技部")
    twice = apply_redactions(once)
    assert once == twice, f"不幂等：{once} -> {twice}"


def test_sentinel_not_leaked():
    """私用区哨兵不得出现在最终产物中。"""
    out = apply_redactions("张三信息科技部")
    assert "\ue004" not in out and "\ue005" not in out, f"哨兵泄漏：{out!r}"


def test_org_placeholder_still_plain_xxxx():
    """最终产物中的机构占位符仍是 ASCII `XXXX`（对外格式不变）。"""
    out = apply_redactions("关于信息科技部的通知")
    assert out == "关于XXXX的通知", f"占位符格式变化：{out}"
