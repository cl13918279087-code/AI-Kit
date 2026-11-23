#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纯 Python 解析 Word 97-2003 二进制（.doc / OLE2 Compound Document）正文文本。

【为什么需要】
1) LibreOffice（soffice）在受限沙箱 / 内网离线环境中常因 profile 不可写而
   抛出 DeploymentException，转换静默失败；
2) textutil / antiword 等外部依赖在内网不一定存在，且在部分沙箱中写文件被拦截；
3) 纯 Python 解析无外部进程、无临时目录依赖，最适合内网离线交付。

【域（field）语义 —— Issue #82】
Word 97-2003 二进制正文流把「域指令段」与「域结果段」同体存储：
  0x13 域开始 → <指令文本> → 0x14 域分隔 → <结果文本> → 0x15 域结束
其中指令文本（`TOC \\o "1-5" \\h \\z \\u`、`PAGEREF _Toc2599986 \\h`、
`HYPERLINK \\l _Toc…`）是**域的元数据**，不是可见正文；
Word/WPS 渲染出来的「目录」只显示结果段（条目文字 + 页码）。
若把指令文本原样抽进正文，会与「按渲染文本抽取」的另一侧（docx 侧 python-docx
只读 w:t、不读 w:instrText）产生**口径分裂** → 两侧文本不可比，
比对指标被对齐噪声污染（Issue #82）。

本模块默认输出**渲染文本**（丢弃指令段、保留结果段），
需要原始码时可显式传 `include_field_codes=True`。

【实现要点】
- FIB（File Information Block）定位正文与片段表：
  * fcClx = FIB 偏移 0x01A2，lcbClx = FIB 偏移 0x01A6（Word 97 FIB）
  * FIB 偏移 0x000A 的 flags 字段 bit9 (fWhichTblStm) 决定片段表在
    "1Table"（1）还是 "0Table"（0）流
  * ccpText（FIB 内 rgLw97 索引 3，绝对偏移 0x004C）给出正文字符数
- Clx 结构：若干 Prc（0x01 开头）后跟 Pcdt（0x02 开头）
  * Pcdt = PlcPcd：n+1 个 4 字节 CP + n 个 8 字节 Pcd
  * Pcd 的 FcCompressed bit30 置位 → 单字节 cp1252；否则 UTF-16LE
- 段落边界：正文中的 \\r (0x0D) 为段落标记

【对外接口】
  is_ole2(path) -> bool
  extract_doc_text(path) -> str
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import List, Optional

import olefile

_FIB_CLX_OFF = 0x01A2
_FIB_CCP_TEXT_OFF = 0x004C
_FIB_FLAGS_OFF = 0x000A
_FIB_FLAG_FWHICHTBLSTM = 0x0200

# Word 域控制符（Issue #82）
_FIELD_BEGIN = "\x13"   # 域开始
_FIELD_SEP = "\x14"     # 域分隔（指令段 / 结果段分界）
_FIELD_END = "\x15"     # 域结束


def strip_field_instructions(text: str) -> str:
    """丢弃域指令段、保留域结果段（渲染文本语义，Issue #82）。

    规则：
    - 0x13 开启一层域（初始处于「指令段」）；
    - 0x14 将当前层切换为「结果段」；
    - 0x15 关闭当前层；
    - 仅当**所有**未关闭的层都处于「结果段」时，字符才计入输出
      （嵌套域：外层仍在指令段时，内层结果同样丢弃）。

    域外的普通正文原样保留。
    """
    out: List[str] = []
    stack: List[str] = []          # 每层状态："instr" / "result"
    for ch in text:
        if ch == _FIELD_BEGIN:
            stack.append("instr")
            continue
        if ch == _FIELD_SEP:
            if stack:
                stack[-1] = "result"
            continue
        if ch == _FIELD_END:
            if stack:
                stack.pop()
            continue
        if all(state == "result" for state in stack):
            out.append(ch)
    return "".join(out)


def is_ole2(path: Path) -> bool:
    """判断是否为 OLE2 Compound Document（真 .doc）。"""
    try:
        return olefile.isOleFile(str(path))
    except Exception:
        return False


def _read_table_stream(ole: olefile.OleFileIO, use_table1: bool) -> Optional[bytes]:
    for name in (("1Table", "0Table") if use_table1 else ("0Table", "1Table")):
        if ole.exists(name):
            with ole.openstream(name) as fh:
                return fh.read()
    return None


def _parse_clx_piece_table(clx: bytes) -> Optional[List[tuple]]:
    """
    解析 Clx，返回片段表 [(cp_start, cp_end, fc, compressed)]。
    """
    i = 0
    n = len(clx)
    while i < n:
        kind = clx[i]
        if kind == 0x01:          # Prc：跳过
            if i + 3 > n:
                return None
            cb = struct.unpack_from("<h", clx, i + 1)[0]
            i += 3 + cb
        elif kind == 0x02:        # Pcdt
            if i + 5 > n:
                return None
            lcb = struct.unpack_from("<I", clx, i + 1)[0]
            plc = clx[i + 5: i + 5 + lcb]
            if len(plc) < 4 + 8:
                return None
            cnt = (len(plc) - 4) // 12
            cps: List[int] = list(struct.unpack_from("<%dI" % (cnt + 1), plc, 0))
            pcd_off = 4 * (cnt + 1)
            pieces: List[tuple] = []
            for k in range(cnt):
                off = pcd_off + k * 8
                fc_raw = struct.unpack_from("<I", plc, off + 2)[0]
                compressed = bool(fc_raw & 0x40000000)
                fc = (fc_raw & 0x3FFFFFFF)
                if compressed:
                    fc //= 2
                pieces.append((cps[k], cps[k + 1], fc, compressed))
            return pieces
        else:
            return None
    return None


def extract_doc_text(path: str | Path, include_field_codes: bool = False) -> str:
    """
    提取 .doc 正文文本（段落以 \\n 结尾）。

    默认输出**渲染文本**：域指令段（`TOC \\o …` / `PAGEREF …` / `HYPERLINK …`）
    被丢弃、域结果段保留（Issue #82）；传 `include_field_codes=True` 可取回原始码。

    解析失败时返回空字符串（调用方需据此降级）。
    """
    path = Path(path)
    if not is_ole2(path):
        return ""
    ole = olefile.OleFileIO(str(path))
    try:
        if not ole.exists("WordDocument"):
            return ""
        with ole.openstream("WordDocument") as fh:
            fib = fh.read(1024)
        if len(fib) < 0x0200:
            return ""

        flags = struct.unpack_from("<H", fib, _FIB_FLAGS_OFF)[0]
        use_table1 = bool(flags & _FIB_FLAG_FWHICHTBLSTM)
        ccp_text = struct.unpack_from("<I", fib, _FIB_CCP_TEXT_OFF)[0]
        fc_clx = struct.unpack_from("<I", fib, _FIB_CLX_OFF)[0]
        lcb_clx = struct.unpack_from("<I", fib, _FIB_CLX_OFF + 4)[0]

        table = _read_table_stream(ole, use_table1)
        if table is None:
            return ""
        clx = table[fc_clx: fc_clx + lcb_clx]
        pieces = _parse_clx_piece_table(clx)
        if not pieces:
            return ""

        with ole.openstream("WordDocument") as fh:
            raw = fh.read()

        out: List[str] = []
        for cp_start, cp_end, fc, compressed in pieces:
            if cp_start >= ccp_text:
                break
            length = cp_end - cp_start
            if compressed:
                chunk = raw[fc: fc + length]
                text = chunk.decode("cp1252", errors="replace")
            else:
                chunk = raw[fc: fc + length * 2]
                text = chunk.decode("utf-16-le", errors="replace")
            out.append(text)

        full = "".join(out)
        if ccp_text and len(full) > ccp_text:
            full = full[:ccp_text]
        # 域语义处理（Issue #82）：丢弃域指令段、保留域结果段
        if not include_field_codes:
            full = strip_field_instructions(full)
        # 控制字符清理：段落标记 \r → \n
        full = full.replace("\r\n", "\n").replace("\r", "\n")
        full = "".join(ch for ch in full if ch == "\n" or ch == "\t" or ord(ch) >= 0x20)
        return full
    finally:
        try:
            ole.close()
        except Exception:
            pass


def extract_doc_paragraphs(path: str | Path) -> List[str]:
    """按段落切分（去空段），用于与 docx 段落级比对。"""
    text = extract_doc_text(path)
    return [p.strip() for p in text.split("\n") if p.strip()]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python3 doc_ole_text.py <file.doc>")
        raise SystemExit(1)
    t = extract_doc_text(sys.argv[1])
    print(f"chars={len(t)} paragraphs={len(extract_doc_paragraphs(sys.argv[1]))}")
    print(t[:800])
