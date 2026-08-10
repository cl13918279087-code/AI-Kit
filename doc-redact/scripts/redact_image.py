#!/usr/bin/env python3
"""
redact_image.py - 图片脱敏脚本
支持 .png / .jpg / .jpeg / .bmp 等格式
依赖: pytesseract, Pillow, numpy, opencv-python
安装: pip install pytesseract Pillow numpy opencv-python
注意: 需安装 Tesseract OCR 引擎 (macOS: brew install tesseract; 中文: brew install tesseract-lang)
"""

import sys
import re
import io
from pathlib import Path

# ---------------------------------------------------------------------------
# 脱敏规则（9类 + 银行Logo）
# ---------------------------------------------------------------------------
# 执行顺序：邮箱→地址→身份证→银行卡→日期→手机/固话→银行名称→银行Logo→人员姓名
REPLACEMENTS = [
    # ① 电子邮箱
    (re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'), 'XXXXX@XXXXX'),
    # ② 详细地址
    (re.compile(
        r'[^\x00-\xFF]{2,6}(?:省|自治区|市)?[^\x00-\xFF]{0,10}'
        r'(?:市|区)?[^\x00-\xFF]{0,10}'
        r'(?:街|路|道|巷|弄|号|大道|大街|东路|西路|南路|北路)[^\x00-\xFF]{0,30}'
    ), 'XX省XX市XX区XXXX'),
    # ③ 身份证号
    (re.compile(r'[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]'),
     'XXXXXXXXXXXXXXXXXX'),
    # ④ 银行卡号
    (re.compile(r'\b(?:\d{16}|\d{17}|\d{18}|\d{19})\b'), 'XXXXXXXXXXXXXXXX'),
    # ⑤ 日期信息
    (re.compile(
        r'\d{4}[-年](?:0[1-9]|1[0-2])[-月](?:0[1-9]|[12]\d|3[01])[日]?\s*'
        r'|(?:19|20)\d{2}年\d{1,2}月\d{1,2}日'
    ), 'YYYY/MM/DD'),
    # ⑥ 手机号码
    (re.compile(r'\b1[3-9]\d{9}\b'), 'XXXXXXXXXXX'),
    # ⑦ 固定电话
    (re.compile(r'0\d{2,3}[-\s]?\d{7,8}'), '0XX-XXXXXXXX'),
    # ⑧ 银行名称
    (re.compile(
        r'(?:(?:中国|交通|招商|浦发|兴业|民生|华夏|平安|光大|广发|浙商|渤海|恒丰|'
        r'南京|宁波|杭州|深圳|上海|北京|广州|农业|建设|工商|中国)银行|'
        r'(?:农信社|信用社|农商银行|合作银行|人民银行))'
    ), '[某银行]'),
    # ⑨ 人员姓名（最后执行）
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
# 银行 Logo 检测（基于 OCR + 图像分析）
# ---------------------------------------------------------------------------

def detect_bank_logo_regions(img_array, ocr_data):
    """
    检测图片中银行 Logo 区域。

    策略：
    1. OCR 找到含"银行"关键词的文本
    2. 扩展该文本周围区域（约 Logo 尺寸 80x80~300x300px）
    3. 用 OpenCV 检测该区域是否符合 Logo 特征：
       - 颜色分布较均匀（非自然照片的随机纹理）
       - 边缘清晰、形状规整（矩形/圆形/椭圆形）
    4. 返回需要打码的矩形区域列表 [(x1,y1,x2,y2), ...]
    """
    import numpy as np
    import cv2

    h, w = img_array.shape[:2]
    logo_regions = []
    n_boxes = len(ocr_data['text'])

    for i in range(n_boxes):
        text = ocr_data['text'][i].strip()
        # 检查是否含"银行"关键词
        if '银行' not in text:
            continue

        x = ocr_data['left'][i]
        y = ocr_data['top'][i]
        bw = ocr_data['width'][i]
        bh = ocr_data['height'][i]

        # 在文本左侧查找 Logo（通常 Logo 在标题左侧）
        # 扩展搜索区域：文本左侧 0~300px，上下方各扩展 50px
        search_x1 = max(0, x - 320)
        search_y1 = max(0, y - 60)
        search_x2 = min(w, x + bw + 20)
        search_y2 = min(h, y + bh + 60)

        search_region = img_array[search_y1:search_y2, search_x1:search_x2]
        gray = cv2.cvtColor(search_region, cv2.COLOR_RGB2GRAY) if len(search_region.shape) == 3 else search_region

        # Logo 检测启发式规则：
        # 1. 颜色方差小（颜色均匀）→ 可能是色块 Logo
        color_std = np.std(search_region) if len(search_region.shape) == 3 else np.std(gray)
        # 2. 边缘密度高（形状规整）→ 可能是文字/图形 Logo
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.sum(edges > 0) / edges.size

        # 3. 检测实心色块（可能是 Logo 背景）
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        filled_ratio = np.sum(binary > 0) / binary.size

        # 4. 判断是否为 Logo 区域
        is_logo_like = (
            (color_std < 60) or          # 颜色较均匀
            (edge_density > 0.05 and filled_ratio > 0.3) or  # 边缘清晰
            (filled_ratio > 0.6)         # 实心色块
        )

        if is_logo_like:
            logo_regions.append((search_x1, search_y1, search_x2, search_y2))

    # 合并重叠区域
    merged = _merge_overlapping_regions(logo_regions)
    return merged


def _merge_overlapping_regions(regions, threshold=50):
    """合并重叠或距离过近的区域"""
    if not regions:
        return []
    import numpy as np
    regions = sorted(regions, key=lambda r: r[0])
    merged = [regions[0]]
    for (x1, y1, x2, y2) in regions[1:]:
        last = merged[-1]
        # 如果与上一个区域有重叠或距离<threshold，合并
        if x1 - last[2] < threshold and not (y2 < last[1] or y1 > last[3]):
            merged[-1] = (last[0], min(last[1], y1), max(last[2], x2), max(last[3], y2))
        else:
            merged.append((x1, y1, x2, y2))
    return merged


def apply_mosaic(img_array, x1, y1, x2, y2, block_size=16):
    """对指定区域应用马赛克"""
    import numpy as np
    from PIL import Image

    region = img_array[y1:y2, x1:x2]
    pil_region = Image.fromarray(region.astype(np.uint8))
    h, w = pil_region.size[1], pil_region.size[0]

    # 缩小后放大实现马赛克
    small_h = max(2, h // block_size)
    small_w = max(2, w // block_size)
    small = pil_region.resize((small_h, small_w), Image.NEAREST)
    mosaic = small.resize((w, h), Image.NEAREST)

    img_array[y1:y2, x1:x2] = np.array(mosaic).astype(np.uint8)


def redact_image(input_path: str, output_path: str = None,
                 method: str = 'mosaic') -> str:
    """
    图片脱敏主函数

    参数:
        input_path: 输入图片路径
        output_path: 输出路径（默认在文件名后加 _脱敏）
        method: 遮盖方式
            - 'mosaic': 马赛克像素化（默认，推荐）
            - 'blur': 高斯模糊
            - 'black': 纯黑色填充
    """
    import pytesseract
    from PIL import Image, ImageFilter
    import numpy as np
    import cv2

    if output_path is None:
        stem = Path(input_path).stem
        ext = Path(input_path).suffix
        output_path = str(Path(input_path).with_name(f"{stem}_脱敏{ext}"))

    img = Image.open(input_path)
    img_rgb = img.convert('RGB')
    img_array = np.array(img_rgb)
    h, w = img_array.shape[:2]

    # OCR 获取文字及坐标
    ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    n_boxes = len(ocr_data['text'])
    redact_count = 0

    # Step 1: 检测银行 Logo 区域
    logo_regions = detect_bank_logo_regions(img_array, ocr_data)

    # Step 2: 对每个 OCR 文本块执行脱敏
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

        # 扩展区域，防止边缘残留
        pad = 3
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(w, x + bw + pad)
        y2 = min(h, y + bh + pad)

        if method == 'mosaic':
            apply_mosaic(img_array, x1, y1, x2, y2)
        elif method == 'blur':
            region = img_array[y1:y2, x1:x2]
            blurred = Image.fromarray(region).filter(ImageFilter.GaussianBlur(radius=15))
            img_array[y1:y2, x1:x2] = np.array(blurred)
        elif method == 'black':
            img_array[y1:y2, x1:x2] = 0

        redact_count += 1

    # Step 3: 对银行 Logo 区域执行马赛克（Logo 强制使用 mosaic）
    for (x1, y1, x2, y2) in logo_regions:
        apply_mosaic(img_array, x1, y1, x2, y2)
        redact_count += 1
        print(f"  [银行Logo马赛克] 区域 ({x1},{y1})-({x2},{y2})")

    result_img = Image.fromarray(img_array.astype(np.uint8))
    result_img.save(output_path)
    print(f"[完成] 共遮盖 {redact_count} 处敏感区域（方法: {method}），结果保存至: {output_path}")
    return output_path


def main():
    if len(sys.argv) < 2:
        print("用法: python3 redact_image.py <输入图片> [输出路径] [--method mosaic|blur|black]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith('--') else None
    method = 'mosaic'
    for arg in sys.argv:
        if arg.startswith('--method='):
            method = arg.split('=')[1]

    ext = Path(input_file).suffix.lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff', '.webp'):
        print(f"[错误] 不支持的文件格式: {ext}", file=sys.stderr)
        sys.exit(1)

    redact_image(input_file, output_file, method)


if __name__ == '__main__':
    main()
