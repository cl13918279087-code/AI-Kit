#!/usr/bin/env python3
"""
redact_pdf.py - PDF 文档脱敏脚本
支持文本型 PDF（提取文本坐标后遮盖）和扫描版 PDF（渲染为图片后 OCR + 遮盖）

处理策略：
  - 文本型 PDF：PyMuPDF 提取带坐标文本 → 绘制黑色矩形遮盖 + 写入占位符
  - 扫描版 PDF：PyMuPDF 渲染为高清图片 → pytesseract OCR + 坐标马赛克遮盖
  - 嵌入图片检测：提取页面图片流 → 银行 Logo 检测（颜色/边缘特征）→ 马赛克

依赖: pymupdf, pytesseract, Pillow, numpy, opencv-python
安装: pip install pymupdf pytesseract Pillow numpy opencv-python
注意: macOS 需安装 Tesseract: brew install tesseract tesseract-lang
"""

import sys
import io
import tempfile
import shutil
from pathlib import Path

from common_rules import apply_redactions

# ---------------------------------------------------------------------------
# PDF 类型检测
# ---------------------------------------------------------------------------

def detect_pdf_type(input_path: str) -> str:
    """
    判断 PDF 是文本型还是图片扫描版。
    策略：遍历所有页面，只要有任意一页包含可提取文本块，即为文本型。
    """
    import fitz  # PyMuPDF

    doc = fitz.open(input_path)
    try:
        for page in doc:
            blocks = page.get_text("dict").get("blocks", [])
            for block in blocks:
                if block.get("type") == 0 and block.get("lines"):
                    return "text"
    finally:
        doc.close()
    return "image_scan"


# ---------------------------------------------------------------------------
# 文本型 PDF 脱敏
# ---------------------------------------------------------------------------

def redact_pdf_text(input_path: str, output_path: str) -> dict:
    """
    处理文本型 PDF：遍历每个字符/单词的边界框，
    用黑色矩形遮盖敏感区域，并在原位写入占位符文本。
    """
    import fitz  # PyMuPDF
    import numpy as np

    counts = {}
    doc = fitz.open(input_path)

    for page_num in range(len(doc)):
        page = doc[page_num]
        # 获取带坐标的文本字典
        text_dict = page.get_text("dict")
        blocks = text_dict.get("blocks", [])

        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    original = span["text"]
                    redacted = apply_redactions(original)
                    if redacted == original:
                        continue

                    # 扩展边界 2px，防止边缘残留
                    bbox = fitz.Rect(span["bbox"])
                    bbox.x0 = max(0, bbox.x0 - 2)
                    bbox.y0 = max(0, bbox.y0 - 2)
                    bbox.x1 += 2
                    bbox.y1 += 2

                    # 黑色矩形遮盖
                    page.draw_rect(bbox, color=(0, 0, 0), fill=(0, 0, 0), overlay=True)

                    # 在遮盖区域上写入脱敏占位符
                    try:
                        font_size = max(span.get("size", 10) * 0.75, 6)
                        page.insert_text(
                            (bbox.x0 + 1, bbox.y1 - 2),
                            redacted,
                            fontsize=font_size,
                            color=(0.3, 0.3, 0.3),
                            overlay=True,
                        )
                    except Exception:
                        pass

                    _inc_count(counts, "文本遮盖")
                    _detect_and_redact_logo_pdf(page, bbox, original)

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    return counts


def _detect_and_redact_logo_pdf(page, text_bbox, text: str) -> None:
    """
    当文本含"银行"关键词时，检测其左侧区域是否存在 Logo 并马赛克遮盖。
    """
    if "银行" not in text:
        return

    import fitz  # PyMuPDF
    import numpy as np

    # 扩展搜索区域：文字左侧 300px
    logo_rect = fitz.Rect(
        max(0, text_bbox.x0 - 300),
        max(0, text_bbox.y0 - 30),
        text_bbox.x1,
        text_bbox.y1 + 30,
    )

    try:
        # 渲染该区域为 2x 图片
        clip = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=logo_rect)
        arr = np.frombuffer(clip.samples, dtype=np.uint8)
        h, w = clip.height, clip.width
        if clip.n == 4:
            arr = arr.reshape(h, w, 4)[:, :, :3]  # RGBA → RGB
        else:
            arr = arr.reshape(h, w, clip.n)[:, :, :3]

        logo_regions = _detect_logo_regions_cv2(arr)
        if logo_regions:
            print(f"  [PDF Logo马赛克] 检测到 {len(logo_regions)} 个区域")
            for (x1, y1, x2, y2) in logo_regions:
                px1, py1 = max(0, int(x1)), max(0, int(y1))
                px2, py2 = min(w, int(x2)), min(h, int(y2))
                if px2 > px1 and py2 > py1:
                    _apply_mosaic_cv2(arr, px1, py1, px2, py2)

            from PIL import Image
            buf = io.BytesIO()
            Image.fromarray(arr).save(buf, format="PNG")
            buf.seek(0)
            page.add_image(logo_rect, stream=buf.read(), overlay=True)
            _inc_count({}, "Logo马赛克")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 扫描版 PDF（图片型）脱敏
# ---------------------------------------------------------------------------

def redact_pdf_image(input_path: str, output_path: str) -> dict:
    """
    处理扫描版 PDF：将每页渲染为高清图片 → OCR → 坐标遮盖 → 替换页面。
    """
    import fitz  # PyMuPDF
    import pytesseract
    from PIL import Image
    import numpy as np

    counts = {}
    doc = fitz.open(input_path)

    for page_num in range(len(doc)):
        page = doc[page_num]
        pdf_rect = page.rect

        # 300 DPI 渲染（高清 OCR 需要）
        mat = fitz.Matrix(300 / 72, 300 / 72)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_arr = np.array(img)
        h, w = img_arr.shape[:2]

        # OCR（含坐标信息）
        ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

        # 检测 Logo 区域
        logo_regions = _detect_logo_regions_cv2(img_arr)
        for (x1, y1, x2, y2) in logo_regions:
            px1, py1 = max(0, int(x1)), max(0, int(y1))
            px2, py2 = min(w, int(x2)), min(h, int(y2))
            if px2 > px1 and py2 > py1:
                _apply_mosaic_cv2(img_arr, px1, py1, px2, py2)
                print(f"  [扫描PDF Logo马赛克] 页面{page_num+1}: 区域 ({px1},{py1})-({px2},{py2})")

        # 遍历 OCR 文本块，遮盖敏感区域
        n_boxes = len(ocr_data["text"])
        masked = False
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
            pad = 4

            px1, py1 = max(0, x - pad), max(0, y - pad)
            px2, py2 = min(w, x + bw + pad), min(h, y + bh + pad)
            if px2 > px1 and py2 > py1:
                _apply_mosaic_cv2(img_arr, px1, py1, px2, py2)
                masked = True
                _inc_count(counts, "OCR文本遮盖")

        # 写回页面
        buf = io.BytesIO()
        Image.fromarray(img_arr.astype(np.uint8)).save(buf, format="PNG")
        buf.seek(0)
        rect = fitz.Rect(0, 0, pdf_rect.width, pdf_rect.height)
        page.add_image(rect, stream=buf.read(), overlay=True)

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    return counts


# ---------------------------------------------------------------------------
# OpenCV 辅助（Logo 检测 + 马赛克）
# ---------------------------------------------------------------------------

def _detect_logo_regions_cv2(img_arr) -> list:
    """
    基于颜色方差/边缘密度检测可能是 Logo 的区域。
    返回 [(x1,y1,x2,y2), ...]。
    """
    import cv2
    import numpy as np

    h, w = img_arr.shape[:2]
    gray = cv2.cvtColor(img_arr, cv2.COLOR_RGB2GRAY)

    # 颜色方差（颜色均匀的区域可能是 Logo）
    color_std = np.std(img_arr)

    # 边缘密度（形状规整的可能是 Logo）
    edges = cv2.Canny(gray, 50, 150)
    edge_density = np.sum(edges > 0) / edges.size

    # 二值化
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    filled_ratio = np.sum(binary > 0) / binary.size

    is_logo = (
        (color_std < 50)
        or (edge_density > 0.05 and filled_ratio > 0.3)
        or (filled_ratio > 0.6)
    )

    if is_logo:
        return [(0, 0, w, h)]
    return []


def _apply_mosaic_cv2(arr, x1, y1, x2, y2, block_size=16) -> None:
    """对 arr[y1:y2, x1:x2] 区域应用马赛克（原地修改）"""
    import cv2
    import numpy as np

    region = arr[y1:y2, x1:x2]
    rh, rw = region.shape[:2]
    small_h = max(2, rh // block_size)
    small_w = max(2, rw // block_size)
    small = cv2.resize(region, (small_w, small_h), interpolation=cv2.INTER_NEAREST)
    mosaic = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
    arr[y1:y2, x1:x2] = mosaic


def _inc_count(counts: dict, key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def redact_pdf(input_path: str, output_path: str = None) -> dict:
    """自动检测 PDF 类型并执行相应脱敏策略"""
    if output_path is None:
        stem = Path(input_path).stem
        output_path = str(Path(input_path).with_name(f"{stem}_脱敏.pdf"))

    pdf_type = detect_pdf_type(input_path)
    print(f"[信息] PDF 类型: {'文本型' if pdf_type == 'text' else '扫描版（图片型）'}")

    if pdf_type == "text":
        counts = redact_pdf_text(input_path, output_path)
    else:
        counts = redact_pdf_image(input_path, output_path)

    total = sum(counts.values())
    print(f"[完成] 共遮盖 {total} 处，结果保存至: {output_path}")
    if counts:
        for label, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"         - {label}: {n} 处")
    return counts


def main():
    if len(sys.argv) < 2:
        print("用法: python3 redact_pdf.py <输入文件.pdf> [输出文件路径]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    redact_pdf(input_file, output_file)


if __name__ == "__main__":
    main()
