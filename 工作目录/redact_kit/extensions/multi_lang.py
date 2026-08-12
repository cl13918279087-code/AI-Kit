"""
Phase 4: 多语言文档支持
extensions/multi_lang.py

支持语言：
- 中文简体（zh-CN）：基础
- 英文（en）：国际银行文档
- 中文繁体（zh-TW）：台湾/香港
- 日文（ja）：外资银行
- 混合语言（mixed）：同一文档含多语言

语言检测 → 路由到对应 LLM prompt → 统一 manifest 合并
"""

from __future__ import annotations

import re
import logging
from typing import Optional

try:
    from langdetect import detect, detect_langs
    from langdetect.lang_detect_exception import LangDetectException
    _LANGDETECT_AVAILABLE = True
except ImportError:
    _LANGDETECT_AVAILABLE = False
    LangDetectException = Exception  # type: ignore

logger = logging.getLogger("multi_lang")

# ============================================================
# 语言检测正则（轻量级，无需 langdetect 依赖时使用）
# ============================================================

LANG_INDICATORS = {
    "zh": re.compile(r'[\u4e00-\u9fff]'),           # 中文
    "en": re.compile(r'[a-zA-Z]{3,}'),            # 英文词
    "ja": re.compile(r'[\u3040-\u309f\u30a0-\u30ff]'),  # 日文
    "ko": re.compile(r'[\uac00-\ud7af]'),           # 韩文
    "ru": re.compile(r'[\u0400-\u04ff]'),           # 俄文
}


def detect_language_fast(text: str) -> str:
    """
    快速语言检测（基于字符集分布，无需外部依赖）
    """
    if not text:
        return "unknown"

    total = len(text)
    scores = {}

    for lang, pattern in LANG_INDICATORS.items():
        matches = len(pattern.findall(text))
        scores[lang] = matches / total if total > 0 else 0

    if not scores or max(scores.values()) < 0.01:
        return "en"  # 默认英文

    return max(scores, key=scores.get)


# ============================================================
# 各语言实体模式（扩展正则库）
# ============================================================

LANG_ENTITY_PATTERNS = {
    "zh-CN": {
        "bank_name_abbr": ["海峡", "分行", "支行"],
        "date_format": "YYYY年MM月DD日",
        "person_suffixes": ["先生", "女士", "经理", "总", "董", "秘"],
    },
    "en": {
        # 英文银行名识别
        "bank_name_patterns": [
            r"(?:Bank of [A-Z][a-z]+)",
            r"(?:[A-Z][a-z]+ Bank)",
            r"(?:[A-Z][a-z]+ (?:Commercial|Savings|Credit) Bank)",
        ],
        # 英文姓名识别（First Last / Last, First）
        "person_patterns": [
            r"(?:Mr\.|Mrs\.|Ms\.|Dr\.)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+",
            r"[A-Z][a-z]+(?:\s+[A-Z]\.)?\s+[A-Z][a-z]+",
        ],
        "date_format": "YYYY-MM-DD",
        "phone_format": r"\+\d[\d\s\-]{9,}",
        "id_format": r"\d{3}-\d{2}-\d{4}",  # SSN格式
    },
    "ja": {
        "person_suffixes": ["様", "氏", "さん", "様", "先生", "銀行"],
        "date_format": "YYYY年MM月DD日",
    },
}


# ============================================================
# 英文实体检测正则
# ============================================================

EN_BANK_PATTERN = re.compile(
    r'\b('
    r'Bank of (?:America|China|Mmerica|International)|'
    r'[A-Z][a-z]+ Bank|'
    r'[A-Z][a-z]+ Commercial Bank|'
    r'[A-Z][a-z]+ Savings Bank|'
    r'[A-Z][a-z]+ Credit Bank|'
    r'HSBC|Citi|JPMorgan|Morgan Stanley|Goldman Sachs|'
    r'Wells Fargo|UBS|Credit Suisse|Deutsche Bank|'
    r'Barclays|Nomura|BNP Paribas'
    r')\b',
    re.IGNORECASE
)

EN_PERSON_PATTERN = re.compile(
    r'\b('
    r'Mr\.\s+[A-Z][a-z]+|'
    r'Mrs\.\s+[A-Z][a-z]+|'
    r'Ms\.\s+[A-Z][a-z]+|'
    r'Dr\.\s+[A-Z][a-z]+|'
    r'[A-Z][a-z]+\s+[A-Z]\.\s+[A-Z][a-z]+|'
    r'[A-Z][a-z]+\s+[A-Z][a-z]+'
    r')\b'
)

EN_PHONE_PATTERN = re.compile(
    r'(?:\+1[\s\-]?)?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4}'
)

EN_DATE_PATTERN = re.compile(
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b'
    r'|\b\d{1,2}[\/\-](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\/\-]\d{2,4}\b'
    r'|\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b'
)


class MultiLangDetector:
    """
    多语言实体检测器

    检测文档主要语言，路由到对应检测逻辑，
    合并各语言结果到统一 Manifest。
    """

    def __init__(self, base_detector):
        """
        Args:
            base_detector: EntityDetector 实例（处理主语言）
        """
        self.base = base_detector

    def detect(self, text: str) -> dict:
        """
        多语言检测

        Returns:
            {
                "primary_lang": "zh-CN",
                "all_langs": ["zh-CN", "en"],
                "entities_by_lang": {...},
                "merged_entities": [...]
            }
        """
        lang = detect_language_fast(text)
        logger.info(f"🌐 检测文档语言: {lang}")

        result = {
            "primary_lang": lang,
            "all_langs": [lang],
        }

        if lang == "zh":
            # 中文文档，主要用 base detector
            entities = self.base.detect_from_text(text)
            result["entities_by_lang"] = {"zh-CN": entities}
            result["merged_entities"] = self._flatten_entities(entities)

        elif lang == "en":
            # 英文文档，用英文模式
            entities = self._detect_english(text)
            result["entities_by_lang"] = {"en": entities}
            result["merged_entities"] = self._flatten_entities(entities)

        elif lang == "ja":
            # 日文文档
            entities = self._detect_japanese(text)
            result["entities_by_lang"] = {"ja": entities}
            result["merged_entities"] = self._flatten_entities(entities)

        else:
            # 混合语言：分段检测
            mixed = self._detect_mixed(text)
            result["all_langs"] = list(mixed.keys())
            result["entities_by_lang"] = mixed
            result["merged_entities"] = []
            for entities in mixed.values():
                result["merged_entities"].extend(self._flatten_entities(entities))

        return result

    def _detect_english(self, text: str) -> list[dict]:
        """英文实体检测"""
        entities = []

        for m in EN_BANK_PATTERN.finditer(text):
            entities.append({
                "text": m.group(),
                "replacement": "XX Bank",
                "confidence": 0.95,
                "category": "bank_name_full",
                "evidence": "英文银行名识别",
            })

        for m in EN_PERSON_PATTERN.finditer(text):
            name = m.group()
            if not any(skip in name for skip in ["Bank", "Company", "Inc", "Ltd", "Corp"]):
                entities.append({
                    "text": name,
                    "replacement": "XXX",
                    "confidence": 0.90,
                    "category": "person",
                    "evidence": "英文姓名格式",
                })

        for m in EN_DATE_PATTERN.finditer(text):
            entities.append({
                "text": m.group(),
                "replacement": "YYYY-MM-DD",
                "confidence": 0.98,
                "category": "date_full",
                "evidence": "英文日期格式",
            })

        for m in EN_PHONE_PATTERN.finditer(text):
            entities.append({
                "text": m.group(),
                "replacement": "***-***-" + m.group()[-4:],
                "confidence": 0.98,
                "category": "phone_number",
                "evidence": "英文电话格式",
            })

        return entities

    def _detect_japanese(self, text: str) -> list[dict]:
        """日文实体检测（预留）"""
        # TODO: 日文姓名/人名识别
        logger.warning("日文检测尚未完整实现")
        return []

    def _detect_mixed(self, text: str) -> dict:
        """混合语言分段检测"""
        # 按段落分组，分别检测语言
        result = {}
        current_lang = "unknown"
        current_chunk = []

        for line in text.split("\n"):
            line_lang = detect_language_fast(line)
            if line_lang == current_lang:
                current_chunk.append(line)
            else:
                # 切换语言，处理前一段
                if current_chunk:
                    chunk_text = "\n".join(current_chunk)
                    lang_entities = self._detect_chunk(current_lang, chunk_text)
                    result[current_lang] = lang_entities

                current_lang = line_lang
                current_chunk = [line]

        # 处理最后一段
        if current_chunk:
            chunk_text = "\n".join(current_chunk)
            lang_entities = self._detect_chunk(current_lang, chunk_text)
            result[current_lang] = lang_entities

        return result

    def _detect_chunk(self, lang: str, chunk: str) -> list[dict]:
        """检测单个语言片段"""
        if lang == "en":
            return self._detect_english(chunk)
        elif lang in ("zh", "zh-CN"):
            return self._flatten_entities(self.base.detect_from_text(chunk))
        else:
            return []

    def _flatten_entities(self, entities) -> list:
        """将 Manifest 实体列表转为普通字典列表"""
        from manifest import SensitiveEntity
        result = []
        if hasattr(entities, "_all_entities"):
            for e in entities._all_entities():
                result.append({
                    "text": e.text,
                    "replacement": e.replacement,
                    "confidence": e.confidence,
                    "category": e.category,
                    "evidence": e.evidence,
                })
        return result
