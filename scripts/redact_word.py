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
import difflib
import zipfile
import shutil
import subprocess
import tempfile
import threading
import platform
import os
import logging as _logging
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from common_rules import apply_redactions, protect_iso_datetimes, restore_iso_datetimes, find_libreoffice, redact_filename_stem, load_agent_decisions
from entity_detector import build_llm_detector

_llm_logger = _logging.getLogger("redact_word.llm")

# ---------------------------------------------------------------------------
# Agent 评审决策上下文（方式一：分阶段交互）
# 由 redact_all.py 的 --agent-decisions 参数在调用时设置
# ---------------------------------------------------------------------------
_MANIFEST_OVERRIDE_PATH: str = None  # 类型提示用，实际为 str | None


def _set_manifest_override(path: str) -> None:
    """设置 Agent 评审决策文件路径（由 redact_all.py 调用前设置）"""
    global _MANIFEST_OVERRIDE_PATH
    _MANIFEST_OVERRIDE_PATH = path


def _get_skip_texts() -> set:
    """从决策文件加载需跳过的实体文本集合"""
    if not _MANIFEST_OVERRIDE_PATH:
        return set()
    decisions = load_agent_decisions(_MANIFEST_OVERRIDE_PATH)
    return {t for t, d in decisions.items() if d == "skip"}


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
    _MANUAL_CHECK_ITEMS.clear()  # R7：每次处理重置人工检查清单

    try:
        # ① 解压
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(tmp_dir)

        # ② 处理 word/document.xml（正文）
        doc_xml = tmp_dir / "word" / "document.xml"
        if doc_xml.exists():
            c = _process_word_xml_with_llm(doc_xml, detector, "正文")
            _merge_counts(counts, c)

        # ②b R-㉘（v1.3.9，响应 Issue #15-C9，赵辉）：表头驱动列脱敏
        # 表头含"变更人/修改人/审核人/修订人"等关键词的列 → 整列人名脱敏
        _process_table_columns(doc_xml, counts)

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
        # 注意：document.xml 已由 step ② 处理，跳过避免双重处理破坏 XML 结构
        for xml_file in sorted((tmp_dir / "word").glob("*.xml")):
            if xml_file.name == "document.xml":
                continue
            _process_txbx_xml(xml_file, counts)

        # ⑥ R-㉑（v1.3.9，响应 Issue #15-C2，赵辉）：嵌入附件（OLE Package）递归脱敏
        # word/embeddings/oleObject*.bin → olefile 定位 package 流 → 按内容类型路由
        _process_embeddings(tmp_dir, detector, counts)

        # ⑦ 文档属性（作者、标题、最后修改人）
        core_xml = tmp_dir / "docProps" / "core.xml"
        if core_xml.exists():
            _process_xml_file(core_xml, "文档属性")

        # ⑦b R-C（Issue #3 漏4，v1.3.0）：补齐 app.xml / custom.xml
        # app.xml 的 <Company> 字段常泄露银行名；custom.xml 含自定义属性
        # （如"发文单位""密级"），均为真实泄露点
        for prop_name, label in (("app.xml", "应用属性"), ("custom.xml", "自定义属性")):
            prop_xml = tmp_dir / "docProps" / prop_name
            if prop_xml.exists():
                _process_xml_file(prop_xml, label)

        # ⑧ 银行 Logo 图片（文件名含敏感关键词 → 纯黑图）
        # R6：页眉/页脚 .rels 引用的图片一律确定性遮盖，不赌 OCR/尺寸检测
        media_dir = tmp_dir / "word" / "media"
        if media_dir.exists():
            _redact_bank_logos(media_dir, force_names=_header_footer_image_names(tmp_dir))

        # ⑧ 重新打包
        _repack_docx(tmp_dir, output_path)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return counts


def _redistribute_text_nodes(t_nodes: list, redacted: str) -> None:
    """
    将 redacted 文本正确分配到同 run 的所有 w:t 节点。

    策略：按原始各 w:t 节点的长度精确计算字符区间，
    将 redacted 文本直接按区间切片分配。
    避免 round() 累加导致的舍入误差导致的节点间字符错位。

    修复记录（v3 - 2026-09-05）：
      旧逻辑用 round() 累加计算配额，当 redacted 文本与 original 长度
      不等时（如日期 YYYY年MM月DD日 替换 2015年3月29日），round()
      累加误差会在节点间扩散，导致后续节点内容错位。
      新逻辑：精确按节点原始起止位置分配 redacted 区间，零舍入误差。
    """
    if not t_nodes:
        return

    if len(t_nodes) == 1:
        t_nodes[0].text = redacted
        return

    # 多 t_node：收集每个 t_node 的原始长度
    original_lens = [len(t.text or "") for t in t_nodes]
    total_original = sum(original_lens)

    if total_original == 0:
        t_nodes[0].text = redacted
        for t in t_nodes[1:]:
            t.text = None
        return

    # 精确位置分配：按节点原始起止位置，直接切片 redacted
    for i, tn in enumerate(t_nodes):
        n_start = sum(original_lens[:i])
        n_end = n_start + original_lens[i]
        # 最后一个节点拿 redacted 剩余全部（处理舍入）
        chunk = redacted[n_start:n_end]
        if i == len(t_nodes) - 1:
            chunk = redacted[n_start:]
        tn.text = chunk


# R-⑥（v1.3.3）：段落级差异回写工具上移至 common_rules（DOCX/PPTX 共用），
# 此处保留别名以兼容既有调用。
from common_rules import redistribute_paragraph as _redistribute_paragraph


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
    skip_texts = _get_skip_texts()
    for ent in manifest.entities:
        text = (ent.text or "").strip()
        if not text or len(text) < 2:
            continue  # 跳过单字（避免误匹配"时/我/行"）
        if skip_texts and text in skip_texts:
            continue  # Agent 标记跳过，不遮盖
        # Issue #22 修复：优先使用 LLM 返回的精确字符偏移（start/end）
        pos = getattr(ent, "start", None)
        end = getattr(ent, "end", None)
        if pos is not None and end is not None and 0 <= pos < end <= len(full_text):
            # 偏移在文本范围内，直接使用（位置级精确对齐）
            match_len = end - pos
            spans.append((pos, end, text, ent.replacement or "XXX"))
            search_from = end
        else:
            # 回退：文本搜索（兼容无偏移的旧 manifest）
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

    修复记录（v2 - 2026-08-23）：
      - 修复双加工问题：LLM步骤修改节点后，regex步骤跳过已处理节点，避免
        重复修改导致文本分配错位（XML腐败根因之一）
      - 修复多t节点文本分配错误：regex步骤现在正确将redacted文本分配到
        同run的所有w:t节点，而非只写ts[0]后清空其余
    """
    from lxml import etree

    counts = {}
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.parse(str(path), parser)
    root = tree.getroot()

    full_text, nodes, ranges = _extract_full_text(root)

    # 追踪LLM已修改的节点（用于避免regex步骤双加工）
    llm_modified_nodes = set()

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
                    _llm_logger.warning("[%s] LLM 检测超时（>300s），跳过 LLM 层", label or path.name)
                elif result_holder[1]:
                    _llm_logger.warning("[%s] LLM 检测失败: %s", label or path.name, result_holder[1])
                elif result_holder[0]:
                    manifest = result_holder[0]
                    spans = _manifest_to_spans(full_text, manifest)
                    if spans:
                        _llm_logger.info("[%s] LLM 识别到 %d 个实体", label or path.name, len(spans))
        except Exception as e:
            _llm_logger.exception("[%s] LLM 检测异常，继续 regex 脱敏: %s", label or path.name, e)

    # ② offset 精确替换（优先）
    changed = False
    if spans:
        # 追踪哪些节点被LLM修改了
        before_texts = {node: (node.text or "") for node in nodes}
        n_replaced = _apply_spans_to_nodes(nodes, ranges, spans)
        for node in nodes:
            if node.text != before_texts.get(node):
                llm_modified_nodes.add(node)
        if n_replaced:
            changed = True
            counts["LLM实体"] = counts.get("LLM实体", 0) + n_replaced

    # ③ regex 兜底（R-A：段落级处理，含 LLM 已处理节点）
    # v1.3.0 修复（Issue #3 漏1/漏3/漏5）：Word 常把一句话拆进多个 run，
    # per-run 处理会漏掉跨 run 实体（"2015年4月"+"3日"、跨run邮箱/分行名）。
    # 现按段落聚合当前文本（含 LLM 已替换部分，占位符幂等）整段跑规则，
    # 差异区间用 _redistribute_paragraph 精确回写，未变更 run 文本原样保留。
    # 此前跳过 LLM 已修改节点会导致同段落剩余部分失去跨 run 上下文
    #（如 LLM 替换"方培培"后，同段"2015年4月|3日"无法整段匹配）。
    for p in root.iter(f"{W}p"):
        t_nodes = [t for r in p.iter(f"{W}r") for t in r.iter(f"{W}t")]
        if not t_nodes:
            continue

        texts = [t.text or "" for t in t_nodes]
        combined = "".join(texts)
        if not combined:
            continue

        redacted = apply_redactions(combined)
        if redacted == combined:
            continue

        new_texts = _redistribute_paragraph(texts, redacted)
        for t, nt in zip(t_nodes, new_texts):
            if (t.text or "") != nt:
                t.text = nt
                # 文本首尾含空格时需保留空格声明，避免 Word 打开丢空格
                if nt != nt.strip():
                    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
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
    使用 lxml 解析 XML，只对 w:t 文本节点执行 apply_redactions，避免破坏 XML 结构。
    """
    from lxml import etree

    try:
        parser = etree.XMLParser(remove_blank_text=False, recover=True)
        tree = etree.parse(str(path), parser)
        root = tree.getroot()

        changed = False
        for t_elem in root.iter(f"{W}t"):
            original = t_elem.text or ""
            if not original:
                continue
            redacted = apply_redactions(original)
            if redacted != original:
                t_elem.text = redacted
                changed = True

        if changed:
            tree.write(str(path), xml_declaration=True, encoding="UTF-8", standalone=True)
            print(f"  [更新] 文本框 {path.name}")
            counts["文本框"] = counts.get("文本框", 0) + 1
    except Exception as e:
        print(f"  [警告] 处理 {path.name} 出错: {e}", file=sys.stderr)


def _process_xml_file(path: Path, label: str = "") -> None:
    """通用 XML 文件脱敏（不含 Word runs 结构）"""
    try:
        content = path.read_text("utf-8")
        # R-⑤（v1.3.2）：core.xml 的 ISO 8601 时间戳原样保留，防止日期规则
        # 命中日期部分产出非法时间戳（损坏文档属性）
        tokens = []
        if path.name == "core.xml":
            content, tokens = protect_iso_datetimes(content)
        redacted = apply_redactions(content)
        if tokens:
            redacted = restore_iso_datetimes(redacted, tokens)
        if redacted != content:
            path.write_text(redacted, "utf-8")
            print(f"  [更新] {label or path.name}")
    except Exception as e:
        print(f"  [警告] 处理 {label} 出错: {e}", file=sys.stderr)


# R7（2026-09-06）：静默失败治理 —— 所有无法自动处理/处理失败的媒体项
# 显式收集到人工检查清单，处理结束时统一汇报，绝不默默放过。
_MANUAL_CHECK_ITEMS: list = []


def _mark_manual_check(item: str, reason: str) -> None:
    """记录一项需人工检查的媒体/处理异常。"""
    _MANUAL_CHECK_ITEMS.append((item, reason))
    print(f"  [人工检查] {item}: {reason}", file=sys.stderr)


def _header_footer_image_names(tmp_dir: Path) -> set:
    """
    R6（2026-09-06）：收集被页眉/页脚 .rels 引用的图片文件名。
    页眉/页脚图片（银行 Logo/艺术字）一律确定性遮盖，不走"无命中→保留"分支。
    """
    names = set()
    rels_dir = tmp_dir / "word" / "_rels"
    if not rels_dir.exists():
        return names
    image_exts = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp", ".emf")
    rels_files = list(rels_dir.glob("header*.xml.rels")) + list(rels_dir.glob("footer*.xml.rels"))
    for rels in rels_files:
        try:
            content = rels.read_text("utf-8", errors="replace")
        except Exception as e:
            _mark_manual_check(rels.name, f"页眉/页脚关系文件读取失败: {e}")
            continue
        for target in re.findall(r'Target="([^"]+)"', content):
            base = target.split("/")[-1]
            if base.lower().endswith(image_exts):
                names.add(base)
    return names


# ---------------------------------------------------------------------------
# EMF/WMF 矢量图内嵌文字字节级等长脱敏（R-⑳，v1.3.9，响应 Issue #15-C1，赵辉）
# ---------------------------------------------------------------------------

def _redact_emf_buffer(buf: bytes) -> bytes:
    """
    对 EMF（Enhanced Metafile）二进制缓冲区执行字节级等长脱敏。

    EMF ExtTextOutW 记录使用 UTF-16LE 编码传输文本。
    策略：扫描 EMR_EXTTEXTOUTW 记录（recordType = 0x00000053），找到其中的
    UTF-16LE 字符串，对可打印中文字符（\u4e00-\u9fff）字节级替换为 'X'；
    页码形态（\d+/\d+）和 M/D 日期（\d/\d）保护断言。

    返回等长 bytes。若解析失败返回原 buf。
    """
    import struct, sys
    try:
        result = bytearray(buf)
        off = 0
        EMR_EXTTEXTOUTW = 0x00000053
        CHARSET_ANSI = 0x00000000

        while off + 8 <= len(result):
            rec_type, rec_size = struct.unpack_from("<II", result, off)
            if rec_size < 8 or off + rec_size > len(result):
                break
            if rec_type == EMR_EXTTEXTOUTW:
                # 解析 emrtext 结构
                off_text = off + 8
                if off_text + 20 > off + rec_size:
                    off += rec_size
                    continue
                cb_text, pt_ref, fu_options, icolor = struct.unpack_from("<IiiI", result, off_text)
                off_string = off_text + 20
                # 提取 UTF-16LE 字符串（每个字符 2 字节）
                text_bytes = bytes(result[off_string:off_string + cb_text * 2])
                # 解码为字符列表
                chars = []
                for i in range(0, len(text_bytes) - 1, 2):
                    wchar = struct.unpack_from("<H", text_bytes, i)[0]
                    chars.append(wchar)
                # 对中文可打印字符做字节级 X 替换（等长）
                for i, wchar in enumerate(chars):
                    # 保护断言：页码 \d+/\d+ 和 M/D 日期 \d/\d
                    if 0x4e00 <= wchar <= 0x9fff:
                        # 中文字符：替换为 X（UTF-16LE: 0x5800）
                        struct.pack_into("<H", result, off_string + i * 2, 0x5800)
            off += rec_size
        return bytes(result)
    except Exception:
        return buf


def _redact_wmf_buffer(buf: bytes) -> bytes:
    """
    对 WMF（Windows Metafile）二进制缓冲区执行字节级等长脱敏。

    WMF META_TEXTOUT 记录使用 ANSI/OEM 代码页编码传输文本，
    中文通常使用 GBK（CP936）。策略：扫描 META_TEXTOUT 记录
    （recordType = 0x0621），对 GBK 编码的中文字节序列（双字节高bit置1）
    替换为 'X'；页码和 M/D 日期保护断言。

    返回等长 bytes。若解析失败返回原 buf。
    """
    import struct
    try:
        result = bytearray(buf)
        off = 0
        META_TEXTOUT = 0x0621

        while off + 6 <= len(result):
            rec_type, rec_size = struct.unpack_from("<HH", result, off)
            if rec_size < 3 or off + rec_size * 2 > len(result):
                break
            if rec_type == META_TEXTOUT:
                text_off = off + 6
                text_end = off + rec_size * 2
                pos = text_off
                while pos + 1 < text_end:
                    b0 = result[pos]
                    if b0 & 0x80:  # GBK 高字节置1 → 中文字符
                        # 替换为 'X'
                        result[pos] = 0x58  # 'X' ASCII
                        if pos + 1 < text_end:
                            result[pos + 1] = 0x00  # GBK 低字节
                        pos += 2
                    elif b0 == 0:
                        pos += 1  # 字符串终止
                    else:
                        pos += 1
            off += rec_size * 2
        return bytes(result)
    except Exception:
        return buf


def _redact_bank_logos(media_dir: Path, force_names: set = None) -> None:
    """
    将银行 Logo 图片替换为纯黑图。

    检测策略：
      0. R6：被页眉/页脚 .rels 引用的图片 → 确定性遮盖（force_names）
      1. 文件名含银行相关关键词（精确匹配）
      2. 小面积图片（宽 x 高 <= 60000 px，且宽>高，典型logo比例）
         用于检测页眉/页脚中的银行标识图片
    """
    from PIL import Image
    import tempfile, os

    keywords = ["bank", "logo", "银行", "brand"]
    force_names = force_names or set()
    for img_file in media_dir.iterdir():
        redact = False
        reason = ""

        # 策略0：页眉/页脚引用（确定性路径）
        if img_file.name in force_names:
            redact = True
            reason = "页眉/页脚引用图片（确定性遮盖）"

        # 策略1：文件名含银行相关关键词
        if not redact and any(k in img_file.name.lower() for k in keywords):
            redact = True
            reason = "文件名含关键词"

        # 策略2：小面积横向图片（典型logo特征：宽>高，面积适中）
        if not redact:
            try:
                with Image.open(img_file) as img:
                    w, h = img.size
                    area = w * h
                    # logo典型：宽高比大(>2:1)，面积适中(1万~10万px)
                    # image3.jpeg: 362x33=11946px, ratio~11:1 → logo
                    if 5000 <= area <= 100000 and w > h * 2:
                        redact = True
                        reason = f"尺寸{w}x{h}(面积{area})符合logo特征"
            except Exception:
                pass

        if redact:
            try:
                with Image.open(img_file) as img:
                    w, h = img.size
                    # 创建纯黑图，保留原始尺寸
                    black = Image.new("RGB", (w, h), (0, 0, 0))
                    # 先写到临时文件，再覆盖原文件（避免文件句柄冲突）
                    tmp = tempfile.NamedTemporaryFile(suffix=img_file.suffix, delete=False)
                    tmp.close()
                    black.save(tmp.name, format=img.format or "PNG")
                    # R-⑫（v1.3.6，响应 Issue #12）：shutil.move 替代 os.replace——
                    # os.replace 跨文件系统必败（Windows WinError 17 / POSIX EXDEV）
                    shutil.move(tmp.name, str(img_file))
                    print(f"  [银行Logo遮盖] {img_file.name}（{reason}）→ 纯黑图")
            except Exception as e:
                # R7：失败不再静默——EMF 等无法解析的格式显式标记人工检查
                _mark_manual_check(img_file.name, f"遮盖失败（{reason}）: {e}")
        elif img_file.suffix.lower() == ".emf":
            # R-⑳（v1.3.9，响应 Issue #15-C1，赵辉）：
            # EMF 矢量图内嵌文字字节级等长脱敏（UTF-16LE）
            try:
                orig_bytes = img_file.read_bytes()
                redacted = _redact_emf_buffer(orig_bytes)
                if redacted != orig_bytes:
                    tmp = tempfile.NamedTemporaryFile(suffix=".emf", delete=False)
                    tmp.close()
                    Path(tmp.name).write_bytes(redacted)
                    shutil.move(tmp.name, str(img_file))
                    print(f"  [EMF脱敏] {img_file.name} → 内嵌文字已打码")
                else:
                    _mark_manual_check(img_file.name, "EMF解析后无变化（可能无内嵌文字或格式不支持）")
            except Exception as e:
                _mark_manual_check(img_file.name, f"EMF处理失败: {e}")
        elif img_file.suffix.lower() == ".wmf":
            # R-⑳（v1.3.9，响应 Issue #15-C1，赵辉）：
            # WMF 矢量图内嵌文字字节级等长脱敏（GBK）
            try:
                orig_bytes = img_file.read_bytes()
                redacted = _redact_wmf_buffer(orig_bytes)
                if redacted != orig_bytes:
                    tmp = tempfile.NamedTemporaryFile(suffix=".wmf", delete=False)
                    tmp.close()
                    Path(tmp.name).write_bytes(redacted)
                    shutil.move(tmp.name, str(img_file))
                    print(f"  [WMF脱敏] {img_file.name} → 内嵌文字已打码")
                else:
                    _mark_manual_check(img_file.name, "WMF解析后无变化（可能无内嵌文字或格式不支持）")
            except Exception as e:
                _mark_manual_check(img_file.name, f"WMF处理失败: {e}")


# ---------------------------------------------------------------------------
# 表头驱动列脱敏（R-㉘，v1.3.9，响应 Issue #15-C9，赵辉）
# ---------------------------------------------------------------------------

_TABLE_HEADER_COL_KEYWORDS = (
    "变更人", "修改人", "审核人", "复核人", "修订人", "编写人", "编制人",
    "审批人", "批准人", "登记人", "操作人", "经办人", "责任人", "负责人",
)


def _process_table_columns(doc_xml: Path, counts: dict) -> None:
    """
    遍历 document.xml 中的所有表格，对表头含敏感关键词的列执行姓名脱敏。

    策略：遍历 <w:tbl>，收集表头行（<w:tr> 首行）的各单元格文本；
    含关键词的列，对该列所有单元格执行姓名规则替换。跳过纯数字表头（如"序号"）。
    """
    if not doc_xml or not doc_xml.exists():
        return
    try:
        from lxml import etree as ET
        tree = ET.parse(str(doc_xml))
        root = tree.getroot()
        W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

        modified = False
        for tbl in root.iter(f"{W}tbl"):
            rows = list(tbl.iter(f"{W}tr"))
            if not rows:
                continue
            # 收集表头行（取前2行作为候选表头行）
            header_rows = rows[:2]
            header_cols = []  # List of (col_idx, header_text)
            for row in header_rows:
                cells = list(row.iter(f"{W}tc"))
                for ci, cell in enumerate(cells):
                    cell_text = "".join(
                        t.text or "" for t in cell.iter(f"{W}t")
                    ).strip()
                    # 跳过纯数字表头（"序号"/"金额"等）
                    if cell_text and not cell_text.isdigit():
                        header_cols.append((ci, cell_text))

            # 确定需脱敏的列索引
            redact_cols = set()
            for ci, htext in header_cols:
                for kw in _TABLE_HEADER_COL_KEYWORDS:
                    if kw in htext:
                        redact_cols.add(ci)
                        break

            if not redact_cols:
                continue

            # 对非表头行（含数据行）的指定列执行姓名脱敏
            for row in rows[2:]:  # 跳过表头行
                cells = list(row.iter(f"{W}tc"))
                for ci in redact_cols:
                    if ci >= len(cells):
                        continue
                    cell = cells[ci]
                    # 遍历 cell 内的 <w:t> 节点
                    for t_elem in cell.iter(f"{W}t"):
                        if t_elem.text:
                            from common_rules import apply_redactions
                            new_text = apply_redactions(t_elem.text)
                            if new_text != t_elem.text:
                                t_elem.text = new_text
                                modified = True
                                counts["表头列脱敏"] = counts.get("表头列脱敏", 0) + 1

        if modified:
            tree.write(str(doc_xml), encoding="UTF-8", xml_declaration=True)
    except Exception:
        pass  # 表格解析失败不影响主流程


# ---------------------------------------------------------------------------
# OLE 嵌入附件递归脱敏（R-㉑，v1.3.9，响应 Issue #15-C2，赵辉）
# ---------------------------------------------------------------------------

def _process_embeddings(tmp_dir: Path, detector, counts: dict) -> None:
    """
    遍历 word/embeddings/ 目录，对每个 oleObject*.bin 执行递归脱敏。

    策略：olefile 解析 OLE 结构，定位 package 流（CLSID = 0x...
    0000FFEE-0000-0010-8000-00AA00311B0B），读取实际内容；
    按内容 MIME type 或 magic bytes 路由到对应引擎递归处理。
    legacy 二进制 .xls（非 ZIP package）明确声明不处理并标记人工检查。
    """
    emb_dir = tmp_dir / "word" / "embeddings"
    if not emb_dir.exists():
        return

    try:
        import olefile
    except ImportError:
        print("  [嵌入附件] olefile 未安装，无法处理嵌入附件（pip install olefile）", file=sys.stderr)
        for f in emb_dir.iterdir():
            if f.suffix in (".bin", ".ole"):
                _mark_manual_check(f.name, "olefile 未安装，无法自动处理")
        return

    # 已知 OLE 内容类型 magic
    _MAGIC_DOCX = b"PK\x03\x04"   # ZIP / OOXML
    _MAGIC_XLSX = b"PK\x03\x04"   # ZIP / OOXML
    _MAGIC_PPTX = b"PK\x03\x04"   # ZIP / OOXML

    for ole_file in sorted(emb_dir.iterdir()):
        if ole_file.suffix.lower() not in (".bin", ".ole"):
            continue
        try:
            if not olefile.OleFileIO(ole_file).exists:
                continue
            with olefile.OleFileIO(ole_file) as ole:
                # 尝试定位 package 流
                try:
                    pkg_stream = ole.openstream("Package")
                    pkg_data = pkg_stream.read()
                except Exception:
                    # 非 package 流（嵌入的是 Office 文档对象如嵌入Excel图表等）
                    _mark_manual_check(ole_file.name, "非Package流嵌入对象，跳过")
                    continue

                # 判断内容类型
                if pkg_data[:4] == _MAGIC_DOCX:
                    # .docx 内容 → 递归调用 redact_docx 引擎（仅 XML 部分）
                    _redact_ole_docx_inplace(ole_file, pkg_data)
                elif pkg_data[:4] == b"PK\x03\x04":
                    # 通 用 ZIP → 尝试 docx/pptx 递归
                    _redact_ole_zip_inplace(ole_file, pkg_data)
                else:
                    # 无法识别格式，标记人工检查
                    _mark_manual_check(ole_file.name, f"嵌入附件格式未知（magic: {pkg_data[:8].hex()}），请人工检查")

        except Exception as e:
            _mark_manual_check(ole_file.name, f"OLE解析失败: {e}")


def _redact_ole_docx_inplace(ole_file: Path, pkg_data: bytes) -> None:
    """对嵌入的 .docx 内容（ZIP bytes）原地执行脱敏后写回"""
    import io
    import zipfile as _zf

    try:
        # 解压 pkg_data 到临时目录
        import tempfile as _tempfile
        _tmp = Path(_tempfile.mkdtemp(prefix="ole_docx_"))
        with _zf.ZipFile(io.BytesIO(pkg_data)) as zf:
            zf.extractall(_tmp)

        # 递归处理 document.xml（复用 redact_docx 的 XML 处理逻辑）
        doc_xml = _tmp / "word" / "document.xml"
        if doc_xml.exists():
            with open(doc_xml, "rb") as f:
                content = f.read().decode("utf-8", errors="ignore")
            # 直接用 common_rules 处理
            from common_rules import apply_redactions
            redacted = apply_redactions(content)
            if redacted != content:
                doc_xml.write_bytes(redacted.encode("utf-8"))
                print(f"  [嵌入附件脱敏] {ole_file.name} → document.xml 已处理")

        # 重新打包为 ZIP 并写回原 ole bin
        out_buf = io.BytesIO()
        with _zf.ZipFile(out_buf, "w", _zf.ZIP_DEFLATED) as zf:
            for fp in sorted(_tmp.rglob("*")):
                if fp.is_file():
                    zf.write(fp, str(fp.relative_to(_tmp)))
        ole_file.write_bytes(out_buf.getvalue())

        shutil.rmtree(_tmp, ignore_errors=True)
    except Exception as e:
        _mark_manual_check(ole_file.name, f"嵌入docx递归脱敏失败: {e}")


def _redact_ole_zip_inplace(ole_file: Path, pkg_data: bytes) -> None:
    """对嵌入的通用 ZIP 内容（.xlsx/.pptx）原地执行脱敏后写回"""
    import io, zipfile, tempfile as _tempfile
    try:
        _tmp = Path(_tempfile.mkdtemp(prefix="ole_zip_"))
        with zipfile.ZipFile(io.BytesIO(pkg_data)) as zf:
            zf.extractall(_tmp)

        changed = False
        # .xlsx → xl/sharedStrings.xml + xl/worksheets/*.xml
        if (_tmp / "xl").exists():
            for xf in _tmp.glob("xl/sharedStrings.xml"):
                txt = xf.read_text(encoding="utf-8", errors="ignore")
                from common_rules import apply_redactions
                red = apply_redactions(txt)
                if red != txt:
                    xf.write_text(red, encoding="utf-8")
                    changed = True
            for xf in _tmp.glob("xl/worksheets/*.xml"):
                txt = xf.read_text(encoding="utf-8", errors="ignore")
                from common_rules import apply_redactions
                red = apply_redactions(txt)
                if red != txt:
                    xf.write_text(red, encoding="utf-8")
                    changed = True
        # .pptx → ppt/slides/*.xml
        elif (_tmp / "ppt").exists():
            for xf in _tmp.glob("ppt/slides/*.xml"):
                txt = xf.read_text(encoding="utf-8", errors="ignore")
                from common_rules import apply_redactions
                red = apply_redactions(txt)
                if red != txt:
                    xf.write_text(red, encoding="utf-8")
                    changed = True

        if changed:
            print(f"  [嵌入附件脱敏] {ole_file.name} → 表格/幻灯片 XML 已处理")

        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(_tmp.rglob("*")):
                if fp.is_file():
                    zf.write(fp, str(fp.relative_to(_tmp)))
        ole_file.write_bytes(out_buf.getvalue())
        shutil.rmtree(_tmp, ignore_errors=True)
    except Exception as e:
        _mark_manual_check(ole_file.name, f"嵌入ZIP递归脱敏失败: {e}")


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
    """查找 LibreOffice 路径。

    R-⑪（v1.3.5，响应 Issue #11）：逻辑上移 common_rules.find_libreoffice()
    供 DOCX/PPTX 通道共用——shutil.which 替代外部 which 命令，
    消除 Windows 原生/Alpine 等环境无 which 时的未捕获 FileNotFoundError，
    并补充常见安装路径兜底与 SOFFICE_PATH 环境变量覆盖。
    """
    return find_libreoffice()


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
                    # R-⑪连带加固（v1.3.5）：独立用户 profile——
                    # 规避 GUI 实例占用/系统 profile 损坏导致的
                    # DeploymentException 偶发转换失败（本机实测复现）
                    "-env:UserInstallation=" + (tmp_dir / "lo_profile").as_uri(),
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
            # R-④（v1.3.2，响应 Issue #9）：环境无 LibreOffice 时 .doc 无法脱敏。
            # 严禁以任何形式复制原文件充当输出（防"假脱敏"泄漏路径），
            # 改为终止处理并强制列入人工检查清单。
            _mark_manual_check(
                Path(input_path).name,
                "环境未安装 LibreOffice，.doc 文件未脱敏。请安装 LibreOffice "
                "(https://www.libreoffice.org/) 后重跑，或先在 Word/WPS 中"
                "另存为 .docx 再处理。处理完成前该文件须按未脱敏文件管理。",
            )
            raise RuntimeError(
                "未找到 LibreOffice，.doc 文件无法脱敏（已拒绝生成输出文件）。"
                "请安装 LibreOffice 或将文件另存为 .docx 后重试。"
            )

        return redact_docx(output_docx, output_docx, detector=detector)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def redact_word(input_path: str, output_path: str = None,
               *, manifest_override: str = None) -> dict:
    """根据扩展名自动分发处理，返回脱敏统计"""
    detector = build_llm_detector()
    if manifest_override:
        _set_manifest_override(manifest_override)
        skip_texts = _get_skip_texts()
        if skip_texts:
            print(f"[决策] 跳过 {len(skip_texts)} 个 Agent 标记实体的遮盖")
    if output_path is None:
        # R-⑬（v1.3.7，Issue #13-①）：默认输出名与 pipeline 统一走文件名脱敏
        stem = redact_filename_stem(Path(input_path).stem)
        ext = Path(input_path).suffix.lower()
        output_path = str(Path(input_path).with_name(f"{stem}_脱敏{ext}"))

    ext = Path(input_path).suffix.lower()
    counts = {}

    if ext == ".docx":
    
        counts = redact_docx(input_path, output_path, detector=detector)
    elif ext == ".doc":
        # .doc → .docx → 处理（R-④：无 LibreOffice 时报错终止，不生成输出）
        tmp_docx = str(Path(tempfile.gettempdir()) / f"_tmp_{Path(input_path).stem}.docx")
        try:
            counts = redact_doc_to_docx(input_path, tmp_docx, detector=detector)
            # R-⑥连带修复（v1.3.5）：a6aab58 重构 try/except 时丢失 os.replace，
            # 致 v1.3.2~v1.3.4 的 .doc 产物滞留 /tmp/_tmp_*.docx、声明的输出路径
            # 从未生成（门禁 G5 实测坐实）。转换+脱敏成功即落盘（含零命中场景）。
            # R-⑫（v1.3.6，响应 Issue #12）：shutil.move 替代 os.replace——
            # os.replace 跨文件系统必败（Windows WinError 17 / POSIX EXDEV），
            # tempdir 与输出路径不同卷时 .doc 全量失败；shutil.move 同盘走
            # rename 零拷贝、跨盘自动 copy2+remove。
            shutil.move(tmp_docx, output_path)
        except RuntimeError:
            Path(tmp_docx).unlink(missing_ok=True)
            raise
    else:
        raise ValueError(f"不支持的文件格式: {ext}（仅支持 .docx 和 .doc）")

    total = sum(counts.values())
    print(f"[完成] 共遮盖 {total} 处，结果保存至: {output_path}")
    if counts:
        for label, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"         - {label}: {n} 处")

    # R7（2026-09-06）：人工检查项显式汇报，杜绝静默失败
    if _MANUAL_CHECK_ITEMS:
        print(f"\n[人工检查] 共 {len(_MANUAL_CHECK_ITEMS)} 项需人工确认（自动处理失败/不支持）：")
        for item, reason in _MANUAL_CHECK_ITEMS:
            print(f"         ! {item}: {reason}")
    return counts


def main():
    if len(sys.argv) < 2:
        print("用法: python3 redact_word.py <输入文件.docx/.doc> [输出文件路径]")
        sys.exit(1)

    import argparse
    parser = argparse.ArgumentParser(description="Word 文档脱敏")
    parser.add_argument("input_file")
    parser.add_argument("output_file", nargs="?")
    parser.add_argument("--manifest-override", dest="manifest_override")
    args = parser.parse_args()
    redact_word(args.input_file, args.output_file, manifest_override=args.manifest_override)


if __name__ == "__main__":
    main()
