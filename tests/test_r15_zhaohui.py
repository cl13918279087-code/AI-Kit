#!/usr/bin/env python3
"""
test_r15_zhaohui.py - R15 回归测试（贡献者：赵辉）

覆盖 doc-redact 规则中心 scripts/common_rules.py 的以下能力域：

  1. 基础敏感信息：邮箱 / 身份证 / 手机 / 固话（带区号）
  2. 无区号本地号码（R-⑮）：上下文关键词限定 + 误伤排除项
  3. 银行名：配置精确名 / 字母形态 / 业态泛称保留
  4. 银行代码缩写（R-㉔）与项目代号（R-㉖）：config 驱动
  5. 日期口径 date_mode：full / year-only（R-㉛）
  6. 姓名：角色词槽位 / 顿号名单锚点扩散 / 英文人名槽位
  7. 地址回溯、计数 API、文件名脱敏

运行方式：
    cd <repo>
    python tests/test_r15_zhaohui.py
    # 或 pytest tests/test_r15_zhaohui.py -v

说明：
  - 断言基于仓库默认 config.json；配置驱动用例通过临时注入
    common_rules._CONFIG_CACHE 实现，用例结束后立即还原，互不污染。
  - 若失败，请先确认 config.json 未被改动（尤其是 replacement 常量）。
"""

import sys
import os
import json

# 将 scripts 目录加入 import path（与 tests/test_r4_name_scoring.py 一致）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import common_rules as cr

# 基线配置快照：供配置驱动用例注入后还原
_BASE_CONFIG = json.loads(json.dumps(cr.get_config(), ensure_ascii=False))
cr.reset_patterns()


def _use_config(extra: dict):
    """临时注入配置并重建规则缓存。"""
    cfg = json.loads(json.dumps(_BASE_CONFIG, ensure_ascii=False))
    cfg.update(extra)
    cr._CONFIG_CACHE = cfg
    cr.reset_patterns()


def _restore_config():
    """还原基线配置并重建规则缓存。"""
    cr._CONFIG_CACHE = _BASE_CONFIG
    cr.reset_patterns()


# ---------------------------------------------------------------------------
# 1. 基础敏感信息
# ---------------------------------------------------------------------------

def test_email():
    """电子邮箱 → XXXXX@XXXXX"""
    src = "联系邮箱：zhangsan@example.com"
    out = cr.apply_redactions(src)
    assert "zhangsan@example.com" not in out, f"邮箱未脱敏：{out!r}"
    assert "XXXXX@XXXXX" in out, f"邮箱占位符不对：{out!r}"
    print(f"✅ email: {src!r} -> {out!r}")


def test_id_card():
    """18 位身份证号 → 等长 X"""
    src = "身份证110101199003072345先生"
    out = cr.apply_redactions(src)
    assert "110101199003072345" not in out, f"身份证未脱敏：{out!r}"
    assert "XXXXXXXXXXXXXXXXXX" in out, f"身份证占位符不对：{out!r}"
    print(f"✅ id_card: {src!r} -> {out!r}")


def test_mobile():
    """11 位手机号 → XXXXXXXXXXX"""
    src = "联系电话：13812345678"
    out = cr.apply_redactions(src)
    assert "13812345678" not in out, f"手机号未脱敏：{out!r}"
    assert "XXXXXXXXXXX" in out, f"手机号占位符不对：{out!r}"
    print(f"✅ mobile: {src!r} -> {out!r}")


def test_landline_with_area_code():
    """带区号固话 → 0XX-XXXXXXXX"""
    src = "电话：010-88888888"
    out = cr.apply_redactions(src)
    assert "88888888" not in out, f"固话未脱敏：{out!r}"
    assert "0XX-XXXXXXXX" in out, f"固话占位符不对：{out!r}"
    print(f"✅ landline: {src!r} -> {out!r}")


# ---------------------------------------------------------------------------
# 2. 无区号本地号码（R-⑮）
# ---------------------------------------------------------------------------

def test_local_phone_with_context():
    """有电话类关键词上下文时，无区号 7-8 位号码应脱敏为 0XX-XXXXXXXX"""
    for ctx in ("电话", "座机", "分机", "传真", "联系方式"):
        src = f"{ctx}：87517381"
        out = cr.apply_redactions(src)
        assert "87517381" not in out, f"[{ctx}] 本地号码漏脱：{out!r}"
        assert "0XX-XXXXXXXX" in out, f"[{ctx}] 占位符不对：{out!r}"
    print("✅ local_phone(有上下文): 电话/座机/分机/传真/联系方式 全部转正")


def test_local_phone_excluded():
    """工号/编号/金额/年度等上下文，或完全无上下文关键词时，不得误伤"""
    for ctx in ("工号", "编号", "金额", "年度"):
        src = f"{ctx}：87517381"
        out = cr.apply_redactions(src)
        assert out == src, f"[{ctx}] 不应脱敏，却变为：{out!r}"
    plain = "本次共录入87517381条"
    assert cr.apply_redactions(plain) == plain, "无上下文关键词不应脱敏"
    print("✅ local_phone(排除项): 工号/编号/金额/年度/无上下文 均保持原文")


# ---------------------------------------------------------------------------
# 3. 银行名
# ---------------------------------------------------------------------------

def test_bank_names_configured():
    """config.bank_names 中的精确银行名 → XX银行"""
    for bank in ("郑州银行", "海峡银行", "中原银行"):
        src = f"{bank}新一代项目"
        out = cr.apply_redactions(src)
        assert bank not in out, f"{bank} 未脱敏：{out!r}"
        assert "XX银行" in out, f"{bank} 占位符不对：{out!r}"
    print("✅ bank_names: 郑州/海峡/中原银行 全部转正")


def test_bank_generic_and_biz_keep():
    """泛称/业态词（手机银行、村镇银行、各农商银行、数字银行）不得误伤"""
    for keep in ("手机银行", "村镇银行", "各农商银行", "数字银行"):
        src = f"{keep}相关业务"
        out = cr.apply_redactions(src)
        assert out == src, f"泛称/业态词『{keep}』被误伤：{src!r} -> {out!r}"
    print("✅ bank_generic_keep: 手机银行/村镇银行/各农商银行/数字银行 均保持原文")


def test_letter_form_bank():
    """字母+汉字形态（LZ银行）→ XX银行"""
    src = "LZ银行新一代项目"
    out = cr.apply_redactions(src)
    assert "LZ银行" not in out, f"字母形态银行名未脱敏：{out!r}"
    assert "XX银行" in out, f"占位符不对：{out!r}"
    print(f"✅ letter_bank: {src!r} -> {out!r}")


# ---------------------------------------------------------------------------
# 4. 银行代码缩写与项目代号（config 驱动）
# ---------------------------------------------------------------------------

def test_bank_codes_config():
    """config.bank_codes（QRCB/JRCB 等）应按字母边界整体替换为 XX银行"""
    try:
        _use_config({"bank_codes": ["QRCB", "JRCB"]})
        for code in ("QRCB", "JRCB"):
            src = f"{code} 项目群"
            out = cr.apply_redactions(src)
            assert code not in out, f"银行代码 {code} 未脱敏：{out!r}"
            assert "XX银行" in out, f"{code} 占位符不对：{out!r}"
        print("✅ bank_codes: QRCB/JRCB 转正")
    finally:
        _restore_config()


def test_project_codes_config():
    """config.project_codes（秦领/农芯 等）应替换为 XX（后接 工程/项目）"""
    try:
        _use_config({"project_codes": ["秦领", "农芯"]})
        for code in ("秦领", "农芯"):
            src = f"{code}工程UAT测试"
            out = cr.apply_redactions(src)
            assert code not in out, f"项目代号 {code} 未脱敏：{out!r}"
            assert "XX工程" in out, f"{code} 占位符不对：{out!r}"
        print("✅ project_codes: 秦领/农芯 转正")
    finally:
        _restore_config()


def test_project_code_numeric():
    """数字型项目代号：数字紧邻『工程/项目』时 → XX工程"""
    src = "811工程UAT方案"
    out = cr.apply_redactions(src)
    assert "811" not in out, f"数字型项目代号未脱敏：{out!r}"
    assert "XX工程" in out, f"占位符不对：{out!r}"
    print(f"✅ project_code_numeric: {src!r} -> {out!r}")


# ---------------------------------------------------------------------------
# 5. 日期口径
# ---------------------------------------------------------------------------

def test_date_full():
    """默认 date_mode=full：完整日期 → YYYY年MM月DD日 / YYYY/MM/DD"""
    out = cr.apply_redactions("2026年9月1日召开")
    assert "2026年9月1日" not in out and "YYYY年MM月DD日" in out, f"中文日期异常：{out!r}"
    out2 = cr.apply_redactions("2026/08/15完成")
    assert "2026/08/15" not in out2 and "YYYY/MM/DD" in out2, f"斜杠日期异常：{out2!r}"
    print("✅ date_full: 完整日期全部转正")


def test_date_short():
    """无年份短日期（X月X日）→ MM月DD日"""
    src = "计划于9月30日完成"
    out = cr.apply_redactions(src)
    assert "9月30日" not in out, f"短日期未脱敏：{out!r}"
    assert "MM月DD日" in out, f"短日期占位符不对：{out!r}"
    print(f"✅ date_short: {src!r} -> {out!r}")


def test_date_mode_year_only():
    """date_mode=year-only：仅脱年份，月日保留"""
    try:
        _use_config({"date_mode": "year-only"})
        out = cr.apply_redactions("2026年9月1日召开")
        assert "2026" not in out and "YYYY年9月1日" in out, f"year-only(中文) 异常：{out!r}"
        out2 = cr.apply_redactions("2026/08/15完成")
        assert "2026" not in out2 and "YYYY/08/15" in out2, f"year-only(斜杠) 异常：{out2!r}"
        print("✅ date_mode=year-only: 仅年份转正，月日保留")
    finally:
        _restore_config()


# ---------------------------------------------------------------------------
# 6. 姓名
# ---------------------------------------------------------------------------

def test_name_role_slot():
    """角色词槽位：『组长：张三』→ 姓名被遮盖"""
    src = "组长：张三负责"
    out = cr.apply_redactions(src)
    assert "张三" not in out, f"角色槽位姓名未脱敏：{out!r}"
    assert "XXX" in out, f"占位符不对：{out!r}"
    print(f"✅ name_role_slot: {src!r} -> {out!r}")


def test_name_anchor_list():
    """顿号/逗号名单锚点扩散：『核心成员：王健、李明』→ 两个姓名都遮盖"""
    src = "核心成员：王健、李明"
    out = cr.apply_redactions(src)
    assert "王健" not in out, f"名单锚点漏脱（王健）：{out!r}"
    assert "李明" not in out, f"名单锚点漏脱（李明）：{out!r}"
    assert out.count("XXX") >= 2, f"应至少遮盖 2 个姓名：{out!r}"
    print(f"✅ name_anchor_list: {src!r} -> {out!r}")


def test_english_name_slot():
    """英文人名槽位：『联系人：Smith』→ 遮盖"""
    src = "联系人：Smith"
    out = cr.apply_redactions(src)
    assert "Smith" not in out, f"英文人名未脱敏：{out!r}"
    assert "XXX" in out, f"占位符不对：{out!r}"
    print(f"✅ english_name_slot: {src!r} -> {out!r}")


# ---------------------------------------------------------------------------
# 7. 地址 / 计数 / 文件名
# ---------------------------------------------------------------------------

def test_address_tail():
    """地址回溯：『花园路39号』→ XX路XX号（保留后缀）"""
    src = "位于花园路39号"
    out = cr.apply_redactions(src)
    assert "花园路" not in out, f"地址未脱敏：{out!r}"
    assert "XX路XX号" in out, f"地址占位符不对：{out!r}"
    print(f"✅ address_tail: {src!r} -> {out!r}")


def test_counted_api():
    """apply_redactions_counted 返回分类计数"""
    src = "联系人张三，电话010-88888888，邮箱a@b.com"
    out, counts = cr.apply_redactions_counted(src)
    assert "张三" not in out and "88888888" not in out, f"文本未脱敏：{out!r}"
    assert counts.get("邮箱", 0) >= 1, f"邮箱计数缺失：{counts}"
    assert counts.get("固话", 0) >= 1, f"固话计数缺失：{counts}"
    assert counts.get("姓名", 0) >= 1, f"姓名计数缺失：{counts}"
    print(f"✅ counted_api: {out!r} | counts={counts}")


def test_filename_stem():
    """文件名脱敏：银行名 → XX银行，8 位日期戳 → YYYYMMDD"""
    s1 = cr.redact_filename_stem("郑州银行新核心项目_启动会材料V0.6-20260901")
    assert "郑州银行" not in s1 and "XX银行" in s1, f"文件名银行名未脱敏：{s1!r}"
    assert "YYYYMMDD" in s1, f"文件名日期戳未脱敏：{s1!r}"
    s2 = cr.redact_filename_stem("海峡银行周例会（20260901）V1.0.0")
    assert "海峡银行" not in s2 and "XX银行" in s2, f"文件名银行名未脱敏：{s2!r}"
    assert "YYYYMMDD" in s2, f"文件名日期戳未脱敏：{s2!r}"
    print(f"✅ filename_stem: {s1!r} / {s2!r}")


if __name__ == "__main__":
    print("=" * 60)
    print("R15 回归测试（贡献者：赵辉） — common_rules 规则中心")
    print("=" * 60)

    test_email()
    test_id_card()
    test_mobile()
    test_landline_with_area_code()
    test_local_phone_with_context()
    test_local_phone_excluded()
    test_bank_names_configured()
    test_bank_generic_and_biz_keep()
    test_letter_form_bank()
    test_bank_codes_config()
    test_project_codes_config()
    test_project_code_numeric()
    test_date_full()
    test_date_short()
    test_date_mode_year_only()
    test_name_role_slot()
    test_name_anchor_list()
    test_english_name_slot()
    test_address_tail()
    test_counted_api()
    test_filename_stem()

    print("=" * 60)
    print("全部测试通过 ✅")
    print("=" * 60)
