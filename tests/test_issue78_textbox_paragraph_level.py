"""Issue #78：Word 文本框通道逐 w:t 节点脱敏 → 跨 run 通用词/实体无法命中。

【背景】
Word 正文通道已按段落聚合处理（`w:p` 内所有 `w:t` 拼接后整段脱敏，再差异回写各 run），
但页眉/页脚/文本框等 XML（`word/*.xml`，除 document.xml 外）此前**逐 `w:t` 节点**脱敏：

    for t_elem in root.iter(f"{W}t"):
        redacted = apply_redactions(t_elem.text)      # ← 单个 run 独立脱敏

Word/WPS 常把一句话拆进多个 run（"演"+"练"、"张"+"三丰"），逐节点处理导致：
  ① 跨 run 的通用词无法命中整词保护表 → 通用词被整段吞掉（过度脱敏）；
  ② 跨 run 的实体（姓名/机构）无法识别 → 漏脱。

【解法】
改为段落级聚合（与正文通道一致）：按 `w:p` 聚合其下所有 `w:t` → 整段脱敏 →
用 `redistribute_paragraph` 做差异级精确回写；不在任何段落内的游离 `w:t` 回退逐节点处理。

运行：
    python3 -m pytest tests/test_issue78_textbox_paragraph_level.py -v
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import redact_word  # noqa: E402

W = redact_word.W


def _hdr_xml(paragraphs):
    """构造一个最小页眉 XML：每个段落由若干 run（w:r/w:t）拼成。"""
    body = []
    for runs in paragraphs:
        rs = "".join(
            f"<w:r><w:t xml:space=\"preserve\">{t}</w:t></w:r>" for t in runs)
        body.append(f"<w:p>{rs}</w:p>")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            + "".join(body) + "</w:hdr>")


def _write(tmp_path, xml):
    p = tmp_path / "header1.xml"
    p.write_text(xml, encoding="utf-8")
    return p


def _texts(path):
    from lxml import etree
    tree = etree.parse(str(path))
    return "".join(t.text or "" for t in tree.getroot().iter(f"{W}t"))


# ---------------------------------------------------------------------------
# 1. 跨 run 通用词 / 机构实体：不得被整段吞掉，也不得被切碎
# ---------------------------------------------------------------------------

def test_split_common_word_kept(tmp_path):
    """「演练」被拆成两个 run → 整词保护表命中 → 不得被吞成占位符。"""
    path = _write(tmp_path, _hdr_xml([["本轮", "演", "练", "指南"]]))
    redact_word._process_txbx_xml(path, {})
    out = _texts(path)
    assert "演练" in out, f"跨 run 通用词被吞：{out!r}"
    assert "XX" not in out, f"通用词被误遮：{out!r}"


def test_split_org_entity_masked_whole(tmp_path):
    """跨 run 的机构实体（「信息科技」+「部」）必须**整段**脱敏。

    逐节点处理会把「信息科技」判为通用机构名而保留、只吞掉「部」
    → 产出「信息科技XXXX」混合残态（漏脱 + 形态错误）。
    """
    path = _write(tmp_path, _hdr_xml([["信息科技", "部", "负责系统建设"]]))
    redact_word._process_txbx_xml(path, {})
    out = _texts(path)
    assert "信息科技" not in out, f"跨 run 机构实体被切碎：{out!r}"
    assert "XXXX负责系统建设" in out, f"机构实体未整段脱敏：{out!r}"


# ---------------------------------------------------------------------------
# 2. 跨 run 实体：不得漏脱
# ---------------------------------------------------------------------------

def test_split_name_masked(tmp_path):
    """「张三丰」被拆成三个 run → 跨 run 人名必须被脱敏（不得整段保留）。"""
    path = _write(tmp_path, _hdr_xml([["负责人", "：", "张", "三", "丰"]]))
    redact_word._process_txbx_xml(path, {})
    out = _texts(path)
    assert "张三丰" not in out, f"跨 run 人名漏脱：{out!r}"
    assert "负责人" in out, f"非敏感文本被破坏：{out!r}"


# ---------------------------------------------------------------------------
# 3. 文本框外（单节点）行为零变化
# ---------------------------------------------------------------------------

def test_single_node_behavior_unchanged(tmp_path):
    """单 run 段落：脱敏结果与逐节点处理一致（无回归）。"""
    path = _write(tmp_path, _hdr_xml([["负责人：张三丰"]]))
    redact_word._process_txbx_xml(path, {})
    out = _texts(path)
    assert "张三丰" not in out, f"人名漏脱：{out!r}"


def test_free_text_node_fallback(tmp_path):
    """不在任何 w:p 内的游离 w:t → 回退逐节点处理（仍须脱敏）。"""
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:t>负责人：张三丰</w:t></w:hdr>')
    path = _write(tmp_path, xml)
    redact_word._process_txbx_xml(path, {})
    out = _texts(path)
    assert "张三丰" not in out, f"游离节点人名漏脱：{out!r}"


# ---------------------------------------------------------------------------
# 4. XML 结构合法、文本完整
# ---------------------------------------------------------------------------

def test_xml_structure_intact(tmp_path):
    """处理后 XML 仍可被解析，且文本不丢失。"""
    path = _write(tmp_path, _hdr_xml([["本轮", "演", "练", "指南"], ["第二章"]]))
    redact_word._process_txbx_xml(path, {})
    from lxml import etree
    tree = etree.parse(str(path))          # 解析失败即抛错
    out = "".join(t.text or "" for t in tree.getroot().iter(f"{W}t"))
    assert "本轮演练指南" in out, f"文本丢失：{out!r}"
    assert "第二章" in out, f"文本丢失：{out!r}"


@pytest.mark.parametrize("runs", [
    ["超柜", "适", "操作说明"],
    ["演练", "前", "准备"],
])
def test_split_guard_words_kept(tmp_path, runs):
    """跨 run 的整词保护词同样不得被吞（超柜适 / 演练前）。"""
    path = _write(tmp_path, _hdr_xml([runs]))
    redact_word._process_txbx_xml(path, {})
    out = _texts(path)
    assert "".join(runs) in out, f"跨 run 保护词被吞：{out!r}"
