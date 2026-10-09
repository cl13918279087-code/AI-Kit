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
            y2 = min(iw, y + bh + pad)
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
            y2 = min(iw, y + bh + pad)
            x1 = max(0, x1 - pad)
            x2 = min(iw, x2 + pad)

            if x2 > x1 and y2 > y1:
                apply_fn(img_arr, x1, y1, x2, y2)
                count += 1

    return count


# ---------------------------------------------------------------------------
# 银行 Logo 检测
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

    # ②-a E7（赵辉，第5批，2026-10-09）：图片年份选区打码
    # 先于整行打码，只打年份段不打破月日
    year_count = _apply_year_spans_by_block(img_arr, ocr_data, apply_fn, pad=2)
    if year_count > 0:
        counts["年份选区遮盖"] = counts.get("年份选区遮盖", 0) + year_count
        print(f"  [E7] 年份选区打码 {year_count} 处")

    # ②-b D1（赵辉，第5批，2026-10-09）：OCR 字符区间姓名打码
    # 只打姓名区间不打整行，按字符比例映射像素坐标
    name_span_count = _apply_name_spans_by_block(img_arr, ocr_data, apply_fn, pad=3)
    if name_span_count > 0:
        counts["姓名区间遮盖"] = counts.get("姓名区间遮盖", 0) + name_span_count
        print(f"  [D1] 姓名区间打码 {name_span_count} 处")

    # ②-c 整行块级兜底（规则层应用整块文本，若块内容含敏感词则整块打码；
    # D1 已处理姓名区间场景，兜底覆盖其他类型敏感词）
    block_count = 0
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
            block_count += 1

    if block_count > 0:
        counts["文本遮盖"] = counts.get("文本遮盖", 0) + block_count

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
