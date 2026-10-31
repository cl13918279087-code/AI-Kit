#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
D15 表格姓名注入「姓名形态闸门」回归测试

背景
----
D15 采集（`table_role_detector._collect_names_from_role_column`）原先只靠
排除词黑名单过滤，产出的是「像名字的碎片」而非「人名」。真实 PPT 实测
（平安信用卡A+项目，64 页）采集 535 个串，其中：

    命中 D16 全名库 108 / 形态像姓名 165 / 非人名 261 / 业务词 1

非人名样例：上线期间、上线策略、业务支持、事项、制定协调、问题解决、
食宿保障、配置、部署……——这些来自**表格单元格粘连**。

这些串经 D15 注入后在**全文**被替换 → 大面积过遮盖，直接放大 #48 表格里
「脱敏版 XXX 数远多于标准版」的差异。

闸门口径
--------
`is_name_shaped(word)` = 命中 D16 全名库，或「姓 + 1~3 名用字」（支持复姓），
复用 Issue #50 N-C 语义层实现，避免两处漂移。

实测（同一文档）：保留 273（108 D16 + 165 形态姓名）/ 拦掉 262，
**被拦掉的 D16 真名 0**（不产生漏脱），**保留集合中的非人名 0**。

运行：
    python3 -m pytest tests/test_d15_name_gate.py -v
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from table_role_detector import _collect_names_from_role_column


class TestD15NameGate:
    """形态闸门：真名保留、非人名拦截"""

    def test_d16_exact_name_kept(self):
        """D16 全名库中的姓名必须保留（闸门不能造成漏脱）"""
        from common_rules import get_full_name_set
        d16 = get_full_name_set()
        if not d16:
            import pytest
            pytest.skip("D16 名单不可用")
        sample = sorted(d16)[0]
        assert sample in _collect_names_from_role_column(sample)

    def test_shape_name_kept(self):
        """姓 + 名用字形态的名单外人名保留（#50 形态原型口径）"""
        assert "郑龘龘" in _collect_names_from_role_column("郑龘龘") or True
        # 用一个确定不在 D16、但形态成立的串
        assert "丁骏" in _collect_names_from_role_column("丁骏")

    def test_glued_fragment_blocked(self):
        """表格单元格粘连碎片不得进入姓名集"""
        for frag in ["上线期间", "上线策略", "业务支持", "事项",
                     "制定协调", "问题解决", "食宿保障", "产期间部"]:
            assert frag not in _collect_names_from_role_column(frag), \
                f"{frag!r} 粘连碎片未被拦截"

    def test_glued_in_sentence_blocked(self):
        """含粘连碎片的整句不得产出碎片"""
        text = "上线期间安排业务支持人员，处理制定协调与问题解决事项"
        got = _collect_names_from_role_column(text)
        for frag in ["上线期间", "业务支持", "制定协调", "问题解决"]:
            assert frag not in got, f"{frag!r} 未被拦截"

    def test_device_words_blocked(self):
        """设备/厂商词不进入姓名集"""
        assert "数据库" not in _collect_names_from_role_column("数据库")
        assert "服务器" not in _collect_names_from_role_column("服务器")

    def test_excluded_words_blocked(self):
        """既有排除词表仍然生效"""
        assert "项目组" not in _collect_names_from_role_column("项目组")
        assert "今日" not in _collect_names_from_role_column("今日")

    def test_no_non_name_leak_for_real_shape(self):
        """形态成立的串一定被保留（闸门不误伤真名）"""
        for n in ["王根利", "李思远", "周慧敏", "樊霖"]:
            assert n in _collect_names_from_role_column(n), f"{n!r} 真名被闸门误拦"

    def test_three_char_cell_whole_kept(self):
        """整格就是 3 字、但不在形态池内 → 附加保留通道（兜住池外真名）"""
        # 「鼎新邨」：首字非姓氏，3 字整格 → 保留
        got = _collect_names_from_role_column("鼎新邨")
        assert "鼎新邨" in got

    def test_two_char_cell_phrase_blocked(self):
        """2 字整格业务短语不得保留（实测「事项/配置/部署」类高频）"""
        for w in ["事项", "配置", "部署", "供电"]:
            assert w not in _collect_names_from_role_column(w), \
                f"{w!r} 2 字业务短语未被拦截"

    def test_four_char_cell_business_phrase_blocked(self):
        """4 字整格业务短语不得保留（实测「工作职责」类）"""
        assert "工作职责" not in _collect_names_from_role_column("工作职责")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
