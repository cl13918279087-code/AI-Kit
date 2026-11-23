"""Issue #81：中文日期遮盖口径统一（短日期一致 / 内部测试日程保留 / 占位符可观测）。

【背景】
日期类遮盖存在三类口径问题：

A. **与人工标准版不一致（G6）**
   人工标准版对**对外里程碑日期**（上线 / 投产 / 发布）遮盖，
   对**内部测试窗口日期**（轮次 / 演练 / 测试计划）整段保留：
   | 位置 | 原文 | 标准版 | 整改前 |
   |---|---|---|---|
   | G6 | `第1轮：2011年9月6日至2011年9月14日` | 整段保留 | 遮盖（7 处 over） |
   | G3 | `本次上线演练的时间为2022年4月8日至2022年4月10日` | 整段遮盖 | 整段遮盖 ✅ |

B. **同一日期在不同上下文口径分裂（G3）**
   `4月9日5:00`（后随数字）曾**不遮盖**，`4月9日下午`（后随汉字）却遮盖。

C. **比对口径缺陷**
   `compare_with_standard.PLACEHOLDER_RE` 未收录短日期占位符 `MM月DD日`，
   使该维度在指标中既不计 agree 也不计 over/residual（不可观测）。

【整改要点】
1. 去掉短日期规则的后边界 `(?![0-9])` → 三种写法口径一致；
2. `PLACEHOLDER_RE` 补齐 `MM月DD日` / `MM/DD`；
3. 新增 `_apply_schedule_range_keep_pass` / `_restore_schedule_range_keep`：
   「第N轮 / 阶段 / 批次 / 环节 + 冒号 + 日期区间」按内部测试日程整段保留。

运行：
    python3 -m pytest tests/test_issue81_date_semantics.py -v
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import common_rules as cr  # noqa: E402
from common_rules import apply_redactions  # noqa: E402
from compare_with_standard import PLACEHOLDER_RE  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 验收 ②：短日期口径统一（后随数字 / 后随汉字 / 单独出现）
# ---------------------------------------------------------------------------

SHORT_DATE_CASES = [
    "核对时间为4月9日5:00-8:00",
    "人员4月9日下午16：00-18：00",
    "第4轮演练绿灯测试的执行时间是4月9日 11:00-15:00",
]


@pytest.mark.parametrize("src", SHORT_DATE_CASES)
def test_short_date_masked_consistently(src):
    """短日期「4月9日」在三种上下文下必须一致遮盖。"""
    out = apply_redactions(src)
    assert "MM月DD日" in out, f"短日期未遮盖：{src!r} -> {out!r}"
    assert "4月9日" not in out, f"短日期残留：{src!r} -> {out!r}"


def test_short_date_tail_number_preserved():
    """日期后的时刻数字必须原样保留（不吞时刻）。"""
    out = apply_redactions("核对时间为4月9日5:00-8:00")
    assert "5:00-8:00" in out, f"时刻被误吞：{out!r}"


# ---------------------------------------------------------------------------
# 2. 验收 ①：内部测试日程区间整段保留；对外里程碑日期仍遮盖
# ---------------------------------------------------------------------------

SCHEDULE_KEEP_CASES = [
    "第1轮：2011年9月6日至2011年9月14日",
    "第2轮：2011年9月15日至2011年9月22日",
    "第3阶段：2011年10月1日至2011年10月7日",
    "第1批次：2011年10月8日至2011年10月15日",
]


@pytest.mark.parametrize("src", SCHEDULE_KEEP_CASES)
def test_internal_schedule_range_kept(src):
    """轮次 / 阶段 / 批次引导的日期区间按标准版口径整段保留。"""
    out = apply_redactions(src)
    assert out == src, f"内部测试日程区间被误遮：{src!r} -> {out!r}"


MILESTONE_MASK_CASES = [
    "最终实现新核心系统于2011年10月15日平稳上线",
    "本次上线演练的时间为2022年4月8日至2022年4月10日",
]


@pytest.mark.parametrize("src", MILESTONE_MASK_CASES)
def test_external_milestone_date_still_masked(src):
    """对外里程碑（上线 / 投产 / 发布）日期维持遮盖。"""
    out = apply_redactions(src)
    assert "2011年10月15日" not in out and "2022年4月8日" not in out, \
        f"对外里程碑日期未被遮盖：{src!r} -> {out!r}"
    assert "YYYY年MM月DD日" in out, f"里程碑日期遮盖形态异常：{out!r}"


def test_schedule_keep_pass_does_not_leak_sentinel():
    """内部日程哨兵必须在流水线末尾还原，不得残留在输出中。"""
    src = "第1轮：2011年9月6日至2011年9月14日"
    out = apply_redactions(src)
    assert "WBSCHED" not in out, f"哨兵泄漏：{out!r}"
    assert "\x00" not in out, f"控制字符泄漏：{out!r}"


def test_schedule_keep_pass_api():
    """保留/还原成对 API 可用且可逆。"""
    kept = []
    text = "第1轮：2011年9月6日至2011年9月14日"
    masked = cr._apply_schedule_range_keep_pass(text, kept)
    assert kept and kept[0] == text
    assert "2011年9月6日" not in masked
    restored = cr._restore_schedule_range_keep(masked, kept)
    assert restored == text


# ---------------------------------------------------------------------------
# 3. 验收 ③：占位符识别表覆盖短日期（该维度在指标中可观测）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ph", ["MM月DD日", "MM/DD", "YYYY年MM月DD日", "YYYY/MM/DD"])
def test_placeholder_re_covers_short_date(ph):
    """短日期占位符必须被识别，否则该维度在指标中不可观测。"""
    assert ph in PLACEHOLDER_RE.pattern, f"占位符表缺项：{ph}"
    assert PLACEHOLDER_RE.search(ph) is not None
