#!/usr/bin/env python3
"""
test_address_recovery_guard.py - 地址回溯「恢复被截断姓名」段落守卫回归（Issue #45 扩池引入的既有回归）

背景：
    Issue #45（精确全名匹配）把 name_pool 从 152 扩到 1283 字，「路/道/号/园/京」
    等**地址用字**随之进入名字池。于是 `common_rules.apply_redactions` 中
    「恢复被地址通道误截断的姓名」后处理段，把地址通道**自己输出的**占位符
    「XX路」「XX号」误判为"被截断的姓名片段"，与前一个字合并成 XXX：

        位于XX路XX号  →  XXXXX号      （期望：位于XX路XX号）

    即：地址占位符被吃掉、路名与门牌号的结构丢失。
    R15 门禁用例 `test_address_tail` 实测失败，二分定位引入点为 `86f0bd5` / Issue #45。

修复：
    在恢复段的合并判据中新增「路型/门牌后缀守卫」——
    `_RECOVERY_ROAD_SUFFIX_CHARS = frozenset("街路巷弄道号")`。
    若 XX 之后紧跟的字属于该集合，说明这个 XX 是地址通道自身的占位符
    （地址通道的终态就是「XX路XX号」），而非被截断的姓名片段，此时绝不合并。

    **行政后缀（镇/村/县/区/市/省）刻意不纳入**——「XX镇」是中间态而非终态，
    必须由本段合并回完整姓名，以保住「黄日镇 → XXX」既有修复场景。

覆盖用例：
    1. 路型后缀不被误并（花园路39号 / 南京路100号）
    2. 行政后缀恢复不被破坏（黄日镇 → XXX）
    3. 机构后缀守卫（R-⑱）不受影响（XX银行 不被合并）
    4. 长地址结构完整性（省/市/区/路/号 多级占位符并存）
    5. 通用语料误伤不退化（地址类语料零新增误伤）

运行方式：
    cd <repo>
    python3 tests/test_address_recovery_guard.py
    # 或 pytest tests/test_address_recovery_guard.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import common_rules as cr  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 路型/门牌后缀不被误并
# ---------------------------------------------------------------------------

def test_road_suffix_not_merged():
    """路型/门牌后缀后的 XX 是地址占位符，绝不能被合并成 XXX"""
    cases = [
        ("位于花园路39号", "位于XX路XX号"),
        ("地址：南京路100号", "地址：XX路XX号"),
    ]
    for src, expect in cases:
        out = cr.apply_redactions(src)
        assert "XXXXX" not in out, f"地址占位符被误并成 XXXXX：{src!r} -> {out!r}"
        assert expect in out, f"地址占位符结构不对：{src!r} -> {out!r}（期望包含 {expect!r}）"
        print(f"✅ road_suffix_not_merged: {src!r} -> {out!r}")


def test_road_suffix_variants():
    """各类路型/门牌型后缀均不受影响"""
    for src in ["中华路5号", "解放大道7号", "文化巷12号", "平安里3号",
                "建国路99号", "学院路2号"]:
        out = cr.apply_redactions(src)
        assert "XXXXX" not in out, f"路型后缀被误并：{src!r} -> {out!r}"
        print(f"✅ road_suffix_variants: {src!r} -> {out!r}")


# ---------------------------------------------------------------------------
# 2. 行政后缀恢复不被破坏（关键反向用例）
# ---------------------------------------------------------------------------

def test_admin_suffix_recovery_preserved():
    """「XX镇」是中间态，必须仍被合并回完整姓名（黄日镇 → XXX）"""
    out = cr.apply_redactions("黄日镇")
    assert out == "XXX", f"行政后缀恢复被破坏：'黄日镇' -> {out!r}（期望 'XXX'）"
    print(f"✅ admin_suffix_recovery_preserved: '黄日镇' -> {out!r}")


def test_admin_suffix_multi():
    """带上下文前缀的行政后缀同样应恢复为姓名"""
    for src in ["联系人黄日镇", "负责人黄日镇"]:
        out = cr.apply_redactions(src)
        assert "XX镇" not in out, f"行政后缀未恢复：{src!r} -> {out!r}"
        assert "XXX" in out, f"行政后缀恢复失败：{src!r} -> {out!r}"
        print(f"✅ admin_suffix_multi: {src!r} -> {out!r}")


# ---------------------------------------------------------------------------
# 3. 机构后缀守卫（R-⑱）不受影响
# ---------------------------------------------------------------------------

def test_org_suffix_guard_unaffected():
    """R-⑱：XX 后的机构后缀属于机构占位符，同样不应被合并"""
    for src in ["XX银行", "XX支行", "XX公司"]:
        out = cr.apply_redactions(src)
        assert "XXX" not in out, f"机构占位符被误并：{src!r} -> {out!r}"
        print(f"✅ org_suffix_guard_unaffected: {src!r} -> {out!r}")


# ---------------------------------------------------------------------------
# 4. 长地址结构完整性
# ---------------------------------------------------------------------------

def test_long_address_structure():
    """多级地址占位符并存时，末级「XX路XX号」结构必须完整"""
    out = cr.apply_redactions("北京市朝阳区建国路88号")
    assert out.endswith("XX路XX号"), f"末级门牌结构丢失：{out!r}"
    assert "XXXXX" not in out, f"出现 XXXXX 异常合并：{out!r}"
    print(f"✅ long_address_structure: '北京市朝阳区建国路88号' -> {out!r}")


# ---------------------------------------------------------------------------
# 5. 通用语料不退化
# ---------------------------------------------------------------------------

def test_generic_corpus_no_regression():
    """地址类通用语料不出现新增误伤"""
    corpus = [
        "位于花园路39号", "地址：南京路100号", "中华路5号", "解放大道7号",
        "文化巷12号", "建国路99号", "学院路2号", "平安里3号",
    ]
    hits = [c for c in corpus if cr.apply_redactions(c) != c]
    # 这些语料本就应该被地址通道脱敏，因此"被改动"是预期行为；
    # 这里守卫的是「不出现 XXXXX / 末级结构丢失」这类结构破坏。
    for c in hits:
        out = cr.apply_redactions(c)
        assert "XXXXX" not in out, f"结构破坏：{c!r} -> {out!r}"
    print(f"✅ generic_corpus_no_regression: {len(hits)}/{len(corpus)} 触发地址脱敏，结构均完整")


def _main():
    print("=" * 60)
    print("地址恢复守卫测试（Issue #45 回归修复）")
    print("=" * 60)
    test_road_suffix_not_merged()
    test_road_suffix_variants()
    test_admin_suffix_recovery_preserved()
    test_admin_suffix_multi()
    test_org_suffix_guard_unaffected()
    test_long_address_structure()
    test_generic_corpus_no_regression()
    print("=" * 60)
    print("全部测试通过 ✅")
    print("=" * 60)


if __name__ == "__main__":
    _main()
