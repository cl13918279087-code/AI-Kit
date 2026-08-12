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
import io
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from common_rules import apply_redactions

# Tesseract 路径自动检测（从 config.json 读取）
import shutil, platform
_tesseract_paths = [
    r"/opt/homebrew/bin/tesseract",
    r"/usr/bin/tesseract",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    'tesseract',
]
for _p in _tesseract_paths:
    if os.path.exists(_p) or _p == 'tesseract':
        try:
            shutil.run([_p, '--version'], capture_output=True, timeout=5)
            pytesseract.pytesseract.tesseract_cmd = _p
            break
        except Exception:
            pass



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


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def redact_image(input_path: str, output_path: str = None,
                 method: str = "mosaic") -> dict:
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
        stem = Path(input_path).stem
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

    # ② 对每个 OCR 文本块执行脱敏
    apply_fn = {"mosaic": _apply_mosaic, "blur": _apply_blur, "black": _apply_black}.get(method)

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
            counts["文本遮盖"] = counts.get("文本遮盖", 0) + 1

    # ③ 对 Logo 区域执行马赛克（强制 mosaic，不受 method 参数影响）
    for x1, y1, x2, y2 in logo_regions:
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(iw, x2), min(ih, y2)
        if x2 > x1 and y2 > y1:
            _apply_mosaic(img_arr, x1, y1, x2, y2)
            counts["Logo马赛克"] = counts.get("Logo马赛克", 0) + 1
            print(f"  [银行Logo马赛克] 区域 ({x1},{y1})-({x2},{y2})")

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
