#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
D15 表格姓名注入链路回归测试（真实缺陷守卫）

背景
----
`common_rules.apply_redactions_counted` 末尾的 D15 段（D15 表格列姓名预扫 →
全局注入）历史上写成：

    result, n = _counting_sub(pat, result, _name_repl, counts)

而 `_counting_sub` 只返回字符串（不是 `subn` 的 `(str, n)` 元组），因此只要
文档里被注入了任何表格姓名，该函数必然抛：

    ValueError: too many values to unpack (expected 2)

影响链条（真实泄漏，而非统计口径问题）：

1. 异常发生在 `apply_redactions_counted` 的**最末**（D15 段），
   → 前面所有已完成的脱敏结果被一并丢弃；
2. 调用方 `redact_ppt._process_xml_file` / `redact_excel._process_xml_file`
   用 `try/except` 兜住异常并**直接跳过文件写回**
   → 该 slide / sheet 整块零脱敏。

所以凡是 Excel/PPT 里存在"角色列姓名表"的文档，D15 之后全文件未脱敏。
本测试锁定三个层面：函数层不抛异常、注入姓名确实被替换、PPT 真实链路
（`_process_xml_file`）在有表格姓名时能正常写回脱敏结果。

运行：
    python3 -m pytest tests/test_d15_extra_names_guard.py -v
"""
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common_rules
from common_rules import apply_redactions_counted, inject_extra_names, clear_extra_names


class TestD15ExtraNames:
    """D15 注入姓名的函数层行为"""

    def setup_method(self):
        clear_extra_names()

    def teardown_method(self):
        clear_extra_names()

    def test_no_unpack_error(self):
        """注入姓名后 apply_redactions_counted 不得抛解包异常"""
        inject_extra_names({"测试姓名"})
        text, counts = apply_redactions_counted("负责人：测试姓名，负责整体推进。")
        assert "测试姓名" not in text, "注入的姓名未被替换"
        assert "XXX" in text
        assert counts.get("姓名", 0) >= 1

    def test_no_error_without_extra_names(self):
        """未注入姓名时行为不变（回归护栏）"""
        text, counts = apply_redactions_counted("联系人：测试姓名")
        assert "测试姓名" not in text

    def test_extra_name_counted_as_name(self):
        """注入姓名命中应计入「姓名」分类计数"""
        inject_extra_names({"张三丰"})
        _, counts = apply_redactions_counted("张三丰负责系统建设")
        assert counts.get("姓名", 0) >= 1

    def test_word_boundary_protection(self):
        """词边界保护：注入名作为更长串的子串时不应被替换

        用英文名做探针，避免中文长姓名先被"主姓名规则"脱敏而掩盖边界行为。
        """
        inject_extra_names({"Zhang"})
        text, _ = apply_redactions_counted("Zhangwei 与 Zhang 是两个人")
        assert "Zhangwei" in text, "Zhangwei 被子串误伤"
        assert "XXX 是" in text, "Zhang 未被替换"


class TestD15PptPipeline:
    """PPT 真实链路：注入姓名后 XML 必须被写回脱敏"""

    def setup_method(self):
        clear_extra_names()

    def teardown_method(self):
        clear_extra_names()

    def _build_pptx_with_name_table(self, path: Path, name: str):
        """构造一个最小 pptx：首页含"负责人"表格（角色列 + 姓名），次页正文含该姓名。"""
        slide1 = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
            ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<p:cSld><p:spTree>'
            '<a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>角色</a:t></a:r></a:p></a:txBody></a:tc>'
            f'<a:tc><a:txBody><a:p><a:r><a:t>负责人</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
            '<a:tr><a:tc><a:txBody><a:p><a:r><a:t>姓名</a:t></a:r></a:p></a:txBody></a:tc>'
            f'<a:tc><a:txBody><a:p><a:r><a:t>{name}</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
            '</a:tbl>'
            '</p:spTree></p:cSld></p:sld>'
        )
        slide2 = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
            ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<p:cSld><p:spTree>'
            f'<a:p><a:r><a:t>该方案由{name}负责</a:t></a:r></a:p>'
            '</p:spTree></p:cSld></p:sld>'
        )
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
            '</Types>'
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types)
            zf.writestr("ppt/slides/slide1.xml", slide1)
            zf.writestr("ppt/slides/slide2.xml", slide2)

    def test_pptx_pipeline_redacts_and_writes_back(self):
        """有姓名表时，PPT 处理不得静默跳过（异常被吞 → 整页零脱敏）"""
        from redact_ppt import redact_pptx

        tmpdir = Path(tempfile.mkdtemp(prefix="d15_guard_"))
        try:
            src = tmpdir / "in.pptx"
            dst = tmpdir / "out.pptx"
            name = "张三丰"
            self._build_pptx_with_name_table(src, name)

            redact_pptx(str(src), str(dst))

            assert dst.exists(), "输出文件未生成"
            with zipfile.ZipFile(dst) as zf:
                slides = [zf.read(n).decode("utf-8")
                          for n in zf.namelist()
                          if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
            assert slides, "输出中无幻灯片"
            joined = "".join(slides)
            assert name not in joined, "姓名仍残留在脱敏结果中（D15 注入链路断裂）"
            assert "XXX" in joined
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
