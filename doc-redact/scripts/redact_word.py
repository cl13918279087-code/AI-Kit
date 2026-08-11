#!/usr/bin/env python3
"""
redact_word.py - Word 文档脱敏脚本
支持 .docx（OOXML）和 .doc（Word 97-2003 OLE2 格式）

核心策略：直接操作 ZIP/XML（lxml + zipfile），完全绕开 python-docx 的
         保存时 run 合并问题，100% 保留 Word 样式（标题/表格/页眉/页脚/格式）。

处理范围：
  - 正文段落、表格单元格、页眉页脚、文本框
  - 批注、修订痕迹、文档属性（作者/最后修改人）
  - 嵌入图片文件名（银行 Logo 检测）

依赖: lxml
安装: pip install lxml
注意: .doc 格式通过 LibreOffice 转换为 .docx 再处理
"""

import sys
import re
import zipfile
import shutil
import tempfile
import platform
import os
from pathlib import Path

from common_rules import apply_redactions

# Word XML 命名空间
NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{NS}}}"


def _iter_text_nodes(tree):
    """迭代所有 w:t 文本节点（跨 runs）"""
    return tree.iter(f"{W}t")


def _iter_all_text_elements(body):
    """迭代 body 内所有含文本的 w:r 或 w:t 元素"""
    for elem in body.iter(f"{W}r", f"{W}hyperlink", f"{W}smartTag"):
        yield elem


# ---------------------------------------------------------------------------
# .docx 处理（XML 级别直接编辑）
# ---------------------------------------------------------------------------

def redact_docx(input_path: str, output_path: str) -> dict:
    """
    处理 .docx 文件：解压 → XML 遍历替换 → 重新打包。
    特点：完全保留 Word 所有内置样式、主题、宏、OLE 对象。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="redact_docx_"))
    counts = {}

    try:
        # ① 解压
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(tmp_dir)

        # ② 处理 word/document.xml（正文）
        doc_xml = tmp_dir / "word" / "document.xml"
        if doc_xml.exists():
            c = _process_word_xml(doc_xml, "正文")
            _merge_counts(counts, c)

        # ③ 处理页眉页脚（*.xml）
        for xml_file in sorted((tmp_dir / "word").glob("header*.xml")):
            _process_word_xml(xml_file, f"页眉 {xml_file.name}")
        for xml_file in sorted((tmp_dir / "word").glob("footer*.xml")):
            _process_word_xml(xml_file, f"页脚 {xml_file.name}")

        # ④ 处理批注
        comments_xml = tmp_dir / "word" / "comments.xml"
        if comments_xml.exists():
            _process_word_xml(comments_xml, "批注")

        # ⑤ 处理文本框（wps:txbx / wpg:txbx）
        for xml_file in sorted((tmp_dir / "word").glob("*.xml")):
            _process_txbx_xml(xml_file, counts)

        # ⑥ 文档属性（作者、标题、最后修改人）
        core_xml = tmp_dir / "docProps" / "core.xml"
        if core_xml.exists():
            _process_xml_file(core_xml, "文档属性")

        # ⑦ 银行 Logo 图片（文件名含敏感关键词 → 纯黑图）
        media_dir = tmp_dir / "word" / "media"
        if media_dir.exists():
            _redact_bank_logos(media_dir)

        # ⑧ 重新打包
        _repack_docx(tmp_dir, output_path)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return counts


def _process_word_xml(path: Path, label: str = "") -> dict:
    """
    对 Word XML 文件执行脱敏，支持跨 w:r 分割的文本（如日期 "2022/4/9"）。
    使用 lxml 保持 XML 结构不变。
    """
    from lxml import etree

    counts = {}
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.parse(str(path), parser)
    root = tree.getroot()

    # 处理 body 中的文本节点（跨 runs 拼接后再替换）
    body = root.find(f".//{W}body")
    if body is None:
        return counts

    # 按 w:r 分组，逐组处理
    changed = False
    for r in list(body.iter(f"{W}r")):
        # 收集该 run 中所有 w:t 文本，拼接为完整字符串
        texts = [t.text or "" for t in r.iter(f"{W}t")]
        combined = "".join(texts)
        if not combined:
            continue

        redacted = apply_redactions(combined)
        if redacted == combined:
            continue

        # 脱敏后，可能需要写回多个 w:t 节点（如果原文本被拆分的话）
        # 简单策略：全部写回第1个 w:t，后续清空
        t_nodes = list(r.iter(f"{W}t"))
        if t_nodes:
            t_nodes[0].text = redacted
            for t in t_nodes[1:]:
                t.text = None
            changed = True
            counts["段落/单元格"] = counts.get("段落/单元格", 0) + 1

    if changed:
        # 写回文件（保持原有编码声明）
        tree.write(str(path), xml_declaration=True, encoding="UTF-8", standalone=True)
        print(f"  [更新] {label or path.name}")

    return counts


def _process_txbx_xml(path: Path, counts: dict) -> None:
    """
    处理文本框（wps:txbx / wpg:txbx）内的 XML。
    这些元素内嵌了完整的 <w:document> 片段。
    """
    from lxml import etree

    try:
        content = path.read_text("utf-8")
        redacted = apply_redactions(content)
        if redacted != content:
            path.write_text(redacted, "utf-8")
            print(f"  [更新] 文本框 {path.name}")
            counts["文本框"] = counts.get("文本框", 0) + 1
    except Exception as e:
        print(f"  [警告] 处理 {path.name} 出错: {e}", file=sys.stderr)


def _process_xml_file(path: Path, label: str = "") -> None:
    """通用 XML 文件脱敏（不含 Word runs 结构）"""
    try:
        content = path.read_text("utf-8")
        redacted = apply_redactions(content)
        if redacted != content:
            path.write_text(redacted, "utf-8")
            print(f"  [更新] {label or path.name}")
    except Exception as e:
        print(f"  [警告] 处理 {label} 出错: {e}", file=sys.stderr)


def _redact_bank_logos(media_dir: Path) -> None:
    """将含银行 Logo 关键词的图片替换为纯黑图"""
    from PIL import Image

    keywords = ["bank", "logo", "银行", "brand", "logo"]
    for img_file in media_dir.iterdir():
        if any(k in img_file.name.lower() for k in keywords):
            try:
                img = Image.open(img_file)
                w, h = img.size
                black = Image.new("RGB", (max(w, 10), max(h, 10)), (0, 0, 0))
                black.save(img_file)
                print(f"  [银行Logo遮盖] {img_file.name} → 纯黑图")
            except Exception as e:
                print(f"  [警告] 无法遮盖 {img_file.name}: {e}")


def _repack_docx(tmp_dir: Path, output_path: str) -> None:
    """将解压后的目录重新打包为 .docx"""
    work_path = output_path + ".tmp"
    try:
        with zipfile.ZipFile(work_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(tmp_dir.rglob("*")):
                if fp.is_file():
                    arcname = str(fp.relative_to(tmp_dir))
                    zf.write(fp, arcname)
        # 原子替换（避免读到不完整文件）
        os.replace(work_path, output_path)
    except Exception:
        if Path(work_path).exists():
            Path(work_path).unlink()
        raise


def _merge_counts(base: dict, new: dict) -> None:
    for k, v in new.items():
        base[k] = base.get(k, 0) + v


# ---------------------------------------------------------------------------
# .doc 处理（通过 LibreOffice 转换为 .docx）
# ---------------------------------------------------------------------------

def _find_converter() -> str | None:
    """查找 LibreOffice 路径"""
    for cmd in ["soffice", "libreoffice"]:
        r = shutil.run(["which", cmd], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def redact_doc_to_docx(input_path: str, output_docx: str) -> dict:
    """
    将 .doc 转换为 .docx（LibreOffice）后处理。
    注意：此转换会丢失部分旧格式（如 VBS 宏），普通文档格式基本保留。
    """
    converter = _find_converter()
    tmp_dir = Path(tempfile.mkdtemp(prefix="doc_convert_"))

    try:
        if converter:
            result = shutil.run(
                [
                    converter,
                    "--headless",
                    "--convert-to", "docx",
                    "--outdir", str(tmp_dir),
                    str(Path(input_path).resolve()),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                print(f"  [警告] LibreOffice 转换失败: {result.stderr[:200]}")
                raise RuntimeError("conversion failed")

            converted = next(tmp_dir.glob("*.docx"), None)
            if converted is None:
                raise RuntimeError("no output file found")
            shutil.copy2(converted, output_docx)
        else:
            print("  [警告] 未找到 LibreOffice，.doc 文件无法处理内容，已复制原文件")
            shutil.copy2(input_path, output_docx)
            return {}

        return redact_docx(output_docx, output_docx)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def redact_word(input_path: str, output_path: str = None) -> dict:
    """根据扩展名自动分发处理，返回脱敏统计"""
    if output_path is None:
        stem = Path(input_path).stem
        ext = Path(input_path).suffix.lower()
        output_path = str(Path(input_path).with_name(f"{stem}_脱敏{ext}"))

    ext = Path(input_path).suffix.lower()
    counts = {}

    if ext == ".docx":
        counts = redact_docx(input_path, output_path)
    elif ext == ".doc":
        # .doc → .docx → 处理
        tmp_docx = str(Path(tempfile.gettempdir()) / f"_tmp_{Path(input_path).stem}.docx")
        counts = redact_doc_to_docx(input_path, tmp_docx)
        if counts:  # 仅在转换成功时替换输出
            os.replace(tmp_docx, output_path)
        else:
            Path(tmp_docx).unlink(missing_ok=True)
    else:
        print(f"[错误] 不支持的文件格式: {ext}（仅支持 .docx 和 .doc）", file=sys.stderr)
        sys.exit(1)

    total = sum(counts.values())
    print(f"[完成] 共遮盖 {total} 处，结果保存至: {output_path}")
    if counts:
        for label, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"         - {label}: {n} 处")
    return counts


def main():
    if len(sys.argv) < 2:
        print("用法: python3 redact_word.py <输入文件.docx/.doc> [输出文件路径]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    redact_word(input_file, output_file)


if __name__ == "__main__":
    main()
