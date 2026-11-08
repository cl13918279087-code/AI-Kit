#!/usr/bin/env python3
"""
test_issue63_missing_entity_rules.py - Issue #63 回归守卫（批次二 D-8 机构实体 + D-9 数字口令）

【缺陷现象】
  ① D-9：独立出现的数字口令未被脱敏
        "初始密码：123123"  → 原文保留   ❌
        "系统初始口令888888" → 原文保留   ❌
  ② D-8：机构实体词混在通用词通道里被拆分，留下混合残态
        "董事会办公室" → "XXXX办公室"（人工标准版：整段保留）  ❌
        "纪检监察组"   → "XXX监察组"                        ❌
        "广州农村商业银行" → 原文保留（银行全称未建词典）       ❌
        "工行网点"      → 原文保留（银行简称未建词典）         ❌

【修复】
  D-9（`_apply_digital_password_pass`）：脱敏条件 =
       「所在片段（tab/| 分隔）只含该数字」且数字具口令形态（全同 / 周期 2、3 重复），
       或「同段落出现口令语义词（口令/密码/初始/默认/临时/登录/重置/测试/账号/pwd/…）」；
       日期形态（8 位 YYYYMMDD、6 位 YYYYMM、2011-10-12）一律保留；
       连号（123456）不作为形态证据，避免金额/序列号误伤。
  D-8（`_apply_org_entity_pass` / `_restore_org_entity_words`）：
       bank_entities = 银行全称（整段脱敏，保留「银行」尾字）；
       bank_abbreviations = 银行简称（整段脱敏）；
       entity_words = 通用部门名（**整段保留**，sentinel 占位保护后还原）。

运行方式：
    python -m pytest tests/test_issue63_missing_entity_rules.py -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common_rules  # noqa: E402
from common_rules import apply_redactions  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, "scripts", "config.json")


def _rules():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("org_name_rules") or {}


# ---------------------------------------------------------------------------
# 1. 配置：三张清单落地
# ---------------------------------------------------------------------------

def test_entity_tables_present():
    r = _rules()
    assert r.get("bank_entities"), "缺少 bank_entities"
    assert r.get("bank_abbreviations"), "缺少 bank_abbreviations"
    assert r.get("entity_words"), "缺少 entity_words"
    assert "广州农村商业银行" in r["bank_entities"]
    assert "工行" in r["bank_abbreviations"]
    assert "董事会办公室" in r["entity_words"]


def test_ambiguous_abbreviations_excluded():
    """东亚（地名）、平安/民生/华夏/广发/兴业/渤海（常用词）不得进简称表。"""
    abbr = set(_rules()["bank_abbreviations"])
    for w in ("东亚", "平安", "民生", "华夏", "广发", "兴业", "渤海"):
        assert w not in abbr, f"{w} 不应作为机构简称词典条目"


# ---------------------------------------------------------------------------
# 2. D-9 数字口令
# ---------------------------------------------------------------------------

def test_patterned_digits_masked():
    for s in ("123123", "888888", "456456", "121212"):
        out = apply_redactions(s)
        assert "******" in out, f"{s} -> {out}"
        assert s not in out, f"{s} 未脱敏"


def test_password_context_masked():
    cases = [
        "初始密码：123123",
        "系统初始口令888888",
        "账号：88888888",
        "默认密码 666666",
    ]
    for s in cases:
        out = apply_redactions(s)
        assert "******" in out, f"{s} -> {out}"


def test_dates_kept():
    """日期形态不得被数字口令通道误脱。

    注意：`2011-10-12` 命中既有的日期归一化规则（→ `YYYY/MM/DD`），属既有行为，
    此处只断言它**不被口令通道**脱成 `******`。
    """
    for s in ("日期20111012", "20111012", "年月201110", "版本号 20240101"):
        out = apply_redactions(s)
        assert out == s, f"日期/版本形态被误脱：{s} -> {out}"
    out = apply_redactions("2011-10-12")
    assert "******" not in out, out


def test_business_numbers_kept():
    for s in ("工号 123456", "序列号 12345678", "金额 1234567 元", "合同编号 2022031501"):
        out = apply_redactions(s)
        assert out == s, f"业务编号被误脱：{s} -> {out}"


def test_sequential_digits_not_evidence():
    """连号不作为口令形态证据。"""
    out = apply_redactions("工号 123456")
    assert "123456" in out


def test_compound_business_code_kept():
    """【批次二 G5 回归】复合业务编号（数字-中文描述）不得被口令通道脱敏。

    根因：D-9 数字正则只排除 `\\w`，`-` 不在其中；
    且口令语义词原按**整行**扫描，XML 感知路径下整张 sheet 视作一行，
    A1 单元格的「账号」会让 B31 的业务编号被误脱。
    """
    for s in ("（050103-往账处理）",
              "（051512-网银跨行转账异常处理）",
              "进入前台系统对上述所有交易进行撤销（050103-往账处理）"):
        out = apply_redactions(s)
        assert out == s, f"复合业务编号被误脱：{s} -> {out}"


def test_password_context_across_adjacent_cells():
    """相邻单元格（标签-密码 / 密码-值）场景仍须脱敏。"""
    xml = ('<c r="A1"><v>登录密码</v></c><c r="B1"><v>888888</v></c>')
    out = common_rules.apply_redactions_xml_aware(xml)[0]
    assert "888888" not in out, f"相邻单元格口令未脱敏：{out}"
    assert "******" in out, out


def test_unrelated_cell_keyword_does_not_mask_business_number():
    """【批次二 G5 回归】同 sheet 内**无关**单元格的口令语义词不得跨格命中。

    复现原缺陷：`<c r="B31">…（050103-往账处理）</v></c><c r="A1"><v>账号管理说明</v></c>`
    原实现按整行扫描 → 业务编号被脱成 `（******-往账处理）`。
    """
    xml = ('<c r="B31"><v>进入前台系统对上述所有交易进行撤销（050103-往账处理）</v></c>'
           '<c r="A1"><v>账号管理说明</v></c>')
    out = common_rules.apply_redactions_xml_aware(xml)[0]
    assert "050103" in out, f"业务编号被跨单元格误脱：{out}"


# ---------------------------------------------------------------------------
# 3. D-8 机构实体
# ---------------------------------------------------------------------------

def test_bank_full_name_masked():
    out = apply_redactions("广州农村商业银行")
    assert "广州农村商业银行" not in out
    assert "银行" in out, f"应保留「银行」尾字：{out}"


def test_bank_abbreviation_masked():
    for s in ("工行", "中行", "建行", "交行", "邮储行", "招行", "浦发"):
        out = apply_redactions(s)
        assert out != s, f"{s} 未脱敏"
        assert "银行" not in out or "XXXX" in out, out


def test_entity_words_kept_whole():
    """通用部门名整段保留，不得被通用词通道拆成分裂残态。"""
    for w in ("董事会办公室", "纪检监察组", "个人金融部", "风险管理部"):
        out = apply_redactions(w)
        assert out == w, f"{w} -> {out}"


def test_org_name_in_context():
    out = apply_redactions("该行由运营管理部负责")
    assert "运营管理部" in out, out


def test_no_mixed_org_form():
    """不得出现「XXXX办公室」这类混合残态。"""
    for w in ("董事会办公室", "纪检监察组", "纪检监察室"):
        out = apply_redactions(w)
        assert "XXXX" not in out and "XXX" not in out, f"{w} -> {out}"


# ---------------------------------------------------------------------------
# 4. 幂等性
# ---------------------------------------------------------------------------

def test_idempotent():
    for s in ("初始密码：123123", "工行网点", "董事会办公室", "广州农村商业银行"):
        once = apply_redactions(s)
        assert apply_redactions(once) == once, s
