#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纯 Python OLE2 (.doc) 解析兜底通道测试。

背景
----
LibreOffice 在内网离线环境 / 受限沙箱中常因 profile 不可写抛
DeploymentException；textutil / antiword 依赖外部二进制。
`scripts/doc_ole_text.py` 提供无外部依赖的纯 Python 解析，
作为 `.doc` 转换链的最后一道防线（ole-python 级）。

覆盖
----
1. OLE2 判定（真 .doc vs 伪 .doc）
2. FIB 片段表解析：正文文本与段落数正确
3. 非 OLE2 输入安全返回空串（不抛异常）
4. redact_word 转换链中 ole-python 级可被调用并产出可打开的 .docx
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from doc_ole_text import (  # noqa: E402
    extract_doc_paragraphs,
    extract_doc_text,
    is_ole2,
)


def _make_fake_ole2(tmp_path: Path) -> Path:
    """构造一个最小 OLE2 容器（仅用于 is_ole2 判定，不含真实内容）。"""
    p = tmp_path / "fake.doc"
    # OLE2 复合文档魔数：D0 CF 11 E0 A1 B1 1A E1
    p.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 512)
    return p


def _make_fake_docx(tmp_path: Path) -> Path:
    """构造一个扩展名为 .doc 但内容是 zip（OOXML）的文件。"""
    p = tmp_path / "fake.doc"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document/>")
    return p


class TestOleDetection:
    def test_ole2_magic_detected(self, tmp_path):
        assert is_ole2(_make_fake_ole2(tmp_path)) is True

    def test_docx_with_doc_extension_is_not_ole2(self, tmp_path):
        assert is_ole2(_make_fake_docx(tmp_path)) is False

    def test_non_ole2_returns_empty_text(self, tmp_path):
        p = _make_fake_docx(tmp_path)
        assert extract_doc_text(p) == ""
        assert extract_doc_paragraphs(p) == []

    def test_missing_file_returns_empty_text(self, tmp_path):
        assert extract_doc_text(tmp_path / "nope.doc") == ""


class TestRealDocParsing:
    """用工作目录中的真实 .doc 语料验证解析能力（语料缺失时跳过）。"""

    CORPUS = ROOT / "工作目录" / "福建海峡银行新核心项目第四轮业务演练切换操作指南V0.2.doc"

    def test_extracts_text_from_real_doc(self):
        if not self.CORPUS.exists():
            pytest.skip("真实 .doc 语料不存在")
        text = extract_doc_text(self.CORPUS)
        assert len(text) > 1000, f"提取字符数异常偏少: {len(text)}"
        assert "演练" in text

    def test_extracts_paragraphs(self):
        if not self.CORPUS.exists():
            pytest.skip("真实 .doc 语料不存在")
        paras = extract_doc_paragraphs(self.CORPUS)
        assert len(paras) > 50, f"段落数异常偏少: {len(paras)}"
        assert all(p.strip() for p in paras)


class TestConverterChain:
    def test_ole_python_converter_is_registered(self):
        import redact_word

        names = [n for n, _ in redact_word._DOC_CONVERTERS]
        assert "ole-python" in names
        # 兜底级必须排在最后
        assert names[-1] == "ole-python"

    def test_ole_python_converter_returns_docx(self, tmp_path):
        if not TestRealDocParsing.CORPUS.exists():
            pytest.skip("真实 .doc 语料不存在")
        import redact_word

        out = redact_word._convert_doc_via_ole_python(
            str(TestRealDocParsing.CORPUS), tmp_path)
        assert out is not None and out.exists()
        # 产出必须是可打开的合法 OOXML
        import docx
        doc = docx.Document(str(out))
        assert len(doc.paragraphs) > 50

    def test_converter_returns_none_for_non_ole2(self, tmp_path):
        import redact_word

        fake = _make_fake_docx(tmp_path)
        assert redact_word._convert_doc_via_ole_python(str(fake), tmp_path) is None
