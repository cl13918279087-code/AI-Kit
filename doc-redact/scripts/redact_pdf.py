#!/usr/bin/env python3
"""
redact_pdf.py - PDF 文档脱敏脚本
支持文本型 PDF 和扫描版（图片型）PDF
依赖: pymupdf, pytesseract, Pillow, numpy, opencv-python
安装: pip install pymupdf pytesseract Pillow numpy opencv-python
"""

import sys
import re
import io
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 脱敏规则（与 Word/PPT/Excel/图片 保持一致）
# ---------------------------------------------------------------------------
REPLACEMENTS = [
    (re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'), 'XXXXX@XXXXX'),
    (re.compile(
        r'[^\x00-\xFF]{2,6}(?:省|自治区|市)?[^\x00-\xFF]{0,10}'
        r'(?:市|区)?[^\x00-\xFF]{0,10}'
        r'(?:街|路|道|巷|弄|号|大道|大街|东路|西路|南路|北路)[^\x00-\xFF]{0,30}'
    ), 'XX省XX市XX区XXXX'),
    (re.compile(r'[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]'),
     'XXXXXXXXXXXXXXXXXX'),
    (re.compile(r'\b(?:\d{16}|\d{17}|\d{18}|\d{19})\b'), 'XXXXXXXXXXXXXXXX'),
    (re.compile(
        r'\d{4}[-年](?:0[1-9]|1[0-2])[-月](?:0[1-9]|[12]\d|3[01])[日]?\s*'
        r'|(?:19|20)\d{2}年\d{1,2}月\d{1,2}日'
    ), 'YYYY/MM/DD'),
    (re.compile(r'\b1[3-9]\d{9}\b'), 'XXXXXXXXXXX'),
    (re.compile(r'0\d{2,3}[-\s]?\d{7,8}'), '0XX-XXXXXXXX'),
    (re.compile(
        r'(?:(?:中国|交通|招商|浦发|兴业|民生|华夏|平安|光大|广发|浙商|渤海|恒丰|'
        r'南京|宁波|杭州|深圳|上海|北京|广州|农业|建设|工商|中国)银行|'
        r'(?:农信社|信用社|农商银行|合作银行|人民银行))'
    ), '[某银行]'),
    (re.compile(r'[\u4e00-\u9fa5]{2,4}(?![a-zA-Z0-9\u4e00-\u9fa5])'), 'XXX'),
]


def apply_redactions(text: str) -> str:
    if not text:
        return text
    result = text
    for pattern, replacement in REPLACEMENTS:
        result = pattern.sub(replacement, result)
    return result


# ---------------------------------------------------------------------------
# 马赛克辅助函数
# ---------------------------------------------------------------------------

def apply_mosaic_to_image(img_array, x1, y1, x2, y2, block_size=16):
    """对图像区域应用马赛克"""
    from PIL import Image
    import numpy as np
    region = img_array[y1:y2, x1:x2]
    pil_region = Image.fromarray(region.astype(np.uint8))
    h, w = pil_region.size[1], pil_region.size[0]
    small_h = max(2, h // block_size)
    small_w = max(2, w // block_size)
    small = pil_region.resize((small_h, small_w), Image.NEAREST)
    mosaic = small.resize((w, h), Image.NEAREST)
    img_array[y1:y2, x1:x2] = np.array(mosaic).astype(np.uint8)


def redact_pdf_text(input_path: str, output_path: str) -> None:
    """处理文本型 PDF：获取文本坐标 → 绘制黑色遮盖矩形 + 马赛克备选"""
    import fitz  # PyMuPDF
    import numpy as np
    from PIL import Image
    import cv2

    doc = fitz.open(input_path)
    redacted_count = 0

    for page_num in range(len(doc)):
        page = doc[page_num]
        blocks = page.get_text('dict')['blocks']
        for block in blocks:
            if block.get('type') != 0:
                continue
            for line in block.get('lines', []):
                for span in line.get('spans', []):
                    original_text = span['text']
                    redacted_text = apply_redactions(original_text)
                    if redacted_text == original_text:
                        continue

                    bbox = fitz.Rect(span['bbox'])
                    # 扩展边界
                    bbox.x0 = max(0, bbox.x0 - 2)
                    bbox.y0 = max(0, bbox.y0 - 2)
                    bbox.x1 += 2
                    bbox.y1 += 2

                    # 黑色矩形遮盖
                    page.draw_rect(bbox, color=(0, 0, 0), fill=(0, 0, 0), overlay=True)

                    # 在遮盖区域上写入脱敏占位符
                    try:
                        font_size = max(span.get('size', 10) * 0.75, 6)
                        page.insert_text(
                            (bbox.x0 + 1, bbox.y1 - 2),
                            redacted_text,
                            fontsize=font_size,
                            color=(0.3, 0.3, 0.3),
                            overlay=True
                        )
                    except Exception:
                        pass

                    redacted_count += 1

                    # 检测银行 Logo（文字含"银行"时）
                    if '银行' in original_text:
                        # 在文字左侧区域查找 Logo 并打马赛克
                        logo_bbox = fitz.Rect(
                            max(0, bbox.x0 - 350),
                            max(0, bbox.y0 - 20),
                            bbox.x1,
                            bbox.y1 + 20
                        )
                        page.add_freetext_annot(
                            logo_bbox,
                            "",
                            fill=(0, 0, 0),
                            fontname="helv",
                            fontsize=1
                        )
                        # 渲染该区域为图片，打马赛克后替换
                        try:
                            clip = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=logo_bbox)
                            clip_arr = np.frombuffer(clip.samples, dtype=np.uint8).reshape(
                                clip.height, clip.width, clip.n
                            )
                            if clip.n == 4:
                                clip_arr = clip_arr[:, :, :3]
                            x1 = 0
                            y1 = 0
                            x2 = clip.width
                            y2 = clip.height
                            apply_mosaic_to_image(clip_arr, x1, y1, x2, y2)
                            from PIL import Image as PILImage
                            import io
                            mosaic_bytes = io.BytesIO()
                            PILImage.fromarray(clip_arr).save(mosaic_bytes, format='PNG')
                            mosaic_bytes.seek(0)
                            page.add_image(logo_bbox, stream=mosaic_bytes.read(), overlay=True)
                        except Exception:
                            pass

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    print(f"[完成] 共遮盖 {redacted_count} 处敏感内容，结果保存至: {output_path}")


def redact_pdf_image_scan(input_path: str, output_path: str) -> None:
    """处理扫描版 PDF（纯图片）：渲染页面为图片 → OCR → 马赛克遮盖 → 替换页面"""
    import fitz
    import pytesseract
    from PIL import Image
    import numpy as np
    import io
    import cv2

    doc = fitz.open(input_path)
    redacted_count = 0

    for page_num in range(len(doc)):
        page = doc[page_num]
        # 渲染页面为高清图片（300 DPI）
        mat = fitz.Matrix(300 / 72, 300 / 72)
        pix = page.get_pixmap(matrix=mat)
        img_data = pix.tobytes('png')
        img = Image.open(io.BytesIO(img_data))
        img_rgb = img.convert('RGB')
        img_array = np.array(img_rgb)
        img_height, img_width = img_array.shape[:2]

        # PDF 页面尺寸（磅），用于坐标转换
        pdf_rect = page.rect
        scale_x = pdf_rect.width / img_width
        scale_y = pdf_rect.height / img_height

        # OCR
        ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

        # 检测银行 Logo 区域
        logo_regions = _detect_bank_logo_pdf(img_array, ocr_data, scale_x, scale_y)

        # 马赛克遮盖所有敏感文本 + Logo 区域
        redact_regions = []
        n_boxes = len(ocr_data['text'])

        for i in range(n_boxes):
            text = ocr_data['text'][i].strip()
            if not text:
                continue
            redacted = apply_redactions(text)
            if redacted == text:
                continue

            x = ocr_data['left'][i]
            y = ocr_data['top'][i]
            bw = ocr_data['width'][i]
            bh = ocr_data['height'][i]
            pad = 4
            redact_regions.append((x, y, bw, bh))
            redacted_count += 1

        # 应用马赛克
        for (x, y, bw, bh) in redact_regions:
            px1 = max(0, x)
            py1 = max(0, y)
            px2 = min(img_width, x + bw)
            py2 = min(img_height, y + bh)
            if px2 > px1 and py2 > py1:
                apply_mosaic_to_image(img_array, px1, py1, px2, py2)

        # 银行 Logo 区域也打马赛克
        for (x1, y1, x2, y2) in logo_regions:
            px1 = max(0, int(x1))
            py1 = max(0, int(y1))
            px2 = min(img_width, int(x2))
            py2 = min(img_height, int(y2))
            if px2 > px1 and py2 > py1:
                apply_mosaic_to_image(img_array, px1, py1, px2, py2)
                print(f"  [银行Logo马赛克] PDF区域 ({px1},{py1})-({px2},{py2})")

        # 写回页面
        redacted_pil = Image.fromarray(img_array.astype(np.uint8))
        img_bytes = io.BytesIO()
        redacted_pil.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        img_rect = fitz.Rect(0, 0, pdf_rect.width, pdf_rect.height)
        page.add_image(img_rect, stream=img_bytes.read(), overlay=True)

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    print(f"[完成] 扫描版 PDF 共遮盖 {redacted_count} 处，结果保存至: {output_path}")


def _detect_bank_logo_pdf(img_array, ocr_data, scale_x, scale_y):
    """检测 PDF/图片中银行 Logo 区域"""
    import cv2
    import numpy as np

    h, w = img_array.shape[:2]
    logo_regions = []
    n_boxes = len(ocr_data['text'])

    for i in range(n_boxes):
        text = ocr_data['text'][i].strip()
        if '银行' not in text:
            continue

        x = ocr_data['left'][i]
        y = ocr_data['top'][i]
        bw = ocr_data['width'][i]
        bh = ocr_data['height'][i]

        # Logo 通常在"银行"文本左侧
        search_x1 = max(0, x - 320)
        search_y1 = max(0, y - 60)
        search_x2 = min(w, x + bw + 20)
        search_y2 = min(h, y + bh + 60)

        search_region = img_array[search_y1:search_y2, search_x1:search_x2]
        gray = cv2.cvtColor(search_region, cv2.COLOR_RGB2GRAY)

        color_std = np.std(search_region) if len(search_region.shape) == 3 else np.std(gray)
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.sum(edges > 0) / edges.size
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        filled_ratio = np.sum(binary > 0) / binary.size

        if (color_std < 60) or (edge_density > 0.05 and filled_ratio > 0.3) or (filled_ratio > 0.6):
            logo_regions.append((search_x1, search_y1, search_x2, search_y2))

    # 合并重叠区域
    if not logo_regions:
        return []
    merged = [logo_regions[0]]
    for (x1, y1, x2, y2) in logo_regions[1:]:
        last = merged[-1]
        if x1 - last[2] < 50 and not (y2 < last[1] or y1 > last[3]):
            merged[-1] = (last[0], min(last[1], y1), max(last[2], x2), max(last[3], y2))
        else:
            merged.append((x1, y1, x2, y2))
    return merged


def detect_pdf_type(input_path: str) -> str:
    """判断 PDF 是文本型还是图片扫描版"""
    import fitz
    doc = fitz.open(input_path)
    has_text = False
    for page in doc:
        blocks = page.get_text('dict')['blocks']
        for block in blocks:
            if block.get('type') == 0 and block.get('lines'):
                has_text = True
                break
    doc.close()
    return 'text' if has_text else 'image_scan'


def main():
    if len(sys.argv) < 2:
        print("用法: python3 redact_pdf.py <输入文件.pdf> [输出文件路径]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    if output_file is None:
        stem = Path(input_file).stem
        output_file = str(Path(input_file).with_name(f"{stem}_脱敏.pdf"))

    pdf_type = detect_pdf_type(input_file)
    print(f"[信息] 检测到 PDF 类型: {'文本型' if pdf_type == 'text' else '扫描版（图片型）'}")

    if pdf_type == 'text':
        redact_pdf_text(input_file, output_file)
    else:
        redact_pdf_image_scan(input_file, output_file)


if __name__ == '__main__':
    main()
