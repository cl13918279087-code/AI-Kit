#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脱敏结果 vs 标准版 三方比对分析工具（源文件 / 本程序产物 / 人工标准版）。

核心方法
--------
以「源文件」为参照系，对每个文本块做序列对齐（difflib.SequenceMatcher），
分别求出：
  S = 标准版遮盖掉的源片段集合
  M = 本程序产物遮盖掉的源片段集合
于是：
  S \\ M  →  漏脱（标准版遮了，本程序未遮）——最高风险项
  M \\ S  →  过度脱敏（本程序遮了，标准版保留）——可能误伤业务语义
  S ∩ M  →  一致遮盖

辅助维度：
  D2 占位符形式一致性（同一片段两侧用的替换符是否相同）
  D4 结构保真（块数、字符数差异）
  D5 产物残留敏感模式复扫（独立正则，独立于脱敏规则）

【用法】
  python3 scripts/compare_with_standard.py --pairs pairs.json -o output/compare_report
"""

from __future__ import annotations

import argparse
import difflib
import os
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT / "scripts"))

PLACEHOLDER_RE = re.compile(
    r"(XXXXX@XXXXX|X{2,}银行|XX分行|XX支行|XX:XX:XX:XX:XX:XX|"
    r"X\.X\.X\.X|0XX-XXXXXXXX|XXX+|XXXX+|X{2,}|"
    r"YYYY年MM月DD日|YYYY年MM月|YYYY/MM/DD|YYYY年MM月DD日)"
)

RESIDUAL_PATTERNS = [
    ("MOBILE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("ID_CARD", re.compile(r"(?<!\d)\d{6}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("BANK_CARD", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("BANK_NAME", re.compile(r"郑州银行|海峡银行|中原银行|福建海峡银行|平安银行|平安信用卡")),
    ("IP", re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")),
]


# ---------------------------------------------------------------------------
# 文本抽取
# ---------------------------------------------------------------------------

def _pptx_texts(path: Path) -> List[Tuple[str, str]]:
    from pptx import Presentation

    prs = Presentation(str(path))
    out: List[Tuple[str, str]] = []
    for idx, slide in enumerate(prs.slides, 1):
        chunks: List[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(r.text for r in para.runs)
                    if t.strip():
                        chunks.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            chunks.append(cell.text)
        out.append((f"slide{idx}", "\n".join(chunks)))
    return out


def _docx_texts(path: Path) -> List[Tuple[str, str]]:
    import docx
    from docx.text.paragraph import Paragraph
    from docx.table import Table

    doc = docx.Document(str(path))
    out: List[Tuple[str, str]] = []
    pidx = tidx = 0
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            if para.text.strip():
                pidx += 1
                out.append((f"p{pidx}", para.text))
        elif tag == "tbl":
            tbl = Table(child, doc)
            for row in tbl.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        tidx += 1
                        out.append((f"t{tidx}", cell.text))
    return out


def _xlsx_texts(path: Path) -> List[Tuple[str, str]]:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=False)
    out: List[Tuple[str, str]] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.strip():
                    out.append((f"{ws.title}!{cell.coordinate}", v))
    wb.close()
    return out


def _doc_to_docx(path: Path, td: str) -> Optional[Path]:
    soffice = shutil.which("soffice")
    if not soffice:
        return None
    subprocess.run(
        [soffice, "--headless", "--convert-to", "docx", "--outdir", td, str(path)],
        check=False, capture_output=True, timeout=300,
    )
    produced = sorted(Path(td).glob("*.docx"))
    return produced[0] if produced else None


def _doc_texts(path: Path) -> List[Tuple[str, str]]:
    """
    .doc 文本抽取（三级链，按可靠性降级）。

    L0 魔数识别：扩展名为 .doc 但内容实为 OOXML（PK zip）——直接按 docx 解析。
               实践中「人工标准版 .doc」大量属于此类，此前按扩展名走 soffice
               转换必然失败（转换目标实为 docx，LibreOffice 无法处理）。
    L1 LibreOffice 转换（真 OLE2 且环境可用时）
    L2 纯 Python OLE2/FIB 片段表解析（scripts/doc_ole_text.py）
               无外部进程、无临时目录依赖，受限沙箱与内网离线场景均可工作。
    """
    # L0：内容嗅探
    try:
        magic = path.read_bytes()[:4]
    except OSError:
        magic = b""
    if magic[:2] == b"PK":
        try:
            return _docx_texts(path)
        except Exception:
            return []

    # L2 优先：真 OLE2 时，纯 Python 解析无需任何外部依赖
    try:
        from doc_ole_text import extract_doc_text, is_ole2

        if is_ole2(path):
            text = extract_doc_text(path)
            if text.strip():
                paras = [p.strip() for p in text.split("\n") if p.strip()]
                return [(f"p{i}", p) for i, p in enumerate(paras, 1)]
    except Exception:
        pass

    # L1：LibreOffice 兜底
    work = Path(os.environ.get("WB_COMPARE_TMP", ".tmpwork/compare_doc"))
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []
    for stale in work.glob("*.docx"):
        try:
            stale.unlink()
        except OSError:
            pass
    conv = _doc_to_docx(path, str(work))
    if conv is None:
        return []
    return _docx_texts(conv)


def extract_texts(path: Path) -> List[Tuple[str, str]]:
    ext = path.suffix.lower()
    if ext in (".pptx", ".ppt"):
        try:
            return _pptx_texts(path)
        except Exception:
            return []
    if ext in (".docx", ".doc"):
        try:
            return _docx_texts(path)
        except Exception:
            return _doc_texts(path)
    if ext in (".xlsx", ".xlsm"):
        try:
            return _xlsx_texts(path)
        except Exception:
            return []
    # 未知扩展名：按魔数兜底
    try:
        magic = path.read_bytes()[:4]
    except OSError:
        return []
    if magic[:2] == b"PK":
        return _docx_texts(path) if ext == ".doc" else []
    return []


# ---------------------------------------------------------------------------
# 三方对齐
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _masked_spans(src: str, other: str) -> List[Tuple[str, str]]:
    """
    返回 [(被 other 遮盖的源片段, other 中的替换文本)]。
    源与 other 长度不同，用序列对齐求「other 相对 src 的替换区」。
    """
    sm = difflib.SequenceMatcher(None, src, other, autojunk=False)
    spans: List[Tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete") and i2 > i1:
            frag = src[i1:i2]
            if frag.strip():
                spans.append((frag, other[j1:j2]))
    return spans


def _residual_spans(src: str, other: str) -> List[Tuple[str, str]]:
    """返回 other 中保留下来、未被遮盖的源片段。"""
    sm = difflib.SequenceMatcher(None, src, other, autojunk=False)
    spans: List[Tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" and i2 > i1:
            frag = src[i1:i2]
            if frag.strip():
                spans.append((frag, other[j1:j2]))
    return spans


def _flatten(texts: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """把整份文档拍平成单个块（跨格式对齐时使用）。"""
    joined = "\n".join(v for _, v in texts if v and v.strip())
    return [("doc", joined)] if joined.strip() else []


def compare_pair(name: str, src_path: Path, mine_path: Path,
                  std_path: Path) -> Dict[str, Any]:
    src_list = extract_texts(src_path)
    mine_list = extract_texts(mine_path)
    std_list = extract_texts(std_path)

    # 跨格式（.doc 与 .docx 混用）时段落键空间不可比 → 降级为整文档对齐
    flatten = any(p.suffix.lower() in (".doc", ".ppt") for p in (src_path, mine_path, std_path))
    if flatten:
        src_list = _flatten(src_list)
        mine_list = _flatten(mine_list)
        std_list = _flatten(std_list)

    src = dict(src_list)
    mine = dict(mine_list)
    std = dict(std_list)

    leak: List[Dict[str, Any]] = []      # 漏脱：标准版遮了、产物未遮
    over: List[Dict[str, Any]] = []       # 过度脱敏：产物遮了、标准版未遮
    agree = 0
    ph_mismatch: List[Dict[str, Any]] = []

    for key, stext in src.items():
        if key not in mine or key not in std:
            continue
        mtext, dtext = mine[key], std[key]
        if not stext.strip():
            continue

        std_spans = _masked_spans(stext, dtext)     # 标准版遮盖集合 S
        mine_spans = _masked_spans(stext, mtext)    # 产物遮盖集合 M

        s_frags = [f for f, _ in std_spans]
        m_frags = [f for f, _ in mine_spans]

        # 以标准版遮盖片段为锚，检查产物是否仍保留原文
        for frag, repl in std_spans:
            if frag in m_frags:
                agree += 1
            else:
                # 产物中该片段是否仍以原文形式出现 → 漏脱
                still = frag in mtext
                leak.append({
                    "block": key,
                    "source_fragment": frag[:80],
                    "std_replacement": repl[:40],
                    "still_in_mine": still,
                    "std_context": dtext[:120],
                    "mine_context": mtext[:120],
                })

        # 以产物遮盖片段为锚，检查标准版是否保留原文 → 过度脱敏
        for frag, repl in mine_spans:
            if frag in s_frags:
                continue
            if frag in dtext:
                over.append({
                    "block": key,
                    "source_fragment": frag[:80],
                    "mine_replacement": repl[:40],
                    "std_context": dtext[:120],
                    "mine_context": mtext[:120],
                })

        # 占位符形式一致性
        s_ph = PLACEHOLDER_RE.findall(dtext)
        m_ph = PLACEHOLDER_RE.findall(mtext)
        if sorted(s_ph) != sorted(m_ph):
            ph_mismatch.append({
                "block": key,
                "std_placeholders": sorted(set(s_ph)),
                "mine_placeholders": sorted(set(m_ph)),
            })

    # D4 结构保真
    struct = {
        "src_blocks": len(src),
        "mine_blocks": len(mine),
        "std_blocks": len(std),
        "src_chars": sum(len(_norm(v)) for v in src.values()),
        "mine_chars": sum(len(_norm(v)) for _, v in mine.items()),
        "std_chars": sum(len(_norm(v)) for _, v in std.items()),
    }
    struct["char_diff_pct"] = (
        round((struct["mine_chars"] - struct["std_chars"]) / struct["std_chars"] * 100, 2)
        if struct["std_chars"] else None
    )

    # D5 产物残留复扫
    residual = []
    for key, text in mine.items():
        for label, pat in RESIDUAL_PATTERNS:
            for m in pat.finditer(text):
                residual.append({
                    "block": key,
                    "type": label,
                    "value": m.group(0),
                    "context": text[max(0, m.start() - 25):m.end() + 25],
                })

    std_mask_total = sum(1 for k, v in src.items()
                         if k in std and _masked_spans(v, std[k]))
    leak_real = [x for x in leak if x["still_in_mine"]]
    total_std_spans = len(leak) + agree
    compared = set(src) & set(mine) & set(std)
    src_nonempty = sum(1 for k, v in src.items() if v.strip())

    return {
        "name": name,
        "src_file": str(src_path),
        "mine_file": str(mine_path),
        "std_file": str(std_path),
        "structure": struct,
        "metrics": {
            "src_nonempty_blocks": src_nonempty,
            "compared_blocks": len(compared),
            "align_rate_pct": round(len(compared) / src_nonempty * 100, 2) if src_nonempty else None,
            "std_masked_spans": total_std_spans,
            "agree_spans": agree,
            "leak_spans": len(leak),
            "leak_residual_spans": len(leak_real),
            "over_spans": len(over),
            "ph_mismatch_blocks": len(ph_mismatch),
            "residual_hits": len(residual),
        },
        "leak": leak[:400],
        "over": over[:400],
        "ph_mismatch": ph_mismatch[:200],
        "residual": residual[:200],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="脱敏三方比对分析（源 / 产物 / 标准版）")
    ap.add_argument("--pairs", required=True, help="JSON：[{name, src, mine, std}, ...]")
    ap.add_argument("-o", "--output", default="output/compare_report")
    args = ap.parse_args()

    pairs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for p in pairs:
        src, mine, std = Path(p["src"]), Path(p["mine"]), Path(p["std"])
        missing = [str(x) for x in (src, mine, std) if not x.exists()]
        if missing:
            print(f"[跳过] {p['name']}：缺文件 {missing}")
            continue
        print(f"[比对] {p['name']} ...")
        r = compare_pair(p["name"], src, mine, std)
        results.append(r)
        m = r["metrics"]
        print(f"   标准版遮盖片段 {m['std_masked_spans']}，一致 {m['agree_spans']}，"
              f"漏脱 {m['leak_spans']}（其中原文仍残留 {m['leak_residual_spans']}），"
              f"过度脱敏 {m['over_spans']}，占位符不一致块 {m['ph_mismatch_blocks']}，"
              f"残留复扫 {m['residual_hits']}")

    out_file = out_dir / "compare_result.json"
    out_file.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    print(f"\n结果已写入: {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
