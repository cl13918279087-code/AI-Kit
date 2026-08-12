"""
OCR 处理器
LLM增强脱敏工具包 - Phase 3

支持多 OCR 引擎：
- tesseract：系统已安装，无需额外依赖
- paddle：（预留，需安装 paddlepaddle）

功能：
1. 图片文字识别
2. 基于 OCR 结果判断图片是否含敏感信息
3. 返回图片类型判断结果
"""

from __future__ import annotations

import subprocess
import logging
import tempfile
import os
import json

from llm_client import LLMClient
from prompts import IMAGE_CONTENT_CHECK_PROMPT

logger = logging.getLogger("ocr_processor")


class OCRResult:
    """OCR 识别结果封装"""
    def __init__(self, text: str, confidence: float = 0.0,
                 lang: str = "", raw: dict = None):
        self.text = text
        self.confidence = confidence
        self.lang = lang
        self.raw = raw or {}

    def has_content(self) -> bool:
        return bool(self.text.strip())

    def __repr__(self):
        return f"OCRResult(text={self.text[:50]!r}..., conf={self.confidence:.2f})"


class OCRProcessor:
    """
    OCR 处理器

    使用 tesseract 进行图片文字识别，
    识别结果发送给 LLM 判断是否含敏感信息。
    """

    def __init__(self, llm_client: LLMClient, config: dict):
        self.llm = llm_client
        self.config = config
        self.ocr_cfg = config.get("ocr", {})
        self.tesseract_cmd = self.ocr_cfg.get("tesseract_cmd", "/opt/homebrew/bin/tesseract")
        self.lang = self.ocr_cfg.get("lang", "chi_sim+eng")

    def recognize(self, image_path: str) -> OCRResult:
        """
        对图片执行 OCR 识别

        Args:
            image_path: 图片路径（本地路径或 file:// URL）

        Returns:
            OCRResult 对象
        """
        # 去除 file:// 前缀
        if image_path.startswith("file://"):
            image_path = image_path[7:]

        logger.info(f"🔍 OCR 识别: {image_path}")

        # 检查文件是否存在
        if not os.path.exists(image_path):
            logger.warning(f"   图片文件不存在: {image_path}")
            return OCRResult("")

        try:
            # 执行 tesseract
            result = subprocess.run(
                [
                    self.tesseract_cmd,
                    image_path,
                    "stdout",
                    "-l", self.lang,
                    "--psm", "6",  # 假设统一文本块
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.warning(f"   Tesseract 错误: {result.stderr}")
                return OCRResult("")

            text = result.stdout.strip()
            logger.info(f"   识别到 {len(text)} 字符")

            return OCRResult(text=text, confidence=1.0, lang=self.lang)

        except subprocess.TimeoutExpired:
            logger.warning("   OCR 识别超时")
            return OCRResult("")
        except FileNotFoundError:
            logger.error(f"   Tesseract 未安装: {self.tesseract_cmd}")
            logger.error("   请安装: brew install tesseract tesseract-lang")
            return OCRResult("")
        except Exception as e:
            logger.error(f"   OCR 异常: {e}")
            return OCRResult("")

    def analyze_image_sensitive(
        self, image_path: str, image_type_hint: str = ""
    ) -> dict:
        """
        综合 OCR + LLM 判断图片是否含敏感信息

        Args:
            image_path: 图片路径
            image_type_hint: 图片类型提示（header/footer/screenshot/logo/other）

        Returns:
            {
                "contains_sensitive": bool,
                "confidence": float,
                "sensitive_items": list[str],
                "image_type": str,
                "action": "mosaic|keep|blur",
                "ocr_text": str,
            }
        """
        # Step 1: OCR 识别
        ocr_result = self.recognize(image_path)

        if not ocr_result.has_content():
            logger.info("   图片无文字内容，判定为装饰性图片 → keep")
            return {
                "contains_sensitive": False,
                "confidence": 1.0,
                "sensitive_items": [],
                "image_type": "decorative",
                "action": "keep",
                "ocr_text": "",
            }

        # Step 2: LLM 判断图片内容
        prompt = f"""## 图片 OCR 识别结果
---
{ocr_result.text}
---

## 图片类型提示
{image_type_hint or "未知"}

请判断图片是否包含敏感信息。"""

        response = self.llm.chat(
            prompt=prompt,
            system=IMAGE_CONTENT_CHECK_PROMPT,
            response_format="json",
        )

        if response.error:
            logger.error(f"   LLM 图片分析失败: {response.error}")
            return {
                "contains_sensitive": False,
                "confidence": 0.0,
                "sensitive_items": [],
                "image_type": "unknown",
                "action": "keep",
                "ocr_text": ocr_result.text,
            }

        try:
            result = self.llm.parse_json_response(response)
            result["ocr_text"] = ocr_result.text
            return result
        except json.JSONDecodeError:
            logger.error("   LLM 响应解析失败")
            return {
                "contains_sensitive": False,
                "confidence": 0.0,
                "sensitive_items": [],
                "image_type": "unknown",
                "action": "keep",
                "ocr_text": ocr_result.text,
            }

    def batch_analyze(
        self, image_paths: list[tuple[int, str]]
    ) -> list[dict]:
        """
        批量分析图片敏感内容

        Args:
            image_paths: [(index, image_path), ...] 列表

        Returns:
            每个图片的分析结果
        """
        results = []
        for idx, path in image_paths:
            logger.info(f"[{idx+1}/{len(image_paths)}] 分析图片...")
            result = self.analyze_image_sensitive(path)
            result["index"] = idx
            results.append(result)
        return results
