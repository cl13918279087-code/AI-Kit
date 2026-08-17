from typing import Optional
#!/usr/bin/env python3
"""
redact_ppt.py - PPT 演示文稿脱敏脚本
支持 .pptx（OOXML）和 .ppt（PowerPoint 97-2003 格式）

处理范围：
  - 幻灯片正文文本、表格、文本框、组合形状
  - 演讲者备注、页眉页脚
  - 嵌入图片中的银行 Logo（基于文件名检测）

依赖: python-pptx（.pptx）、libreoffice（.ppt → .pptx 转换）
安装: pip install python-pptx
"""

import sys
import re
import zipfile
import shutil
import subprocess
import tempfile
import platform
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from common_rules import apply_redactions

# ---------------------------------------------------------------------------
# PPT XML 命名空间
# ---------------------------------------------------------------------------
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


# ---------------------------------------------------------------------------
# .pptx 处理（直接 XML 编辑，保留所有格式）
# ---------------------------------------------------------------------------

def redact_pptx(input_path: str, output_path: str) -> dict:
    """
    处理 .pptx 文件：python-pptx 处理主文本 + ZIP/XML 深层处理。
    策略：先解压修改 XML，再重新打包，完全绕开 python-pptx 保存的格式丢失问题。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="redact_pptx_"))
    counts = {}

    try:
        # ① 解压
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(tmp_dir)

        # ② 处理幻灯片 XML（正文/文本框/形状）
        slides_dir = tmp_dir / "ppt" / "slides"
        if slides_dir.exists():
            for xml_file in sorted(slides_dir.glob("slide*.xml")):
                c = _process_xml_file(xml_file, f"幻灯片 {xml_file.stem}")
                _merge_counts(counts, c)

        # ③ 处理幻灯片布局
        layouts_dir = tmp_dir / "ppt" / "slideLayouts"
        if layouts_dir.exists():
            for xml_file in sorted(layouts_dir.glob("*.xml")):
                _process_xml_file(xml_file, f"布局 {xml_file.stem}")

        # ④ 处理母版
        masters_dir = tmp_dir / "ppt" / "slideMasters"
        if masters_dir.exists():
            for xml_file in sorted(masters_dir.glob("*.xml")):
                _process_xml_file(xml_file, f"母版 {xml_file.stem}")

        # ⑤ 处理备注页
        notes_dir = tmp_dir / "ppt" / "notesSlides"
        if notes_dir.exists():
            for xml_file in sorted(notes_dir.glob("*.xml")):
                c = _process_xml_file(xml_file, f"备注 {xml_file.stem}")
                _merge_counts(counts, c)

        # ⑥ 处理音频/视频文件名（检查是否含敏感词）
        media_dir = tmp_dir / "ppt" / "media"
        if media_dir.exists():
            _process_media_dir(media_dir)

        # ⑦ 处理银行 Logo 图片（文件名含银行相关关键词）
        _redact_bank_logos(tmp_dir / "ppt" / "media")

        # ⑧ 重新打包
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(tmp_dir.rglob("*")):
                if fp.is_file():
                    arcname = str(fp.relative_to(tmp_dir))
                    zf.write(fp, arcname)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return counts


def _process_xml_file(path: Path, label: str = "") -> dict:
    """对单个 XML 文件执行脱敏，返回变化计数"""
    try:
        content = path.read_text("utf-8")
        original = content
        redacted = apply_redactions(content)
        if redacted != original:
            path.write_text(redacted, "utf-8")
            print(f"  [更新] {label or path.name}")
            return {"XML文件": 1}
    except Exception as e:
        print(f"  [警告] 处理 {label or path.name} 出错: {e}", file=sys.stderr)
    return {}


def _merge_counts(base: dict, new: dict) -> None:
    for k, v in new.items():
        base[k] = base.get(k, 0) + v


def _process_media_dir(media_dir: Path) -> None:
    """检查媒体文件名是否含敏感词，含敏感词则重命名为脱敏名"""
    for f in media_dir.iterdir():
        name = f.name
        redacted = name
        # 对文件名中的中文关键词脱敏（保留扩展名）
        stem = f.stem
        ext = f.suffix
        redacted_stem = apply_redactions(stem)
        if redacted_stem != stem:
            new_path = f.with_name(f"{redacted_stem}{ext}")
            f.rename(new_path)
            print(f"  [媒体重命名] {stem}{ext} → {redacted_stem}{ext}")


def _mosaic_image(img_path: Path, quality: int = 8) -> None:
    """将图片原地马赛克处理（缩小→放大）"""
    from PIL import Image
    img = Image.open(img_path).convert("RGB")
    small = img.resize(
        (max(1, img.width // quality), max(1, img.height // quality)),
        Image.NEAREST,
    )
    big = small.resize(img.size, Image.NEAREST)
    big.save(img_path)


def _is_logo_image(img: Image.Image) -> bool:
    """
    基于图片统计特征判断是否为 Logo/文字型图片。
    特征：颜色数少（<300）、高宽比特殊（横条/小图）、边缘弱
    """
    import io
    w, h = img.size
    # 横条型（宽>300，高<200，宽高比>3）→ 典型 Banner/Header Logo
    if w > 300 and h < 200 and w / max(h, 1) > 3:
        return True
    # 颜色数少 + 尺寸小（<300x300）
    pixels = list(img.convert("L").getdata())
    unique_colors = len(set(pixels))
    if unique_colors < 300 and w < 1000 and h < 300:
        return True
    return False


def _redact_bank_logos(media_dir: Path) -> None:
    """
    检测并遮盖银行 Logo 图片。
    策略：结合文件名关键词检测 + 图片统计特征（颜色数/尺寸）双重判断。
    """
    if not media_dir.exists():
        return

    from PIL import Image

    sensitive_keywords = ["bank", "logo", "银行", "brand"]
    blurred = []
    skipped = []

    for img_file in media_dir.iterdir():
        name_lower = img_file.name.lower()

        # 策略1：文件名含敏感关键词 → 直接模糊
        if any(k in name_lower for k in sensitive_keywords):
            try:
                _mosaic_image(img_file)
                blurred.append(img_file.name)
                print(f"  [Logo遮盖-文件名] {img_file.name}")
                continue
            except Exception as e:
                print(f"  [警告] {img_file.name}: {e}")

        # 策略2：图片统计特征判断（颜色数少/横条型）
        try:
            img = Image.open(img_file)
            if _is_logo_image(img):
                _mosaic_image(img_file)
                blurred.append(img_file.name)
                print(f"  [Logo遮盖-特征分析] {img_file.name}: {img.size}")
            else:
                skipped.append(img_file.name)
        except Exception as e:
            print(f"  [警告] 无法分析图片 {img_file.name}: {e}")

    if blurred:
        print(f"  [Logo处理] 模糊 {len(blurred)} 个，保留原图 {len(skipped)} 个")


# ---------------------------------------------------------------------------
# .ppt 处理（转换为 .pptx 后处理）
# ---------------------------------------------------------------------------

def _find_converter() -> Optional[str]:
    """查找可用的 PPT 转换工具"""
    # LibreOffice（跨平台）
    for cmd in ["soffice", "libreoffice"]:
        r = subprocess.run(
            ["which", cmd], capture_output=True, text=True
        )
        if r.returncode == 0:
            return r.stdout.strip()

    # Windows PowerPoint COM（需要在 Windows 上）
    if platform.system() == "Windows":
        return "powershell"

    return None


def redact_ppt_to_pptx(input_path: str, output_pptx: str) -> dict:
    """将 .ppt 转换为 .pptx（使用 LibreOffice）后处理"""
    converter = _find_converter()
    if converter in ("soffice", "libreoffice"):
        tmp_dir = Path(tempfile.mkdtemp(prefix="ppt_convert_"))
        try:
            result = subprocess.run(
                [
                    converter,
                    "--headless",
                    "--convert-to", "pptx",
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
            # 找到生成的 pptx 文件
            converted = next(tmp_dir.glob("*.pptx"), None)
            if converted is None:
                raise RuntimeError("no output file found")
            shutil.copy2(converted, output_pptx)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        # 无转换工具时直接复制（无法处理内容）
        print("  [警告] 未找到 LibreOffice，无法转换 .ppt 文件，内容跳过处理")
        shutil.copy2(input_path, output_pptx)
    return {}


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def redact_ppt(input_path: str, output_path: str = None) -> dict:
    """自动根据扩展名分发处理，返回脱敏统计"""
    if output_path is None:
        stem = Path(input_path).stem
        output_path = str(Path(input_path).with_name(f"{stem}_脱敏.pptx"))

    ext = Path(input_path).suffix.lower()
    counts = {}

    if ext == ".pptx":
        counts = redact_pptx(input_path, output_path)
    elif ext == ".ppt":
        # .ppt 转换为 .pptx 再处理
        tmp_pptx = str(Path(tempfile.gettempdir()) / f"_tmp_{Path(input_path).stem}.pptx")
        redact_ppt_to_pptx(input_path, tmp_pptx)
        counts = redact_pptx(tmp_pptx, output_path)
        Path(tmp_pptx).unlink(missing_ok=True)
    else:
        print(f"[错误] 不支持的文件格式: {ext}（仅支持 .pptx 和 .ppt）", file=sys.stderr)
        sys.exit(1)

    total = sum(counts.values())
    print(f"[完成] 共遮盖 {total} 处，结果保存至: {output_path}")
    if counts:
        for label, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"         - {label}: {n} 处")
    return counts


def main():
    if len(sys.argv) < 2:
        print("用法: python3 redact_ppt.py <输入文件.pptx/.ppt> [输出文件路径]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    redact_ppt(input_file, output_file)


if __name__ == "__main__":
    main()
