"""Issue #76：数字类结构化编号语境豁免（目录页码 / 表格序号 / 系统编号）。

【背景】
#74 关闭后，G3 残余 over 中仍有一类「结构化编号」被误遮：
    「改造34A115工程信e融服务平台」 → 「改造34AXX工程信e融服务平台」
其中 "115" 是系统改造编号「34A115」的组成部分，属**结构化编号**
（系统编号），语义上不是敏感值；而真正需要脱敏的是**独立数值型**
项目代号（如「811工程」——数字自成一体，属 R-㉖ 项目代号口径）。

【解法：D-13 结构化编号语境守卫】
`common_rules._is_structured_number_context()` 给出三类通用判据：
    ① 编号 token 内部（34A115 / 1A101 / 31V0）；
    ② 编号标签语境（编号 / 序号 / 代号 / 工单号 … 紧邻的数字）；
    ③ 目录页码锚点（PAGEREF _TocNNNN <页码> / 第N页 / 共N页）。

【安全边界（#76 硬要求 5）】
守卫**只**接入非安全类数字规则（数字型项目代号），
**绝不接入** IPv4 / 内网 URL / 身份证 / 手机号 / 固话 / 日期 / 密码 / PIN 规则
——那些是**有意脱敏项**（E6 等安全设计），放宽即安全回退。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import common_rules as cr  # noqa: E402
from common_rules import apply_redactions  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 结构化编号：必须保留（正向效果）
# ---------------------------------------------------------------------------

STRUCT_NUMBER_KEEP = [
    ("改造34A115工程信e融服务平台", "改造34A115工程信e融服务平台"),
    ("改造1A101司法查控平台改造项目", "改造1A101司法查控平台改造项目"),
    ("改造31V0工程平台上线", "改造31V0工程平台上线"),
]


@pytest.mark.parametrize("text,expect", STRUCT_NUMBER_KEEP)
def test_structured_number_kept(text, expect):
    """编号 token 内部的数字不得被误遮。"""
    out = apply_redactions(text)
    assert out == expect, f"结构化编号被误遮：{text} -> {out}"


# ---------------------------------------------------------------------------
# 2. 独立数值型项目代号：仍须脱敏（不得过度放宽）
# ---------------------------------------------------------------------------

PROJECT_CODE_MASK = [
    ("改造811工程信e融服务平台", "811"),
    ("某项目2021工程立项", "2021"),
    ("完成9999项目验收", "9999"),
]


@pytest.mark.parametrize("text,code", PROJECT_CODE_MASK)
def test_standalone_project_code_still_masked(text, code):
    """数字自成一体时仍是项目代号，必须照常脱敏。"""
    out = apply_redactions(text)
    assert code not in out, f"项目代号未脱敏：{text} -> {out}"


# ---------------------------------------------------------------------------
# 3. 安全项零回归（#76 硬要求 5）
#    IPv4 / 内网 URL / 日期 / 口令 / 手机号 / 身份证 —— 行为不得改变
# ---------------------------------------------------------------------------

SECURITY_CASES = [
    ("ftp://10.128.16.12", "10.128.16.12"),
    ("http://10.3.0.57:8080/gdums/login", "10.3.0.57"),
    ("登录DNS:192.32.1.200", "192.32.1.200"),
    ("修订日期2022-04-07", "2022-04-07"),
    ("业务补录内容为4月2日的生产交易", "4月2日"),
    ("密码：1231232", "1231232"),
    ("联系手机13812345678", "13812345678"),
    ("身份证350101199003072316", "350101199003072316"),
]


@pytest.mark.parametrize("text,secret", SECURITY_CASES)
def test_security_items_still_masked(text, secret):
    """安全类脱敏不得被 D-13 放宽。"""
    out = apply_redactions(text)
    assert secret not in out, f"安全项被放宽：{text} -> {out}"


# ---------------------------------------------------------------------------
# 4. 目录页码 / 表格序号：保持原样
# ---------------------------------------------------------------------------

PAGE_AND_INDEX_KEEP = [
    "目录 PAGEREF _Toc2599986 5",
    "第 3 页 共 12 页",
    "编号：27 详见附件",
    "序号：12 任务名称",
    "工单号：88921 已受理",
]


@pytest.mark.parametrize("text", PAGE_AND_INDEX_KEEP)
def test_page_and_index_numbers_kept(text):
    """目录页码与表格序号属结构化编号，必须原样保留。"""
    out = apply_redactions(text)
    assert out == text, f"页码/序号被误遮：{text} -> {out}"


# ---------------------------------------------------------------------------
# 5. 守卫判定（单元级）
# ---------------------------------------------------------------------------

def _ctx(text, frag, occurrence=1):
    idx = -1
    for _ in range(occurrence):
        idx = text.find(frag, idx + 1)
    return idx


def test_guard_detects_number_inside_token():
    text = "改造34A115工程信e融服务平台"
    i = _ctx(text, "115")
    assert cr._is_structured_number_context(text, i, i + 3) is True


def test_guard_rejects_standalone_number():
    text = "改造811工程信e融服务平台"
    i = _ctx(text, "811")
    assert cr._is_structured_number_context(text, i, i + 3) is False


def test_guard_detects_label_context():
    text = "编号：27 详见附件"
    i = _ctx(text, "27")
    assert cr._is_structured_number_context(text, i, i + 2) is True


def test_guard_detects_pageref_page_number():
    text = "目录 PAGEREF _Toc2599986 5"
    i = _ctx(text, " 5")
    assert cr._is_structured_number_context(text, i + 1, i + 2) is True


def test_guard_detects_page_word():
    text = "第 3 页 共 12 页"
    i = _ctx(text, "3")
    assert cr._is_structured_number_context(text, i, i + 1) is True


# ---------------------------------------------------------------------------
# 6. 规则边界：守卫不得影响配置化项目代号词表（显式指定的真项目名）
# ---------------------------------------------------------------------------

def test_configured_project_codes_not_exempted():
    """config.project_codes 是显式指定的真实项目名 → 不受 D-13 豁免。"""
    cfg = cr._load_config()
    codes = cfg.get("project_codes") or []
    if not codes:
        pytest.skip("config.project_codes 为空")
    code = codes[0]
    out = apply_redactions(f"该项目代号为{code}，属于重点项目。")
    assert code not in out, f"配置化项目代号被豁免：{code}"
