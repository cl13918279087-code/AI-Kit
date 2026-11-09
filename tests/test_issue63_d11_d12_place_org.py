# -*- coding: utf-8 -*-
"""
Issue #63 批次四补充用例：D-11（地名 / 网点名 / 职场名）与 D-12（企业·机构全称）
以及 Issue #64 D-8 派生的「机构简称脱敏」。

口径说明（与人工标准版对齐）：
  - 地名 / 网点名前缀 → LOCATION 占位符 ``XX``，可回溯后缀（职场 / 支行 / 网点…）保留；
  - 行业机构通名后接（连江自来水 / 漳平供电）→ 整体保留，不当作地名脱敏；
  - 地名整词保护（长江大桥 / 翻山越岭）→ 不被银行品牌规则或姓名规则误伤；
  - 企业 / 机构全称 → 整段脱敏为 ORG 占位符 ``XXXX``（以行政区划为锚点，避免误伤
    「某金融科技公司」「母公司的CFO」这类通用词组）；
  - 机构简称 + 指定通名（平安科技）→ 只脱简称，保留通名；
  - 独立简称（计财）→ 整词脱敏，但后接 部/处/科/室/所 时保留（营运计财部）。
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


def _cfg():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. 配置落地
# ---------------------------------------------------------------------------

def test_d11_place_rules_present():
    pr = _cfg().get("place_rules") or {}
    assert pr.get("place_names"), "缺少 place_names"
    assert pr.get("site_names"), "缺少 site_names"
    assert pr.get("net_suffixes"), "缺少 net_suffixes"
    assert pr.get("keep_after_words"), "缺少 keep_after_words"
    assert pr.get("place_guards"), "缺少 place_guards"


def test_d12_org_rules_present():
    r = _cfg().get("org_name_rules") or {}
    assert r.get("admin_divisions"), "缺少 admin_divisions"
    assert r.get("full_org_suffixes"), "缺少 full_org_suffixes"
    assert r.get("abbreviation_derivations"), "缺少 abbreviation_derivations"
    assert r.get("standalone_abbreviations"), "缺少 standalone_abbreviations"


# ---------------------------------------------------------------------------
# 2. D-11 地名 / 网点名
# ---------------------------------------------------------------------------

def test_place_names_masked():
    for name in ("八卦岭", "连江", "漳平"):
        out = apply_redactions(name)
        assert "XX" in out and name not in out, "%s 未脱敏: %r" % (name, out)


def test_net_suffix_preserved():
    out = apply_redactions("地点：八卦岭职场501会议室。")
    assert "八卦岭" not in out
    assert "职场501会议室" in out
    out2 = apply_redactions("旭辉职场A栋六楼")
    assert "旭辉" not in out2 and "职场A栋" in out2


def test_site_names_masked():
    out = apply_redactions("张江、深圳各场地")
    assert "张江" not in out and "旭辉" not in out


def test_keep_after_words_not_masked():
    # 地名 + 行业机构通名 → 视为机构名前缀，整体保留
    for text in ("连江自来水", "漳平供电局"):
        out = apply_redactions(text)
        assert out == text, "%r 被误脱敏 -> %r" % (text, out)


def test_place_guards_kept():
    for text in ("长江大桥", "黄河大桥", "珠江大桥", "翻山越岭"):
        out = apply_redactions(text)
        assert out == text, "%r 被误伤 -> %r" % (text, out)


def test_bank_chain_keeps_place_name():
    # Issue #53 §3.4 通道互斥：银行判定链内的地名归银行链所有
    out = apply_redactions("中国银行深圳分行")
    assert out == "XX银行深圳分行", out


def test_place_word_in_name_form_defers_to_name_channel():
    # 「姓 + 词典词」构成姓名时，本通道必须让位给姓名通道（否则真名丢失）
    out = apply_redactions("负责人：龚旭辉")
    assert out == "负责人：XXX", out


def test_placeholder_length_matches_standard():
    # 「深圳XX」（银行品牌规则产物）+ 本通道脱敏 → 占位符长度须为 XX
    out = apply_redactions("建立旭辉投产主战场及张江、深圳各场地之间的视频连接")
    assert "XXXX" not in out, out
    assert "XX、XX各场地" in out, out


def test_d11_idempotent():
    text = "地点：八卦岭职场501会议室。"
    once = apply_redactions(text)
    twice = apply_redactions(once)
    assert once == twice


# ---------------------------------------------------------------------------
# 3. D-12 企业 / 机构全称
# ---------------------------------------------------------------------------

def test_full_org_name_masked_whole():
    out = apply_redactions("漳平市泽源水电开发有限公司")
    assert out == "XXXX", out
    out2 = apply_redactions("漳州实验中学")
    assert out2 == "XXXX", out2


def test_generic_company_words_not_masked():
    # 以行政区划为锚点，避免误伤通用词组
    for text in ("某金融科技公司2026-2028战略规划",
                 "顶级咨询公司实践",
                 "母公司的CFO"):
        out = apply_redactions(text)
        assert out == text, "%r 被误脱敏 -> %r" % (text, out)


def test_d12_idempotent():
    text = "漳平市泽源水电开发有限公司"
    once = apply_redactions(text)
    assert apply_redactions(once) == once


# ---------------------------------------------------------------------------
# 4. D-8 派生：机构简称
# ---------------------------------------------------------------------------

def test_abbreviation_derivation_keeps_common_name():
    out = apply_redactions("平安科技南京共享开发中心")
    assert "平安" not in out
    assert "科技" in out


def test_standalone_abbreviation_masked():
    out = apply_redactions("战略计财组")
    assert "计财" not in out and "组" in out


def test_standalone_abbreviation_keeps_generic_dept():
    out = apply_redactions("营运计财部")
    assert out == "营运计财部", out


# ---------------------------------------------------------------------------
# 5. D-9 边界回归（本批次修复）
# ---------------------------------------------------------------------------

def test_digit_password_masked_with_chinese_adjacent():
    out = apply_redactions("默认初始密码为888888")
    assert "888888" not in out, out


def test_digit_business_code_kept():
    for text in ("20111012", "工号 123456"):
        out = apply_redactions(text)
        assert out == text, "%r 被误脱敏 -> %r" % (text, out)


# ---------------------------------------------------------------------------
# 6. 机构链口径 + 「地名性前缀 vs 专名网点」语境判别
#    （Issue #63 批次四 D-11 补强，对齐人工标准版）
# ---------------------------------------------------------------------------
#
# 【标准版口径取证（本批次实测 7 组语料）】
#   机构链 = [地名性前缀] + [机构类型后缀链]，脱敏只作用于前缀：
#     自贸区分行营业部 → XX分行营业部   温州分行营业部 → XX分行营业部
#     鼓楼支行营业部   → XX支行营业部   总行营业部     → 总行营业部（原文）
#   专名网点按**语境**判别（同一名称两语境处理相反，故非词表驱动）：
#     机构清单/枚举语境 → 遮蔽
#       「叫号机：由总行营业部、三山支行、福新支行、洪山支行、永兴支行进行演练」
#       → 总行营业部、XX支行、XX支行、XX支行、XX支行
#       「应急取款——鼓楼支行营业部、福清分行营业部、…」
#       → XX支行营业部、XX分行营业部、…
#     散文叙述语境 → 保留原文
#       「（3）由三山支行负责配合发起定期借记往账演练操作」→ 原样
#       「本轮补录由洪山支行和晋江支行配合执行」→ 原样
#       「由台江支行营业部配合发起开立银行承兑汇票的演练」→ 原样
#       「模拟操作指定福新支行、科技支行、温泉支行执行」→ 原样
# ---------------------------------------------------------------------------


def test_org_named_rules_present():
    pr = _cfg().get("place_rules") or {}
    assert pr.get("named_org_keep"), "缺少 named_org_keep（专名网点保留词）"
    assert pr.get("geo_org_prefixes"), "缺少 geo_org_prefixes（地名性前缀词）"
    assert pr.get("org_list_markers"), "缺少 org_list_markers（清单标记域）"


def test_org_chain_suffix_preserved():
    """机构类型后缀链保留：脱敏只作用于地名性前缀。"""
    assert apply_redactions("自贸区分行营业部") == "XX分行营业部"
    assert apply_redactions("温州分行营业部") == "XX分行营业部"
    assert apply_redactions("福清分行营业部") == "XX分行营业部"
    # 带地名性前缀的专名网点同样只遮蔽前缀，类型后缀链保留
    assert apply_redactions("福州鼓楼支行营业部") == "XX支行营业部"


def test_org_chain_generic_prefix_kept():
    """通用机构前缀（总行）不当作地名遮蔽，整体保留。"""
    assert apply_redactions("总行营业部") == "总行营业部"
    assert apply_redactions("由总行营业部、XX分行") == "由总行营业部、XX分行"


def test_named_branch_kept_in_narrative():
    """专名网点在叙述语境保留原文。"""
    for text in (
        "（3）由三山支行负责配合发起定期借记往账演练操作",
        "本轮补录由洪山支行和晋江支行配合执行",
        "由台江支行营业部配合发起演练操作",
        "模拟操作指定福新支行、科技支行、温泉支行执行",
    ):
        out = apply_redactions(text)
        assert out == text, "%r 被误脱敏 -> %r" % (text, out)


def test_named_branch_masked_in_list_context():
    """专名网点在机构清单/枚举语境仍遮蔽。"""
    out = apply_redactions(
        "叫号机：由总行营业部、三山支行、福新支行、洪山支行、永兴支行进行演练。")
    assert out == ("叫号机：由总行营业部、XX支行、XX支行、XX支行、XX支行进行演练。"), out
    out = apply_redactions(
        "（1）应急取款——鼓楼支行营业部、福清分行营业部、莆田分行营业部")
    assert out == ("（1）应急取款——XX支行营业部、XX分行营业部、XX分行营业部"), out


def test_named_branch_with_geo_prefix_still_masked():
    """专名网点带地名性前缀时仍遮蔽（福州三山支行 → XX支行）。"""
    for text, expect in (
        ("福州三山支行", "XX支行"),
        ("福州台江支行", "XX支行"),
        ("泉州科技支行", "XX支行"),
    ):
        out = apply_redactions(text)
        assert out == expect, "%r -> %r（应为 %r）" % (text, out, expect)


def test_named_branch_in_list_marker_domain_masked():
    """清单标记域（参演机构/演练网点）内的机构名一并遮蔽。"""
    out = apply_redactions(
        "序号参演机构分行（直属行）对口支持人员2台江支行、宁德分行张梅3鼓楼支行")
    assert "台江支行" not in out and "鼓楼支行" not in out, out
    assert "XX支行" in out, out


def test_named_branch_keep_idempotent():
    """专名网点保留通道幂等（二次脱敏不产生新改动）。"""
    text = "（3）由三山支行负责配合发起定期借记往账演练操作"
    once = apply_redactions(text)
    assert apply_redactions(once) == once


def test_workplace_suffix_preserved():
    """职场类可回溯后缀：前缀脱敏、后缀保留（职场通道口径）。"""
    assert apply_redactions("八卦岭职场501会议室") == "XX职场501会议室"
    assert apply_redactions("旭辉职场A栋六楼") == "XX职场A栋六楼"
