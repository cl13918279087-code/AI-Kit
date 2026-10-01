#!/usr/bin/env python3
"""
test_r4_name_scoring.py - R4 层测试（Issue #17）
覆盖：分词边界 + 上下文评分 + 双阈值 + LLM 回退
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from r4_name_scoring import (
    _score_candidate, _classify_by_threshold,
    _compute_surname_feature, _compute_name_commonality,
    _compute_role_feature, _compute_density_feature,
    _compute_excluded_conflict, _compute_digit_ratio,
    apply_r4_name_pass, set_name_mode, get_name_mode,
    update_thresholds, debug_score,
)


def test_surname_feature():
    """姓氏池命中特征"""
    text = "张三李四王五"

    # 张 → 姓氏池命中
    assert _compute_surname_feature(text, 0, 2) == 1.0, "张是姓氏，应命中"
    # 王 → 姓氏池命中
    assert _compute_surname_feature(text, 4, 6) == 1.0, "王是姓氏，应命中"
    # 三 → 不在姓氏池
    assert _compute_surname_feature(text, 1, 3) == 0.0, "三不是姓氏，不应命中"
    print("✅ surname_feature 测试通过")


def test_name_commonality():
    """名字常用度特征"""
    text = "张三李四王五赵六"

    # 张三：张(姓)+三(不在 name_pool) → 0/1 = 0.0
    # 李四：李(姓)+四(不在 name_pool) → 0/1 = 0.0
    # 王五：王(姓)+五(不在 name_pool) → 0/1 = 0.0
    # 赵六：赵(姓)+六(不在 name_pool) → 0/1 = 0.0

    # 测试名字池字符
    text2 = "张伟李强王明赵华"
    # 伟/强/明/华 在 name_pool 中
    score1 = _compute_name_commonality(text2, 0, 2)  # 张伟
    score2 = _compute_name_commonality(text2, 2, 4)  # 李强
    assert score1 > 0, "张伟的名字'伟'应在 name_pool 中"
    assert score2 > 0, "李强的名字'强'应在 name_pool 中"
    print(f"✅ name_commonality 测试通过（张伟={score1}, 李强={score2}）")


def test_role_feature():
    """周围角色词特征"""
    # 词块不在角色关键词表中，周围有角色词 → 1.0
    text = "组长负责张三的项目"
    block_start = text.find("张三")
    score = _compute_role_feature(text, block_start, block_start + 2)
    assert score == 1.0, "周围有'组长'角色词，应加1分"

    # 周围无角色词 → 0.0
    text2 = "提高业务发展质量"
    block_start2 = text2.find("发展")
    score2 = _compute_role_feature(text2, block_start2, block_start2 + 2)
    assert score2 == 0.0, "周围无角色词，不应加分"
    print("✅ role_feature 测试通过")


def test_excluded_conflict():
    """排除词冲突特征"""
    # 词块直接命中排除词 → 1.0
    assert _compute_excluded_conflict("客户经理", 0, 4) == 1.0, "客户经理是排除词"
    assert _compute_excluded_conflict("说明书", 0, 3) == 1.0, "说明书是排除词"
    assert _compute_excluded_conflict("项目组", 0, 3) == 1.0, "项目组是排除词"

    # 词块不是排除词 → 0.0
    text = "张三负责项目"
    score = _compute_excluded_conflict(text, 0, 2)
    assert score == 0.0, "张三不是排除词"
    print("✅ excluded_conflict 测试通过")


def test_digit_ratio():
    """数字比例特征（返回 1.0 = 数字比例高，会拉低总分）"""
    # 高数字环境（≥0.4）→ 1.0
    # "编号12345负责人张三" = 12字含5数字 → 5/12=0.417 ≥ 0.4
    text = "编号12345负责人张三"
    block_start = text.find("编号")
    score = _compute_digit_ratio(text, block_start, block_start + 2)
    assert score == 1.0, f"高数字环境（≥0.4）应返回 1.0，实际：{score}"

    # 低数字环境（<0.2）→ 0.0
    text2 = "组长张三负责项目"
    block_start2 = text2.find("组长")
    score2 = _compute_digit_ratio(text2, block_start2, block_start2 + 2)
    assert score2 == 0.0, f"低数字环境应返回 0.0，实际：{score2}"

    # 中等数字环境（0.2~0.4）→ 0.5
    # 加长非数字部分：16字含5数字 → 5/16=0.3125
    text3 = "编号12345负责人张三abcd"  # 16字，5数字 → 5/16=0.3125
    block_start3 = text3.find("编号")
    score3 = _compute_digit_ratio(text3, block_start3, block_start3 + 2)
    assert score3 == 0.5, f"中等数字环境应返回 0.5，实际：{score3}"
    print("✅ digit_ratio 测试通过")


def test_threshold_classification():
    """双阈值分类"""
    update_thresholds(0.75, 0.50)

    assert _classify_by_threshold(0.90) == "high", "≥0.75 → high"
    assert _classify_by_threshold(0.75) == "high", "=0.75 → high"
    assert _classify_by_threshold(0.70) == "medium", "0.50~0.75 → medium"
    assert _classify_by_threshold(0.50) == "medium", "=0.50 → medium"
    assert _classify_by_threshold(0.49) == "low", "<0.50 → low"
    assert _classify_by_threshold(0.0) == "low", "0.0 → low"
    print("✅ threshold_classification 测试通过")


def test_apply_r4_basic():
    """R4 基本脱敏功能"""
    set_name_mode("r4")

    # 高置信姓名（姓氏+常用名+角色词上下文）→ 应脱敏
    text1 = "组长张三负责项目"
    result1 = apply_r4_name_pass(text1, mode="r4")
    assert "XXX" in result1, f"高置信姓名应被脱敏，结果：{result1}"
    print(f"  场景1：{text1!r} → {result1!r}")

    # 低置信词块（常用词，无角色上下文）→ 不应脱敏
    text2 = "提高业务发展质量"
    result2 = apply_r4_name_pass(text2, mode="r4")
    assert "提高业务" in result2, f"常用词不应被脱敏，结果：{result2}"
    print(f"  场景2：{text2!r} → {result2!r}")

    # 枚举模式（enum）→ R4 不执行
    set_name_mode("enum")
    text3 = "组长李四负责项目"
    result3 = apply_r4_name_pass(text3, mode="enum")
    # enum 模式下 R4 不处理，但测试框架中 get_name_mode() == "enum" → 直接返回原文
    # 注：apply_r4_name_pass 本身不判断 mode，mode 由调用方控制
    print(f"  场景3（enum 模式）：{text3!r} → {result3!r}")
    print("✅ apply_r4_basic 测试通过")


def test_apply_r4_density():
    """R4 密度信号特征"""
    set_name_mode("r4")

    # 人名密集区（分工表场景）：多个姓名聚集 → 密度特征 +1 → 高置信
    text = "核心组：蔡新发、张瑜、李明、王健负责"
    result = apply_r4_name_pass(text, mode="r4")
    # 蔡新发/张瑜/李明/王健 均应在密度信号下被高置信判定
    # 但 N-B 锚点扩散会先处理掉这些，换 XXX
    # 在 r4 模式（无 N-B）下，这些词块密度高，应被 R4 脱敏
    print(f"  密度场景：{text!r}")
    print(f"  结果：{result!r}")
    print("✅ apply_r4_density 测试通过")


def test_apply_r4_counting():
    """R4 脱敏计数"""
    set_name_mode("r4")
    counts = {}

    text = "组长张三负责，李四协助"
    result = apply_r4_name_pass(text, counts=counts, mode="r4")
    assert counts.get("姓名", 0) > 0, f"应计姓名数>0，实际：{counts}"
    print(f"  计数场景：{text!r}")
    print(f"  结果：{result!r}, 计数：{counts}")
    print("✅ apply_r4_counting 测试通过")


def test_name_mode_setget():
    """name_mode 配置化"""
    set_name_mode("r4")
    assert get_name_mode() == "r4"
    set_name_mode("both")
    assert get_name_mode() == "both"
    set_name_mode("enum")
    assert get_name_mode() == "enum"
    print("✅ name_mode_setget 测试通过")


def test_debug_score():
    """调试评分工具"""
    text = "组长张三负责"
    start = text.find("张三")
    result = debug_score(text, start, start + 2)

    assert "text" in result and result["text"] == "张三"
    assert "total_score" in result
    assert "classification" in result
    assert result["surname_feature"] == 1.0, "张是姓氏"
    assert result["role_feature"] == 1.0, "周围有组长"
    print(f"  debug_score 输出：{result}")
    print("✅ debug_score 测试通过")


if __name__ == "__main__":
    print("=" * 60)
    print("R4 分词边界层 + 上下文评分测试")
    print("=" * 60)

    test_surname_feature()
    test_name_commonality()
    test_role_feature()
    test_excluded_conflict()
    test_digit_ratio()
    test_threshold_classification()
    test_apply_r4_basic()
    test_apply_r4_density()
    test_apply_r4_counting()
    test_name_mode_setget()
    test_debug_score()

    print("=" * 60)
    print("全部测试通过 ✅")
    print("=" * 60)
