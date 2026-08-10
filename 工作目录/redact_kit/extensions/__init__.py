"""
Phase 4 扩展模块
"""

from extensions.multi_lang import MultiLangDetector, detect_language_fast
from extensions.templates import IndustryTermLibrary, TemplateManager
from extensions.feedback import FeedbackCollector, init_db

__all__ = [
    "MultiLangDetector",
    "detect_language_fast",
    "IndustryTermLibrary",
    "TemplateManager",
    "FeedbackCollector",
    "init_db",
]
