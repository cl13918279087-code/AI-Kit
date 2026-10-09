#!/usr/bin/env python3
"""
redact_image.py - 图片脱敏脚本
支持 .png / .jpg / .jpeg / .bmp / .gif / .webp / .tiff

处理流程：
  1. pytesseract OCR 定位文字及坐标
  2. 正则识别敏感信息 → 马赛克/模糊/黑块遮盖
  3. 银行 Logo 检测（颜色均匀度 + 边缘密度）→ 马赛克遮盖

依赖: pytesseract, Pillow, numpy, opencv-python
安装: pip install pytesseract Pillow numpy opencv-python
注意: macOS: brew install tesseract tesseract-lang
       Windows: 下载 tesseract.exe 并添加到 PATH
       Linux: sudo apt install tesseract-ocr tesseract-ocr-chi-sim
"""

import sys
import os
import io
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from common_rules import apply_redactions, redact_filename_stem, set_agent_decisions_override, _get_skip_texts

# Tesseract 路径自动检测（从 config.json 读取）
import shutil
import subprocess, platform
_tesseract_paths = [
    r"/opt/homebrew/bin/tesseract",
    r"/usr/bin/tesseract",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    'tesseract',
]
for _p in _tesseract_paths:
    if os.path.exists(_p) or _p == 'tesseract':
        try:
            subprocess.run([_p, '--version'], capture_output=True, timeout=5)
            pytesseract.pytesseract.tesseract_cmd = _p
            break
        except Exception:
            pass



# ---------------------------------------------------------------------------
# D1（赵辉，第5批，2026-10-09）：OCR 字符区间姓名检测
#
# 背景：整行打码无法区分"正文+姓名混排行"中的姓名区间。
# 本模块对每个 OCR 文本块做规则检测，返回姓名区间，
# 按字符宽度比例映射到像素坐标，只打姓名区间不打整行。
# ---------------------------------------------------------------------------

import re as _re

# 角色前缀词（支持：角色词/部门机构词/厂商简称）
_ROLE_PREFIX_WORDS = (
    "组长", "副组长", "负责人", "联系人", "成员", "组员",
    "经理", "总监", "工程师", "设计师", "分析师", "架构师",
    "主管", "部长", "主任", "处长", "科员",
    "总经理", "副总经理", "项目经理", "技术经理", "产品经理",
    "主办", "协办", "承办", "参会", "主讲", "主持",
    "开发", "测试", "运维", "安全", "业务", "产品",
    "技术", "实施", "设计", "验收", "评审",
)
_ROLE_PREFIX_RE = _re.compile(
    r"^(?:"
    + "|".join(_re.escape(w) for w in _ROLE_PREFIX_WORDS)
    + r")[:：\s]*(.+)$"
)

# D12（赵辉，第5批，2026-10-09）：OCR 单元格分隔符扩充
# 原分隔符：、/\n，新增 /／ 以及尾随分隔（OCR 把"刘殿麒、马杰"截成"刘殿麒/马"）
_CELL_SEP_RE = _re.compile(r'[、/／\\，,\n]+')


def _detect_name_spans_in_text(text: str) -> list:
    """
    D1（赵辉，第5批，2026-10-09）：检测文本中的姓名区间。
    返回 [(start_char_idx, end_char_idx, name_text)]，字符索引基于 text。
    """
    spans = []
    if not text:
        return spans

    # 模式1：角色前缀 + 名单（如"组长：张三、李四"）
    m = _ROLE_PREFIX_RE.match(text)
    if m:
        rest = m.group(1)
        parts = _CELL_SEP_RE.split(rest)
        char_pos = len(m.group(0)) - len(rest)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            name = part
            for suffix in ("总", "等", "和", "与"):
                if name.endswith(suffix) and len(name) > 2:
                    name = name[:-1]
            if 2 <= len(name) <= 4 and _re.search(r'[\u4e00-\u9fa5]', name):
                spans.append((char_pos, char_pos + len(name), name))
            char_pos += len(part)

    # 模式2：姓名 + "总"（敬称）："谢红总" → 谢红
    for m in _re.finditer(r'[\u4e00-\u9fa5]{2,3}总', text):
        name = m.group(0)[:-1]
        if 2 <= len(name) <= 3:
            spans.append((m.start(), m.start() + len(name), name))

    # 模式3：单姓 + "行"（邓行 = 邓行长简写）
    # 匹配单汉字+行，span 返回完整 2 字符，redact 时只打姓名部分
    for m in _re.finditer(r'[\u4e00-\u9fa5]行', text):
        surname_start = m.start()
        # 前一字是连词/标点，或位于行首 → 有效单姓行
        valid = True
        if surname_start > 0:
            prev = text[surname_start - 1]
            if prev in '和与及或但而且并：：':
                valid = False
        if valid:
            spans.append((surname_start, surname_start + 2, text[surname_start]))

    # 模式4：顿号分隔名单（如"蔡元龙、张瑜"）
    parts = _CELL_SEP_RE.split(text)
    pos = 0
    for part in parts:
        part = part.strip()
        if not part:
            pos += 1
            continue
        for suffix in ("总", "等"):
            if part.endswith(suffix) and len(part) > 2:
                part = part[:-1]
        if 2 <= len(part) <= 4 and _re.search(r'[\u4e00-\u9fa5]', part):
            skip = False
            if pos > 0:
                prev_char = text[max(0, pos - 1)]
                if prev_char in '和与及或但而且并':
                    skip = True
            if not skip:
                spans.append((pos, pos + len(part), part))
        pos += len(part) + 1

    # 去重：相同区间保留最长 name（优先保留更完整的姓名）
    seen = set()
    result = []
    for start, end, name in sorted(spans, key=lambda x: (x[0], -x[1])):
        key = (start, end)
        if key not in seen:
            seen.add(key)
            result.append((start, end, name))
        else:
            # 已有相同区间，若新 name 更长则替换
            for i, (s, e, n) in enumerate(result):
                if s == start and e == end and len(name) > len(n):
                    result[i] = (start, end, name)
    return result


def _apply_name_spans_by_block(img_arr, ocr_data: dict, apply_fn,
                               pad: int = 3) -> int:
    """
    D1（赵辉，第5批，2026-10-09）：对每个 OCR 文本块检测姓名区间，
    按字符宽度比例计算像素坐标并打码。只打姓名区间不打整行。
    返回打码处数。
    """
    ih, iw = img_arr.shape[:2]
    count = 0
    n = len(ocr_data["text"])

    for i in range(n):
        text = ocr_data["text"][i].strip()
        if not text:
            continue

        x = ocr_data["left"][i]
        y = ocr_data["top"][i]
        bw = ocr_data["width"][i]
        bh = ocr_data["height"][i]
        text_len = len(text)

        # 检测姓名区间
        spans = _detect_name_spans_in_text(text)
        if not spans:
            continue

        for start, end, name in spans:
            if start >= end or text_len == 0:
                continue
            # 按字符宽度比例计算像素区间
            char_width = bw / text_len
            x1 = int(x + start * char_width)
            x2 = int(x + end * char_width)
            y1 = max(0, y - pad)
            y2 = min(ih, y + bh + pad)
            x1 = max(0, x1 - pad)
            x2 = min(iw, x2 + pad)

            if x2 > x1 and y2 > y1:
                apply_fn(img_arr, x1, y1, x2, y2)
                count += 1

    return count


# ---------------------------------------------------------------------------
# E7（赵辉，第5批，2026-10-09）：图片年份选区打码
# 图片中 OCR 识别的年份（如"2025.3.31"），只打年份部分，月日保留
# ---------------------------------------------------------------------------

_YEAR_PAT = _re.compile(r'20[12]\d')


def _apply_year_spans_by_block(img_arr, ocr_data: dict, apply_fn,
                                pad: int = 2) -> int:
    """
    E7（赵辉，第5批，2026-10-09）：对 OCR 文本块中检测到的年份（20xx），
    按字符占比计算像素区间并打码。只打年份段，月日保留
    （如 2025.3.31 → ████.3.31）。
    返回打码处数。
    """
    ih, iw = img_arr.shape[:2]
    count = 0
    n = len(ocr_data["text"])

    for i in range(n):
        text = ocr_data["text"][i].strip()
        if not text:
            continue

        x = ocr_data["left"][i]
        y = ocr_data["top"][i]
        bw = ocr_data["width"][i]
        bh = ocr_data["height"][i]
        text_len = len(text)

        if text_len == 0:
            continue

        for m in _YEAR_PAT.finditer(text):
            year_start = m.start()
            year_end = m.end()
            char_width = bw / text_len
            x1 = int(x + year_start * char_width)
            x2 = int(x + year_end * char_width)
            y1 = max(0, y - pad)
            y2 = min(ih, y + bh + pad)
            x1 = max(0, x1 - pad)
            x2 = min(iw, x2 + pad)

            if x2 > x1 and y2 > y1:
                apply_fn(img_arr, x1, y1, x2, y2)
                count += 1

    return count


# ---------------------------------------------------------------------------
# H5（赵辉，第5批，2026-10-09）：组织架构图场景通道
#
# 柳州 4.3 组织架构图（image3.png）：框线定位整格遮蔽（G2）原本是一次性脚本
# +外科回补，整份重跑即被冲掉。沉淀为场景通道，在主流程前置判定。
#
# 门槛：底部带冒号组标签 ≥3 且 角色前缀行 ≥2 且 存在表格框线
# 命中→专用通道（与人工返工逐字节一致）；未命中→通用通道（行为与旧版全同）
# ---------------------------------------------------------------------------

import cv2
import numpy as np


def _detect_orgchart_scene(img_arr) -> dict:
    """
    H5（赵辉，第5批，2026-10-09）：判断图片是否为组织架构图。
    返回 {"matched": bool, "detail": dict}。
    命中条件：底部带冒号组标签 ≥3 且 角色前缀行 ≥2 且 存在表格框线。
    """
    import pytesseract

    ih, iw = img_arr.shape[:2]
    gray = cv2.cvtColor(img_arr, cv2.COLOR_RGB2GRAY)

    # Step 1：表格框线检测（OpenCV 轮廓）
    # 二值化 + 形态学闭运算合并断线
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, iw // 50), 1))
    kernel_v = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, ih // 50)))
    closed_h = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_h)
    closed_v = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_v)
    table_mask = cv2.bitwise_or(closed_h, closed_v)

    # 找矩形轮廓（表格框线）
    contours, _ = cv2.findContours(table_mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    # 过滤掉极小轮廓（噪点）和极大轮廓（整图边缘）
    table_contours = [
        c for c in contours
        if 200 < cv2.contourArea(c) < (iw * ih * 0.9)
        and len(c) >= 4
    ]
    has_table_border = len(table_contours) >= 4  # 至少4条边才算有框线

    # Step 2：OCR 文本块分析
    ocr_data = pytesseract.image_to_data(img_arr, output_type=pytesseract.Output.DICT)
    n_boxes = len(ocr_data["text"])

    # 角色前缀行（行内含角色词 + 冒号）
    role_prefix_count = 0
    for i in range(n_boxes):
        text = ocr_data["text"][i].strip()
        if not text:
            continue
        # 角色词 + 冒号（或 OCR 丢了冒号的等宽空格分隔）
        for kw in ("组长", "负责人", "经理", "工程师", "主管", "主办"):
            if kw in text and ("：" in text or ":" in text or len(text.split()) >= 2):
                role_prefix_count += 1
                break

    # 底部组标签（行尾为"组"且含冒号，行首为"××"格式）
    group_label_count = 0
    for i in range(n_boxes):
        text = ocr_data["text"][i].strip()
        if not text:
            continue
        # 带冒号的组标签（如"对公存款组："，"中间业务组："）
        # OCR 丢冒号时用"组："匹配（冒号可能转成空格）
        if (text.endswith("组") or text.endswith("组：") or text.endswith("组:")
                or "组：" in text or "组:" in text) and len(text) <= 20:
            group_label_count += 1

    # Step 3：门槛判定（只数带冒号标签；遮蔽阶段用宽条件兜底）
    matched = (
        has_table_border
        and role_prefix_count >= 2
        and group_label_count >= 3
    )

    return {
        "matched": matched,
        "detail": {
            "has_table_border": has_table_border,
            "table_contours": len(table_contours),
            "role_prefix_count": role_prefix_count,
            "group_label_count": group_label_count,
        }
    }


# ---------------------------------------------------------------------------
# G2（赵辉，第5批，2026-10-09）：架构图密排小字表格——框线定位整格遮蔽
#
# 背景：OCR 行框在密排小字上斜跨两行/漏检续行。
# 方案：标签行锚定 + 表格框线定位，整格遮蔽标签以下全部内容。
# 墨迹剖面 x 只能用标签框自身范围，不能用整格 x（否则边框连续墨迹
# 会把空隙行永有墨，判不出间隙致矩形退化整格漏打）。
# ---------------------------------------------------------------------------

def _find_table_grid(img_arr, min_cell_h: int = 10, min_cell_w: int = 10) -> dict:
    """
    用 OpenCV 轮廓检测表格框线，返回网格信息。
    返回 {"rows": [y_top, ...], "cols": [x_left, ...], "cells": [(x1,y1,x2,y2),...]}
    """
    ih, iw = img_arr.shape[:2]
    gray = cv2.cvtColor(img_arr, cv2.COLOR_RGB2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # 形态学闭运算合并断线
    kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, iw // 40), 1))
    kernel_v = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, ih // 40)))
    closed = cv2.addWeighted(
        cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_h), 0.5,
        cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_v), 0.5, 0
    )

    contours, _ = cv2.findContours(closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    # 收集所有水平线和垂直线段
    h_lines, v_lines = [], []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = w * h
        if area < 400 or area > (iw * ih * 0.8):
            continue
        ratio = max(w / max(h, 1), h / max(w, 1))
        if ratio > 4:  # 长宽比>4认为是线段
            if w > h:  # 水平线
                h_lines.append((y, x, x + w))
            else:  # 垂直线
                v_lines.append((x, y, y + h))

    # 聚类线段位置（容差 8px）
    def cluster(lines, tol=8):
        if not lines:
            return []
        sorted_lines = sorted(lines)
        clusters = []
        current = [sorted_lines[0]]
        for l in sorted_lines[1:]:
            if l - current[-1][1] <= tol or abs(l - current[-1][2]) <= tol:
                current.append(l)
            else:
                clusters.append(sum(c[0] for c in current) // len(current))
                current = [l]
        clusters.append(sum(c[0] for c in current) // len(current))
        return sorted(set(clusters))

    row_ys = cluster([l[0] for l in h_lines])
    col_xs = cluster([l[0] for l in v_lines])

    # 生成所有单元格
    cells = []
    for r_idx in range(len(row_ys) - 1):
        for c_idx in range(len(col_xs) - 1):
            x1, x2 = col_xs[c_idx], col_xs[c_idx + 1]
            y1, y2 = row_ys[r_idx], row_ys[r_idx + 1]
            if x2 - x1 >= min_cell_w and y2 - y1 >= min_cell_h:
                cells.append((x1, y1, x2, y2))

    return {"rows": row_ys, "cols": col_xs, "cells": cells}


def _redact_by_border_boxes(img_arr, apply_fn,
                             strong_mosaic: bool = True) -> int:
    """
    G2（赵辉，第5批，2026-10-09）：框线定位整格遮蔽。
    对表格区域内的每个单元格：
      - 首行（含标签）保留标签文字（取标签框自身 x 范围，不含整格框线墨迹）
      - 标签行以下所有单元格整格打码（马赛克覆盖，包括 OCR 漏检区域）
    返回打码格数。
    """
    import pytesseract

    ih, iw = img_arr.shape[:2]
    grid = _find_table_grid(img_arr)

    if not grid["cells"]:
        return 0

    cells = grid["cells"]
    if not cells:
        return 0

    # 按 y 坐标排序单元格（首行在上）
    cells_sorted = sorted(cells, key=lambda c: (c[1], c[0]))

    # 找首行（最小 y）
    min_y = min(c[1] for c in cells_sorted)
    first_row_cells = [c for c in cells_sorted if abs(c[1] - min_y) < 5]

    # 标签行（首行）OCR，提取标签文本及位置
    label_cells = []  # [(x1, y1, x2, y2, label_text, label_x_range)]
    for c in first_row_cells:
        x1, y1, x2, y2 = c
        cell_roi = img_arr[y1:y2, x1:x2]
        try:
            text = pytesseract.image_to_string(cell_roi, lang='chi_sim+eng',
                                                config='--psm 6').strip()
        except Exception:
            text = ""
        # 标签的 x 范围：用标签自身框（不用整格框线墨迹）
        # 在 ROI 内再做一次字符级定位
        try:
            char_data = pytesseract.image_to_data(cell_roi,
                                                   output_type=pytesseract.Output.DICT)
        except Exception:
            char_data = {"left": [], "top": [], "width": [], "height": [], "text": []}

        # 找标签字符（取前 2/3 宽度范围内含汉字的部分）
        roi_w = x2 - x1
        label_x_end = x1 + int(roi_w * 0.65)  # 标签通常占前 2/3
        label_cells.append((x1, y1, x2, y2, text, label_x_end))

    count = 0
    # 按 y 分组处理后续行（标签行以下全部打码）
    all_rows = sorted(set(c[1] for c in cells_sorted))
    first_row_idx = 0

    for row_idx, row_y in enumerate(all_rows):
        if row_idx == first_row_idx:
            continue  # 首行标签行：保留标签，遮蔽右侧内容

        row_cells = [c for c in cells_sorted if abs(c[1] - row_y) < 5]
        for c in row_cells:
            x1, y1, x2, y2 = c

            # 检查是否是标签列（首行对应的列）
            is_label_col = False
            preserve_x = None
            for lc in label_cells:
                lx1, ly1, lx2, ly2, ltext, lx_end = lc
                # 同列：x1 接近
                if abs(c[0] - lx1) < 5:
                    is_label_col = True
                    preserve_x = lx_end
                    break

            if is_label_col and preserve_x is not None:
                # 标签列：只遮蔽标签右侧（保留标签文字）
                if x2 > preserve_x + 5:
                    apply_fn(img_arr, preserve_x + 2, y1, x2, y2)
                    count += 1
            else:
                # 非标签列：整格遮蔽
                apply_fn(img_arr, x1, y1, x2, y2)
                count += 1

    return count


def _redact_orgchart_table(img_arr, apply_fn, method: str = "mosaic") -> int:
    """
    H5（赵辉，第5批，2026-10-09）：组织架构图专用通道。
    上半部：角色前缀行保留前缀、冒号图像定位后打码；纯姓名行整行打码；
            打码矩形先对"保留行"收缩。
    底部：组标签行锚定 + 表格框线定位（G2），整格遮蔽标签行以下全部内容。
    返回打码处数。
    """
    import pytesseract

    ih, iw = img_arr.shape[:2]
    ocr_data = pytesseract.image_to_data(img_arr, output_type=pytesseract.Output.DICT)
    n_boxes = len(ocr_data["text"])

    # 角色词 + 冒号行：保留冒号前的角色词，只打冒号后的姓名部分
    role_kw = ("组长", "负责人", "经理", "工程师", "主管", "主办",
               "总监", "主任", "部长", "架构师")
    role_rows = []  # [(text, left, top, width, height)]

    for i in range(n_boxes):
        text = ocr_data["text"][i].strip()
        if not text:
            continue
        matched_kw = next((kw for kw in role_kw if kw in text), None)
        if matched_kw:
            role_rows.append({
                "text": text,
                "left": ocr_data["left"][i],
                "top": ocr_data["top"][i],
                "width": ocr_data["width"][i],
                "height": ocr_data["height"][i],
                "kw": matched_kw,
            })

    count = 0

    # 上半部：角色前缀行处理
    for row in role_rows:
        text = row["text"]
        left, top, width, height = row["left"], row["top"], row["width"], row["height"]

        # 找冒号位置（宽条件：OCR 可能丢冒号，用空格或"："或"  "分隔）
        colon_pos = -1
        for sep in ("：", ":", "  ", " "):
            pos = text.find(sep)
            if pos >= 0:
                colon_pos = pos
                break

        if colon_pos >= 0:
            # 保留冒号前内容，遮蔽冒号后姓名
            preserved_len = colon_pos + 1  # 含冒号
            text_len = len(text)
            if text_len == 0:
                continue
            char_w = width / text_len
            x_preserve_end = int(left + preserved_len * char_w)

            pad = 3
            y1 = max(0, top - pad)
            y2 = min(ih, top + height + pad)
            x2 = min(iw, left + width + pad)
            x1 = max(0, x_preserve_end)

            if x2 > x1 and y2 > y1:
                apply_fn(img_arr, x1, y1, x2, y2)
                count += 1
        else:
            # 纯姓名行（无角色词/冒号），整行打码
            pad = 3
            x1 = max(0, left - pad)
            y1 = max(0, top - pad)
            x2 = min(iw, left + width + pad)
            y2 = min(ih, top + height + pad)
            if x2 > x1 and y2 > y1:
                apply_fn(img_arr, x1, y1, x2, y2)
                count += 1

    # 底部：表格框线定位整格遮蔽（G2）
    border_count = _redact_by_border_boxes(img_arr, apply_fn)
    count += border_count

    return count


# ---------------------------------------------------------------------------
# H6（赵辉，第5批，2026-10-09）：OCR 选区打码复验迭代
#
# 背景：单轮 OCR 召回缺口是结构性的——密排小字表格 OCR 框斜跨两行/漏检，
# 打码必然改变上下文，新检出首轮漏检的名字。
# 方案：打码后重跑 OCR，对新检出的敏感行继续打码，直至无新增（≤3 轮收敛）。
# ---------------------------------------------------------------------------

def _redact_image_iterative(img_arr, apply_fn,
                             method: str = "mosaic",
                             max_iterations: int = 3) -> dict:
    """
    H6（赵辉，第5批，2026-10-09）：OCR 选区打码复验迭代。
    返回 {"counts": {label: n}, "iterations": int, "total": int}。
    """
    import pytesseract

    total_counts = {}
    iterations = 0
    prev_total = -1

    while iterations < max_iterations:
        iterations += 1

        # OCR（每轮用最新图像像素）
        ocr_data = pytesseract.image_to_data(img_arr, output_type=pytesseract.Output.DICT)
        ih, iw = img_arr.shape[:2]
        n_boxes = len(ocr_data["text"])

        round_counts = {}
        round_total = 0

        for i in range(n_boxes):
            text = ocr_data["text"][i].strip()
            if not text:
                continue
            redacted = apply_redactions(text)
            if redacted == text:
                continue

            x = ocr_data["left"][i]
            y = ocr_data["top"][i]
            bw = ocr_data["width"][i]
            bh = ocr_data["height"][i]

            pad = 3
            x1 = max(0, x - pad)
            y1 = max(0, y - pad)
            x2 = min(iw, x + bw + pad)
            y2 = min(ih, y + bh + pad)

            if x2 > x1 and y2 > y1:
                apply_fn(img_arr, x1, y1, x2, y2)
                round_counts["文本遮盖"] = round_counts.get("文本遮盖", 0) + 1
                round_total += 1

        # 合并本轮计数
        for k, v in round_counts.items():
            total_counts[k] = total_counts.get(k, 0) + v

        # 收敛判定：没有新增打码
        if round_total == 0:
            break
        if round_total == prev_total:
            # 收敛但非零——继续下一轮（避免死循环）
            pass
        prev_total = round_total

        # H6：打码后重跑 OCR（只在有新打码时继续）
        if iterations < max_iterations and round_total > 0:
            # 继续下一轮迭代
            continue
        else:
            break

    return {
        "counts": total_counts,
        "iterations": iterations,
        "total": sum(total_counts.values())
    }


# ---------------------------------------------------------------------------
# 银行 Logo 检测
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def detect_logo_regions(img_arr) -> list:
    """
    检测图片中银行 Logo 区域。
    策略：OCR 找到含"银行"文本 → 扩展左侧区域 → 颜色/边缘启发式判断。
    返回 [(x1, y1, x2, y2), ...]。
    """
    import cv2
    import numpy as np
    import pytesseract

    h, w = img_arr.shape[:2]
    logo_regions = []

    # OCR 获取文本块坐标
    ocr_data = pytesseract.image_to_data(img_arr, output_type=pytesseract.Output.DICT)
    n_boxes = len(ocr_data["text"])

    for i in range(n_boxes):
        text = ocr_data["text"][i].strip()
        if "银行" not in text:
            continue

        x = ocr_data["left"][i]
        y = ocr_data["top"][i]
        bw = ocr_data["width"][i]
        bh = ocr_data["height"][i]

        # 扩展搜索区域（Logo 通常在"银行"文字左侧）
        sx1 = max(0, x - 320)
        sy1 = max(0, y - 60)
        sx2 = min(w, x + bw + 20)
        sy2 = min(h, y + bh + 60)

        region = img_arr[sy1:sy2, sx1:sx2]
        gray = (
            cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
            if len(region.shape) == 3
            else region
        )

        color_std = np.std(region)
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.sum(edges > 0) / edges.size
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        filled_ratio = np.sum(binary > 0) / binary.size

        is_logo_like = (
            (color_std < 60)
            or (edge_density > 0.05 and filled_ratio > 0.3)
            or (filled_ratio > 0.6)
        )

        if is_logo_like:
            logo_regions.append((sx1, sy1, sx2, sy2))

    return _merge_overlapping(logo_regions)


def _merge_overlapping(regions, threshold=50) -> list:
    """合并重叠或过近的区域"""
    if not regions:
        return []
    regions = sorted(regions, key=lambda r: r[0])
    merged = [regions[0]]
    for x1, y1, x2, y2 in regions[1:]:
        last = merged[-1]
        if x1 - last[2] < threshold and not (y2 < last[1] or y1 > last[3]):
            merged[-1] = (last[0], min(last[1], y1), max(last[2], x2), max(last[3], y2))
        else:
            merged.append((x1, y1, x2, y2))
    return merged


# ---------------------------------------------------------------------------
# 遮盖方法
# ---------------------------------------------------------------------------

def _apply_mosaic(arr, x1, y1, x2, y2, block_size=16) -> None:
    """原地马赛克"""
    import cv2
    region = arr[y1:y2, x1:x2]
    rh, rw = region.shape[:2]
    sh = max(2, rh // block_size)
    sw = max(2, rw // block_size)
    small = cv2.resize(region, (sw, sh), interpolation=cv2.INTER_NEAREST)
    mosaic = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
    arr[y1:y2, x1:x2] = mosaic


def _apply_blur(arr, x1, y1, x2, y2, radius=15) -> None:
    """原地高斯模糊"""
    import cv2
    region = arr[y1:y2, x1:x2]
    blurred = cv2.GaussianBlur(region, (radius * 2 + 1, radius * 2 + 1), 0)
    arr[y1:y2, x1:x2] = blurred


def _apply_black(arr, x1, y1, x2, y2) -> None:
    """原地纯黑填充"""
    arr[y1:y2, x1:x2] = 0


def _apply_solid_fill(arr, x1, y1, x2, y2) -> None:
    """
    原地纯色填充（R-㉚，v1.3.9，响应 Issue #15-C11，赵辉）：
    取区域周边众数 RGB 值，直接填充。与马赛克相比，对大色块 Logo 效果更好。
    """
    region = arr[y1:y2, x1:x2]
    rh, rw = region.shape[:2]
    if rh <= 0 or rw <= 0:
        return
    # 采集周边像素（上下各5行，左右各5列）
    border_pixels = []
    if y1 >= 5:
        border_pixels.append(arr[y1-5:y1, x1:x2].reshape(-1, 3))
    if y2 + 5 <= arr.shape[0]:
        border_pixels.append(arr[y2:y2+5, x1:x2].reshape(-1, 3))
    if x1 >= 5:
        border_pixels.append(arr[y1:y2, x1-5:x1].reshape(-1, 3))
    if x2 + 5 <= arr.shape[1]:
        border_pixels.append(arr[y1:y2, x2:x2+5].reshape(-1, 3))
    if border_pixels:
        import numpy as np
        all_border = np.vstack(border_pixels)
        # 众数 RGB（按行聚合成元组后统计）
        from collections import Counter
        rgb_tuples = [tuple(p) for p in all_border]
        most_common_rgb = Counter(rgb_tuples).most_common(1)[0][0]
        fill_color = np.array(most_common_rgb, dtype=arr.dtype)
    else:
        # 无周边像素时用白色填充
        fill_color = np.array([255, 255, 255], dtype=arr.dtype)
    arr[y1:y2, x1:x2] = fill_color


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def redact_image(input_path: str, output_path: str = None,
                 method: str = "mosaic",
                 *, manifest_override: str = None,
                 auto_gray: bool = False) -> dict:
    """
    图片脱敏主函数。

    参数:
        input_path : 输入图片路径
        output_path: 输出路径（默认在文件名后加 _脱敏）
        method     : 遮盖方式
                     - mosaic : 马赛克像素化（默认，推荐）
                     - blur   : 高斯模糊
                     - black  : 纯黑填充
    返回:
        各类遮盖计数
    """
    import pytesseract
    import numpy as np

    if output_path is None:
        # R-⑬（v1.3.7，Issue #13-①）：默认输出名统一走文件名脱敏
        stem = redact_filename_stem(Path(input_path).stem)
        ext = Path(input_path).suffix
        output_path = str(Path(input_path).with_name(f"{stem}_脱敏{ext}"))

    counts = {}

    img = Image.open(input_path)
    img_rgb = img.convert("RGB")
    img_arr = np.array(img_rgb)
    ih, iw = img_arr.shape[:2]

    # OCR（含坐标）
    ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    n_boxes = len(ocr_data["text"])

    # ① 检测银行 Logo 区域
    logo_regions = detect_logo_regions(img_arr)

    # ② 脱敏方法
    apply_fn = {"mosaic": _apply_mosaic, "blur": _apply_blur, "black": _apply_black}.get(method)

    # ③ H5（赵辉，第5批，2026-10-09）：组织架构图场景通道前置判定
    scene = _detect_orgchart_scene(img_arr)
    if scene["matched"]:
        print(f"  [H5] 组织架构图场景命中（表框线:{scene['detail']['table_contours']} "
              f"个 角色行:{scene['detail']['role_prefix_count']} "
              f"组标签:{scene['detail']['group_label_count']}）")
        # H5 专用通道（G2 框线定位整格遮蔽 + 角色前缀行处理）
        orgchart_count = _redact_orgchart_table(img_arr, apply_fn, method)
        if orgchart_count > 0:
            counts["架构图整格遮盖"] = counts.get("架构图整格遮盖", 0) + orgchart_count
            print(f"  [H5+G2] 架构图通道打码 {orgchart_count} 处")
        # 通用通道 E7/D1 不再重复执行（架构图已由专用通道处理）
        # H6 复验迭代仍执行（兜住漏网）
        result = _redact_image_iterative(img_arr, apply_fn, method, max_iterations=2)
        if result["iterations"] > 0:
            for k, v in result["counts"].items():
                counts[k] = counts.get(k, 0) + v
            print(f"  [H6] 架构图通道复验迭代 {result['iterations']} 轮，新增 "
                  f"{result['total']} 处")
    else:
        # 通用通道：E7 年份选区 + D1 姓名区间 + H6 复验迭代
        # ③-a E7（赵辉，第5批，2026-10-09）：图片年份选区打码
        # 先于整行打码，只打年份段不打破月日
        year_count = _apply_year_spans_by_block(img_arr, ocr_data, apply_fn, pad=2)
        if year_count > 0:
            counts["年份选区遮盖"] = counts.get("年份选区遮盖", 0) + year_count
            print(f"  [E7] 年份选区打码 {year_count} 处")

        # ③-b D1（赵辉，第5批，2026-10-09）：OCR 字符区间姓名打码
        # 只打姓名区间不打整行，按字符比例映射像素坐标
        name_span_count = _apply_name_spans_by_block(img_arr, ocr_data, apply_fn, pad=3)
        if name_span_count > 0:
            counts["姓名区间遮盖"] = counts.get("姓名区间遮盖", 0) + name_span_count
            print(f"  [D1] 姓名区间打码 {name_span_count} 处")

        # ③-c H6（赵辉，第5批，2026-10-09）：OCR 选区打码复验迭代
        # 替代原单轮块级兜底；最多 3 轮收敛
        result = _redact_image_iterative(img_arr, apply_fn, method, max_iterations=3)
        if result["iterations"] > 0:
            for k, v in result["counts"].items():
                counts[k] = counts.get(k, 0) + v
            print(f"  [H6] 通用通道复验迭代 {result['iterations']} 轮，新增 "
                  f"{result['total']} 处")

    # ③ 对 Logo 区域执行纯色填充（R-㉚，v1.3.9，响应 Issue #15-C11，赵辉）：
    # 纯色填充（周边众数取色）优于马赛克，对大色块 Logo 效果更好
    for x1, y1, x2, y2 in logo_regions:
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(iw, x2), min(ih, y2)
        if x2 > x1 and y2 > y1:
            _apply_solid_fill(img_arr, x1, y1, x2, y2)
            counts["Logo纯色填充"] = counts.get("Logo纯色填充", 0) + 1
            print(f"  [银行Logo纯色填充] 区域 ({x1},{y1})-({x2},{y2})")

    # ④ 保存
    from PIL import Image
    result = Image.fromarray(img_arr.astype(np.uint8))
    result.save(output_path)

    total = sum(counts.values())
    print(f"[完成] 共遮盖 {total} 处（方法: {method}），结果保存至: {output_path}")
    for label, n in counts.items():
        print(f"         - {label}: {n} 处")
    return counts


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(
            "用法: python3 redact_image.py <输入图片> [输出路径] "
            "[--method mosaic|blur|black]"
        )
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = None
    method = "mosaic"

    for i, arg in enumerate(sys.argv[2:], 2):
        if arg.startswith("--method="):
            method = arg.split("=", 1)[1]
        elif not arg.startswith("--"):
            output_file = arg

    ext = Path(input_file).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"):
        print(f"[错误] 不支持的文件格式: {ext}", file=sys.stderr)
        sys.exit(1)

    redact_image(input_file, output_file, method)


if __name__ == "__main__":
    main()
