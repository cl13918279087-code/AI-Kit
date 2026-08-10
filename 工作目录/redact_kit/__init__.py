"""
LLM增强文档脱敏工具包
Phase 1-4 完整实现

目录结构：
  redact_kit/
  ├── config.json           # 配置文件（API Key、替换规则等）
  ├── manifest.py           # RedactionManifest 数据结构（Phase 1）
  ├── prompts.py            # Prompt 工程库（Phase 1）
  ├── llm_client.py         # LLM 客户端（Phase 1）
  ├── entity_detector.py    # 混合实体检测器（Phase 1）
  ├── editor_executor.py    # editor_sdk 执行层（Phase 2）
  ├── ocr_processor.py      # OCR 处理器（Phase 3）
  ├── pipeline.py           # Pipeline 主控（Phase 2）
  ├── app.py                # FastAPI Web 服务（Phase 3）
  └── extensions/           # Phase 4 扩展
      ├── multi_lang.py     # 多语言支持
      ├── templates.py      # 行业模板
      └── feedback.py       # 增量学习
"""

__version__ = "1.0.0"
__all__ = [
    "RedactionManifest",
    "LLMClient",
    "EntityDetector",
    "OCRProcessor",
    "EditorSDKExecutor",
    "RedactionPipeline",
]

from manifest import RedactionManifest
from llm_client import LLMClient
from entity_detector import EntityDetector
from ocr_processor import OCRProcessor
from editor_executor import EditorSDKExecutor
from pipeline import RedactionPipeline
