from typing import Optional
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
import subprocess
import tempfile
import threading
import platform
import os
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from common_rules import apply_redactions
from entity_detector import build_llm_detector

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

def redact_docx(input_path: str, output_path: str, detector=None) -> dict:
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
            c = _process_word_xml_with_llm(doc_xml, detector, "正文")
            _merge_counts(counts, c)

        # ③ 处理页眉页脚（*.xml）
        for xml_file in sorted((tmp_dir / "word").glob("header*.xml")):
            _process_word_xml_with_llm(xml_file, detector, f"页眉 {xml_file.name}")
        for xml_file in sorted((tmp_dir / "word").glob("footer*.xml")):
            _process_word_xml_with_llm(xml_file, detector, f"页脚 {xml_file.name}")

        # ④ 处理批注
        comments_xml = tmp_dir / "word" / "comments.xml"
        if comments_xml.exists():
            _process_word_xml_with_llm(comments_xml, detector, "批注")

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

    修复：以段落（w:p）为单位跨 run 拼接文本，处理完整段落后再分配到各 run，
    解决跨 run 的日期范围等模式无法匹配的问题。
    """
    from lxml import etree

    counts = {}
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.parse(str(path), parser)
    root = tree.getroot()

    body = root.find(f".//{W}body")
    if body is None:
        return counts

    changed = False

    # 以段落为单位处理：拼接段落内所有 run 文本，处理完整段落后再分配回各 run
    for p in list(body.iter(f"{W}p")):
        # 收集段落内所有 w:r 和对应的 w:t 节点
        run_nodes = []
        t_nodes = []
        run_texts = []

        for r in p.iter(f"{W}r"):
            ts = [t for t in r.iter(f"{W}t")]
            if not ts:
                continue
            combined = "".join(t.text or "" for t in ts)
            if combined:  # 只处理有文本的 run
                run_nodes.append(r)
                t_nodes.append(ts)
                run_texts.append(combined)

        if not run_texts:
            continue

        # 拼接完整段落文本
        full_text = "".join(run_texts)

        # 对完整段落文本执行脱敏
        redacted_full = apply_redactions(full_text)
        if redacted_full == full_text:
            continue

        # 将脱敏后的文本分配回各 run
        # 策略：逐 run 按顺序取字符，写入第1个 w:t，后续清空
        pos = 0
        for r, ts in zip(run_nodes, t_nodes):
            run_len = len(run_texts[len(run_nodes) - len(run_nodes) + run_nodes.index(r)])
            # 重新计算索引
            idx = run_nodes.index(r)
            run_len = len(run_texts[idx])

            redacted_chunk = redacted_full[pos:pos + run_len]
            pos += run_len

            if ts:
                ts[0].text = redacted_chunk
                for t in ts[1:]:
                    t.text = None

        changed = True
        counts["段落/单元格"] = counts.get("段落/单元格", 0) + 1

    # 也处理表格单元格（w:tc）
    for tc in list(body.iter(f"{W}tc")):
        # 收集单元格内所有段落
        cell_paras = list(tc.iter(f"{W}p"))
        if not cell_paras:
            continue

        # 拼接单元格内所有段落文本（段落间加换行符）
        all_runs = []
        all_t_nodes = []
        para_texts = []

        for pi, p in enumerate(cell_paras):
            runs_in_p = []
            ts_in_p = []
            for r in p.iter(f"{W}r"):
                ts = [t for t in r.iter(f"{W}t")]
                if not ts:
                    continue
                combined = "".join(t.text or "" for t in ts)
                if combined:
                    runs_in_p.append(r)
                    ts_in_p.append(ts)
            if runs_in_p:
                all_runs.extend(runs_in_p)
                all_t_nodes.extend(ts_in_p)
                para_texts.append("\n".join("".join(t.text or "" for t in ts) for ts in ts_in_p))

        if not all_runs:
            continue

        full_cell_text = "\n".join(para_texts)
        redacted_cell = apply_redactions(full_cell_text)
        if redacted_cell == full_cell_text:
            continue

        # 分配回各 run
        pos = 0
        for idx, (r, ts) in enumerate(zip(all_runs, all_t_nodes)):
            # 计算该 run 对应的原文长度（跨段落时需正确计算）
            # 找到该 run 属于哪个段落
            para_start = 0
            for pi2, p in enumerate(cell_paras):
                runs_in_p_count = sum(1 for rr in p.iter(f"{W}r") if list(rr.iter(f"{W}t")))
                if idx < para_start + runs_in_p_count:
                    break
                para_start += runs_in_p_count

            # 计算 run 长度（同段落内）
            run_len = len("".join(t.text or "" for t in ts))
            redacted_chunk = redacted_cell[pos:pos + run_len]
            pos += run_len

            if ts:
                ts[0].text = redacted_chunk
                for t in ts[1:]:
                    t.text = None

        changed = True
        counts["段落/单元格"] = counts.get("段落/单元格", 0) + 1

    if changed:
        tree.write(str(path), xml_declaration=True, encoding="UTF-8", standalone=True)
        print(f"  [更新] {label or path.name}")

    return counts


def _extract_full_text(root) -> tuple:
    """
    提取 XML 树中所有 w:t 文本（文档顺序，跨 run 拼接）。
    返回 (拼接文本, w:t 节点列表, 每个节点的字符区间列表)。
    区间与节点一一对应，供 LLM offset 精确替换使用。
    """
    # 按段落（</w:p>）分组 w:t 节点，并在段落末尾加换行符
    paras_nodes = []
    para_nodes = []
    for node in root.iter(f"{W}p"):
        para_nodes = list(node.iter(f"{W}t"))
        if para_nodes:
            paras_nodes.append(para_nodes)
    # 按文档顺序平铺，为每段末尾加 \n（最后一端除外）
    parts, nodes, ranges = [], [], []
    offset = 0
    for pi, pnodes in enumerate(paras_nodes):
        for node in pnodes:
            t = node.text or ""
            parts.append(t)
            nodes.append(node)
            ranges.append((offset, offset + len(t)))
            offset += len(t)
        if pi < len(paras_nodes) - 1:
            parts.append("\n")
            ranges.append((offset, offset + 1))
            offset += 1
    return "".join(parts), nodes, ranges


def _manifest_to_spans(full_text: str, manifest, node_texts: list = None) -> list:
    """
    将检测清单转换为 (start, end, text, replacement) 区间列表。
    使用字符偏移在 full_text 中定位，区间与 _extract_full_text 的 ranges 完全对应。
    """
    spans = []
    search_from = 0
    for ent in manifest.entities:
        text = (ent.text or "").strip()
        if not text or len(text) < 2:
            continue  # 跳过单字（避免误匹配"时/我/行"）
        pos = full_text.find(text, search_from)
        match_len = len(text)
        if pos < 0:
            # 容错：忽略空格差异（如 run 间有空格）
            fuzzy = re.compile(re.escape(text).replace(r"\ ", r"\s*"))
            m = fuzzy.search(full_text, search_from)
            if m:
                pos = m.start()
                match_len = m.end() - m.start()
        if pos < 0:
            continue
        search_from = pos + match_len
        spans.append((pos, pos + match_len, text, ent.replacement or "XXX"))

    # 排序并合并重叠区间
    spans.sort(key=lambda x: (x[0], -x[1]))
    merged = []
    for sp in spans:
        if merged and sp[0] < merged[-1][1]:
            last = merged[-1]
            if sp[1] > last[1]:
                merged[-1] = (last[0], sp[1], last[2], last[3])
        else:
            merged.append(sp)
    return merged


def _apply_spans_to_nodes(nodes, ranges, spans) -> int:
    """
    按 offset 区间把替换写入对应 w:t 节点（支持实体跨多个 run 拆分）。
    使用 bisect 精确定位每个子区间属于哪个节点，从右往左应用。
    spans 格式: (start, end, text, replacement)
    """
    import bisect
    replaced = 0
    # node_ends[i] = ranges[i][1]，用于 bisect 找 span 所在节点
    node_ends = [r[1] for r in ranges]

    for start, end, _orig, repl in reversed(spans):
        if start >= end:
            continue
        # 找第一个 node_end > start（bisect_left on ends）
        idx = bisect.bisect_left(node_ends, start + 1)
        # 验证 span 在该节点内 [n_start, n_end)
        if idx >= len(nodes):
            idx = len(nodes) - 1
        n_start, n_end = ranges[idx]
        if not (n_start <= start < n_end and n_start < end <= n_end):
            # span 跨节点或越界，跳过（regex 兜底仍会处理）
            continue
        node_text = nodes[idx].text or ""
        local_s = start - n_start
        local_e = end - n_start
        local_s = max(0, min(local_s, len(node_text)))
        local_e = max(0, min(local_e, len(node_text)))
        if local_s >= local_e:
            continue
        nodes[idx].text = node_text[:local_s] + repl + node_text[local_e:]
        replaced += 1

    return replaced


def _process_word_xml_with_llm(path: Path, detector=None, label: str = "") -> dict:
    """
    对 Word XML 文件执行脱敏（LLM 智能检测 + regex 兜底）。

    流程：
      1. _extract_full_text() 提取全量文本（跨 run 拼接，含表格单元格）
      2. 检测器识别敏感实体 → 转 offset 区间
      3. 优先按 offset 精确替换（支持实体跨多个 run）
      4. regex 兜底（保留原有逻辑，保证覆盖率）

    detector 为 None 或检测失败时，自动退化为纯 regex 脱敏（不抛异常）。
    """
    from lxml import etree

    counts = {}
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.parse(str(path), parser)
    root = tree.getroot()

    full_text, nodes, ranges = _extract_full_text(root)


    # ① LLM 智能检测（失败静默降级，不阻断脱敏）
    # 策略：只把含姓名特征的段落送 LLM（regex 预筛），避免全量超时
    spans = []
    if detector is not None and full_text.strip() and len(full_text.strip()) >= 8:
    
        try:
            # 预筛：提取含"负责人"/"联系人"/"支持人员"等角色词的段落
            # 这些段落最可能含人名，是 regex 的盲区
            # 预筛：收集所有可能含人名的段落（扩大覆盖，不止角色词）
            role_word_lines = []
            para_list = full_text.split("\n")
            # 1) 含角色词的段落
            for para in para_list:
                if any(kw in para for kw in ["负责人", "联系人", "支持人员", "联系人：", "负责人：", "系统负责人", "项目负责人", "编写人", "审核人", "批准人"]):
                    role_word_lines.append(para)
            # 2) 含已知姓氏紧接汉字的段落（人名特征）
            # 从 config 加载姓氏池，避免硬编码
            import json as _json
            try:
                _cfg = _json.load(open(Path(__file__).parent.parent / "config.json", encoding="utf-8"))
                _surname_pool = _cfg.get("surname_pool", "")
            except Exception:
                _surname_pool = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣邓单洪包诸左石崔吉钮龚林门龙段郑孔牛童浦施零厉刘卓曾廖"
            for para in para_list:
                if 2 <= len(para) <= 300:
                    for i, ch in enumerate(para):
                        if ch in _surname_pool and i + 2 < len(para):
                            c2 = para[i+1]
                            if "一" <= c2 <= "鿿":
                                role_word_lines.append(para)
                                break
            # 去重
            llm_input = "\n".join(dict.fromkeys(role_word_lines))[:15000]  # 最多15K字符
            if llm_input.strip():
                result_holder = [None, None]
                def _llm_call():
                    try:
                        result_holder[0] = detector.detect(llm_input)
                    except Exception as e:
                        result_holder[1] = e
                t = threading.Thread(target=_llm_call)
                t.daemon = True
                t.start()
                t.join(timeout=300)
                if t.is_alive():
                    print(f"  [LLM] 检测超时（>60s），跳过 LLM 层")
                elif result_holder[1]:
                    print(f"  [LLM] 检测失败: {result_holder[1]}")
                elif result_holder[0]:
                    manifest = result_holder[0]
                    spans = _manifest_to_spans(full_text, manifest)
                    if spans:
                        print(f"  [LLM检测] {label or path.name}: 识别到 {len(spans)} 个实体")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  [警告] LLM 检测失败（{label or path.name}），继续 regex 脱敏: {e}", file=sys.stderr)

    # ② offset 精确替换（优先）
    changed = False
    if spans:
        n_replaced = _apply_spans_to_nodes(nodes, ranges, spans)
        if n_replaced:
            changed = True
            counts["LLM实体"] = counts.get("LLM实体", 0) + n_replaced

    # ③ regex 兜底（per-run 处理保持不变；跨 run 日期等模式由 step ② LLM offset 处理）
    for r in root.iter(f"{W}r"):
        texts = [t.text or "" for t in r.iter(f"{W}t")]
        combined = "".join(texts)
        if not combined:
            continue

        redacted = apply_redactions(combined)
        if redacted == combined:
            continue

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

def _find_converter() -> Optional[str]:
    """查找 LibreOffice 路径"""
    for cmd in ["soffice", "libreoffice"]:
        r = subprocess.run(["which", cmd], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def redact_doc_to_docx(input_path: str, output_docx: str, detector=None) -> dict:
    """
    将 .doc 转换为 .docx（LibreOffice）后处理。
    注意：此转换会丢失部分旧格式（如 VBS 宏），普通文档格式基本保留。
    """
    converter = _find_converter()
    tmp_dir = Path(tempfile.mkdtemp(prefix="doc_convert_"))

    try:
        if converter:
            result = subprocess.run(
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

        return redact_docx(output_docx, output_docx, detector=detector)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def redact_word(input_path: str, output_path: str = None) -> dict:
    """根据扩展名自动分发处理，返回脱敏统计"""
    detector = build_llm_detector()
    if output_path is None:
        stem = Path(input_path).stem
        ext = Path(input_path).suffix.lower()
        output_path = str(Path(input_path).with_name(f"{stem}_脱敏{ext}"))

    ext = Path(input_path).suffix.lower()
    counts = {}

    if ext == ".docx":
    
        counts = redact_docx(input_path, output_path, detector=detector)
    elif ext == ".doc":
        # .doc → .docx → 处理
        tmp_docx = str(Path(tempfile.gettempdir()) / f"_tmp_{Path(input_path).stem}.docx")
        counts = redact_doc_to_docx(input_path, tmp_docx, detector=detector)
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
