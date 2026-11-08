#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0 回归测试：整文件级兜底通道不得破坏 XML 结构。

【缺陷背景】
PPT / Word / Excel 处理器的「整文件级兜底」通道对 XML 原文整体跑正则，
XML 属性名也会被替换：

    <p:transition spd="slow" p14:dur="2000"/>
                 ↑ 属性名 "spd" 命中拉丁银行缩写 SPD（上海浦东发展银行）
                 → 被替换为 "[Bank]"，属性名变为 "[Bank]"="slow"

产物 PPTX 的 12 个 slide XML 结构非法，python-pptx 与 Office 均无法打开——
交付级致命缺陷（实测于「平安信用卡A+项目-投产整体方案」）。

【修复口径】
common_rules.apply_redactions_xml_aware：
  - 标签名与属性名：哨兵保护，不参与替换
  - 文本节点与属性值：正常脱敏
  - **不拆节点**——检测通道（N-C 名单区域检测等）依赖文档级上下文，
    逐节点处理会造成大规模漏脱（实测姓名召回 444 → 63）
"""

import shutil
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from lxml import etree  # noqa: E402

from common_rules import apply_redactions_counted, apply_redactions_xml_aware  # noqa: E402


@pytest.fixture()
def tmpd():
    """临时工作目录（与项目其他测试文件同名 fixture 保持一致）。"""
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="wb_xml_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 单元：属性名保护
# ---------------------------------------------------------------------------

def test_xml_aware_preserves_attribute_names():
    """XML 属性名不得被脱敏规则替换。"""
    xml = '<p:transition xmlns:p="urn:p" xmlns:p14="urn:p14" spd="slow" p14:dur="2000"/>'
    out, _ = apply_redactions_xml_aware(xml)
    assert 'spd="slow"' in out, f"属性名 spd 被破坏：{out}"
    assert "[Bank]" not in out
    etree.fromstring(out.encode("utf-8"))  # 结构必须合法


def test_xml_aware_preserves_tag_names():
    """XML 标签名不得被脱敏规则替换。"""
    xml = '<abc:def xmlns:abc="urn:abc">文本</abc:def>'
    out, _ = apply_redactions_xml_aware(xml)
    assert "<abc:def" in out and "</abc:def>" in out
    assert "WBX" not in out, f"哨兵未还原：{out}"
    etree.fromstring(out.encode("utf-8"))


def test_xml_aware_redacts_attribute_values():
    """属性值中的敏感信息仍须脱敏（修 P0 不得引入漏脱）。"""
    xml = '<p:cNvPr name="张三" descr="郑州银行"/>'
    out, counts = apply_redactions_xml_aware(xml)
    assert "张三" not in out, f"属性值未脱敏：{out}"
    assert "郑州银行" not in out
    assert 'name="' in out and "descr=" in out, f"属性结构被破坏：{out}"


def test_xml_aware_redacts_text_nodes():
    """文本节点中的敏感信息须脱敏。"""
    xml = "<a:t>郑州银行张三</a:t>"
    out, _ = apply_redactions_xml_aware(xml)
    assert "郑州银行" not in out and "张三" not in out


def test_xml_aware_preserves_attribute_quoting_style():
    """属性引号风格（单/双引号）与空白须原样保留。"""
    xml = "<a x='1'   y=\"2\" />"
    out, _ = apply_redactions_xml_aware(xml)
    assert "x='1'" in out and 'y="2"' in out


def test_xml_aware_keeps_plain_text_path():
    """非 XML 输入走原路径，行为不变。"""
    text = "郑州银行张三"
    out, counts = apply_redactions_counted(text)
    out2, counts2 = apply_redactions_xml_aware(text)
    assert out2 == out
    assert counts2 == counts


def test_xml_aware_no_recall_loss_vs_whole_file():
    """
    关键回归护栏：XML 感知处理**不得**丢失召回能力。
    逐节点处理会让姓名召回从 444 掉到 63（N-C 区域检测失去文档级上下文）。
    """
    xml = (
        '<a:t>张三</a:t><a:t>李四</a:t>'
        '<p:cNvPr name="王五" descr="郑州银行"/>'
    )
    _, old_counts = apply_redactions_counted(xml)
    _, new_counts = apply_redactions_xml_aware(xml)
    assert new_counts.get("姓名", 0) >= old_counts.get("姓名", 0), (
        f"召回下降：旧 {old_counts} → 新 {new_counts}"
    )


# ---------------------------------------------------------------------------
# 集成：真实 PPTX 产物必须可被 python-pptx 打开
# ---------------------------------------------------------------------------

def _all_xml_parts(pptx_path: Path):
    with zipfile.ZipFile(pptx_path) as z:
        for name in z.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                yield name, z.read(name)


def test_generated_pptx_is_openable(tmpd):
    """
    端到端：脱敏产物 PPTX 的全部 XML 部件结构合法，且可被 python-pptx 打开。
    """
    from pptx import Presentation
    from redact_ppt import redact_ppt

    src = tmpd / "sample.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "郑州银行启动会材料"
    box = slide.shapes.add_textbox(0, 0, 10000000, 10000000)
    box.text_frame.text = "项目负责人：张三"
    prs.save(str(src))

    out = tmpd / "out.pptx"
    redact_ppt(str(src), str(out))

    assert out.exists(), "脱敏产物未生成"
    # 全部 XML 部件结构合法
    for name, blob in _all_xml_parts(out):
        try:
            etree.fromstring(blob)
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"产物 XML 部件损坏：{name} — {exc}")
    # 可被 python-pptx 重新打开
    reloaded = Presentation(str(out))
    assert len(reloaded.slides) >= 1
    # 文本已脱敏
    texts = "\n".join(
        shape.text_frame.text
        for s in reloaded.slides
        for shape in s.shapes
        if shape.has_text_frame
    )
    assert "郑州银行" not in texts
    assert "张三" not in texts


def test_generated_docx_is_openable(tmpd):
    """端到端：脱敏产物 DOCX 可被 python-docx 打开。"""
    import docx
    from redact_word import redact_word

    src = tmpd / "sample.docx"
    d = docx.Document()
    d.add_paragraph("郑州银行新核心项目")
    d.add_paragraph("联系人：张三")
    d.save(str(src))

    out = tmpd / "out.docx"
    redact_word(str(src), str(out))

    assert out.exists()
    reloaded = docx.Document(str(out))
    text = "\n".join(p.text for p in reloaded.paragraphs)
    assert "郑州银行" not in text
    assert "张三" not in text


def test_generated_xlsx_is_openable(tmpd):
    """端到端：脱敏产物 XLSX 可被 openpyxl 打开。"""
    import openpyxl
    from redact_excel import redact_excel

    src = tmpd / "sample.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "姓名"
    ws["B1"] = "张三"
    wb.save(str(src))

    out = tmpd / "out.xlsx"
    redact_excel(str(src), str(out))

    assert out.exists()
    reloaded = openpyxl.load_workbook(str(out))
    vals = [c.value for row in reloaded.active.iter_rows() for c in row]
    assert "张三" not in vals
