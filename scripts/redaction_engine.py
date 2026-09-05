#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# redaction_engine.py - 统一脱敏流水线引擎
# doc-redact-project / v1.2.0
#
# 架构原则（v2 整改）：
#   1. 四阶段清晰分离：TextExtractor → EntityDetector → RedactionEngine → XMLWriter
#   2. entity_detector.py 只负责 LLM+RoleWord 高层检测，不重复 common_rules 的规则
#   3. common_rules.py 是唯一规则来源（单点维护）
#   4. 所有阶段支持优雅降级，任意阶段失败不阻断后续处理
#   5. 分层状态机（RedactionStatus）替代简单的 success/failed
# ---------------------------------------------------------------------------

from __future__ import annotations

import time
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from manifest import RedactionManifest, SensitiveEntity

logger = logging.getLogger("redaction_engine")


# ---------------------------------------------------------------------------
# 分层状态机（替代简单的 success/failed）
# ---------------------------------------------------------------------------

class RedactionStatus(str, Enum):
    """脱敏处理分层状态"""
    PENDING    = "pending"     # 初始状态
    DETECTING  = "detecting"  # 实体检测中
    APPLYING   = "applying"   # 脱敏应用中文
    WRITING    = "writing"     # 文件写出中
    COMPLETE   = "complete"   # 完成
    FAILED     = "failed"     # 失败


@dataclass
class StageResult:
    """单个阶段的执行结果"""
    stage: str                      # 阶段名称
    status: RedactionStatus         # 状态
    duration_ms: int = 0           # 耗时
    entities_count: int = 0        # 检测/处理的实体数
    error: str = ""                # 错误信息（若有）
    warning: str = ""              # 警告信息（若有）
    details: Dict[str, Any] = field(default_factory=dict)  # 额外详情

    @property
    def is_success(self) -> bool:
        return self.status in (RedactionStatus.COMPLETE, RedactionStatus.DETECTING, RedactionStatus.APPLYING, RedactionStatus.WRITING)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "entities_count": self.entities_count,
            "error": self.error,
            "warning": self.warning,
            "details": self.details,
        }


@dataclass
class RedactionResult:
    """
    完整脱敏处理结果（分层状态机版本）。

    状态流转：PENDING → DETECTING → APPLYING → WRITING → COMPLETE/FAILED
    """
    input_path: str
    output_path: str = ""
    status: RedactionStatus = RedactionStatus.PENDING
    error: str = ""
    stages: List[StageResult] = field(default_factory=list)

    # 汇总字段
    duration_ms: int = 0
    entities_count: int = 0
    file_size: int = 0
    manifest_path: str = ""

    def add_stage(self, stage: StageResult) -> None:
        self.stages.append(stage)
        if stage.error:
            self.status = RedactionStatus.FAILED
            self.error = stage.error

    def set_complete(self) -> None:
        if self.status != RedactionStatus.FAILED:
            self.status = RedactionStatus.COMPLETE

    @property
    def is_success(self) -> bool:
        return self.status == RedactionStatus.COMPLETE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_path": self.input_path,
            "output_path": self.output_path,
            "status": self.status.value,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "entities_count": self.entities_count,
            "file_size": self.file_size,
            "manifest_path": self.manifest_path,
            "stages": [s.to_dict() for s in self.stages],
        }


# ---------------------------------------------------------------------------
# Stage 1: TextExtractor（文本提取器）
# ---------------------------------------------------------------------------

class TextExtractor(ABC):
    """文本提取器抽象基类"""

    @abstractmethod
    def extract(self, input_path: str) -> tuple[str, Dict[str, Any]]:
        """
        提取纯文本内容。
        返回: (text_content, metadata_dict)
        metadata 包含: page_count, line_count, has_tables, has_images 等
        """
        ...

    @abstractmethod
    def extract_xml_text_with_ranges(self, xml_path: str) -> tuple[str, list, list]:
        """
        提取 XML 中的文本并返回 (text, nodes, ranges)。
        返回 (完整拼接文本, w:t节点列表, 字符区间列表)
        区间格式: [(start, end), ...]，与 nodes 一一对应
        """
        ...


# ---------------------------------------------------------------------------
# Stage 2: EntityDetector（实体检测器）
# ---------------------------------------------------------------------------

class EntityDetector(ABC):
    """
    实体检测器抽象基类。

    设计原则：
      - 本类只定义接口，具体实现为 LLMDetector / RegexDetector / HybridDetector
      - 不直接持有规则（规则统一在 common_rules.py，单点维护）
      - 检测结果统一为 RedactionManifest 格式
    """

    @abstractmethod
    def detect(self, text: str) -> RedactionManifest:
        """
        从文本内容检测所有敏感实体。
        返回 RedactionManifest（统一格式）。
        """
        ...

    def detect_with_context(self, text: str, context_snippet: int = 200) -> RedactionManifest:
        """
        带上下文的检测（默认截取首尾各200字符作为全局上下文）。
        默认实现直接调用 detect()，子类可覆盖优化。
        """
        return self.detect(text)


# ---------------------------------------------------------------------------
# Stage 3: RedactionEngine（脱敏引擎）
# ---------------------------------------------------------------------------

class RedactionEngine(ABC):
    """
    脱敏引擎抽象基类。

    职责：
      - 将检测结果（RedactionManifest）应用到 XML/DOM 结构
      - 处理跨节点实体（如日期范围跨多个 w:r）
      - 协调 _redistribute_text_nodes 等 XML 级操作
      - 跟踪 llm_modified_nodes 避免双加工
    """

    @abstractmethod
    def apply(
        self,
        xml_path: str,
        manifest: RedactionManifest,
        nodes: list,
        ranges: list,
    ) -> StageResult:
        """
        将 manifest 中的实体应用到 XML 结构。
        返回 StageResult。
        """
        ...


# ---------------------------------------------------------------------------
# Stage 4: XMLWriter（文件写出器）
# ---------------------------------------------------------------------------

class XMLWriter(ABC):
    """XML 写出器抽象基类"""

    @abstractmethod
    def write(self, xml_path: str, tree) -> StageResult:
        """
        将修改后的 XML 写回文件。
        返回 StageResult。
        """
        ...


# ---------------------------------------------------------------------------
# 统一流水线 Orchestrator
# ---------------------------------------------------------------------------

class RedactionOrchestrator:
    """
    统一脱敏流水线 Orchestrator。

    协调四个阶段按序执行，支持任意阶段失败时优雅降级，
    保证"能脱多少脱多少"，不因单阶段失败整体中断。

    使用方式：
        orch = RedactionOrchestrator(
            extractor=WordTextExtractor(),
            detector=HybridEntityDetector(config_path="config.json"),
            engine=WordRedactionEngine(),
            writer=WordXMLWriter(),
        )
        result = orch.process("input.docx", "output.docx")
    """

    def __init__(
        self,
        extractor: TextExtractor,
        detector: Optional[EntityDetector],
        engine: Optional[RedactionEngine],
        writer: Optional[XMLWriter],
    ):
        self.extractor = extractor
        self.detector = detector
        self.engine = engine
        self.writer = writer

    def process(self, input_path: str, output_path: str) -> RedactionResult:
        """
        执行完整流水线。
        任意阶段失败都会记录到 result 中，但继续执行后续阶段。
        """
        start_time = time.time()
        result = RedactionResult(input_path=input_path, output_path=output_path)

        # Stage 1: Text Extraction
        stage_start = time.time()
        result.add_stage(StageResult(stage="text_extraction", status=RedactionStatus.DETECTING))
        try:
            text, metadata = self.extractor.extract(input_path)
            result.stages[-1].status = RedactionStatus.COMPLETE
            result.stages[-1].duration_ms = int((time.time() - stage_start) * 1000)
            result.stages[-1].details["metadata"] = metadata
            result.stages[-1].details["char_count"] = len(text)
        except Exception as e:
            result.stages[-1].status = RedactionStatus.FAILED
            result.stages[-1].error = f"文本提取失败: {e}"
            result.stages[-1].duration_ms = int((time.time() - stage_start) * 1000)
            logger.warning("文本提取失败，继续（降级模式）: %s", e)
            text = ""
            metadata = {}

        if not text and self.engine is None:
            result.error = "文本提取失败且无降级方案，终止"
            result.status = RedactionStatus.FAILED
            result.duration_ms = int((time.time() - start_time) * 1000)
            return result

        # Stage 2: Entity Detection
        stage_start = time.time()
        result.add_stage(StageResult(stage="entity_detection", status=RedactionStatus.DETECTING))
        manifest = RedactionManifest()
        if self.detector and text:
            try:
                manifest = self.detector.detect(text)
                manifest.total_characters = len(text)
                result.stages[-1].status = RedactionStatus.COMPLETE
                result.stages[-1].entities_count = len(manifest.entities)
                result.stages[-1].duration_ms = int((time.time() - stage_start) * 1000)
                result.entities_count += len(manifest.entities)
            except Exception as e:
                result.stages[-1].status = RedactionStatus.FAILED
                result.stages[-1].error = f"实体检测失败: {e}"
                result.stages[-1].duration_ms = int((time.time() - stage_start) * 1000)
                logger.warning("实体检测失败，继续（纯规则模式）: %s", e)
        else:
            result.stages[-1].status = RedactionStatus.COMPLETE
            result.stages[-1].warning = "无检测器，降级为纯规则"
            result.stages[-1].duration_ms = int((time.time() - stage_start) * 1000)

        # Stage 3: Apply Redactions
        stage_start = time.time()
        result.add_stage(StageResult(stage="applying_redactions", status=RedactionStatus.APPLYING))
        if self.engine and text:
            try:
                # 提取 XML 节点和区间（供 engine 使用）
                if hasattr(self.extractor, 'extract_xml_text_with_ranges'):
                    _, nodes, ranges = self.extractor.extract_xml_text_with_ranges(input_path)
                    stage_result = self.engine.apply(input_path, manifest, nodes, ranges)
                    result.stages[-1] = stage_result
                    result.stages[-1].duration_ms = int((time.time() - stage_start) * 1000)
                else:
                    result.stages[-1].status = RedactionStatus.COMPLETE
                    result.stages[-1].warning = "extractor 不支持 XML 节点提取，跳过 apply 阶段"
                    result.stages[-1].duration_ms = int((time.time() - stage_start) * 1000)
            except Exception as e:
                result.stages[-1].status = RedactionStatus.FAILED
                result.stages[-1].error = f"应用脱敏失败: {e}"
                result.stages[-1].duration_ms = int((time.time() - stage_start) * 1000)
                logger.warning("应用脱敏失败: %s", e)

        # Stage 4: Write Output
        stage_start = time.time()
        result.add_stage(StageResult(stage="xml_writing", status=RedactionStatus.WRITING))
        result.duration_ms = int((time.time() - start_time) * 1000)

        if result.status != RedactionStatus.FAILED:
            result.set_complete()
            result.duration_ms = int((time.time() - start_time) * 1000)

        return result


# ---------------------------------------------------------------------------
# 工厂函数：构建标准 Word 流水线
# ---------------------------------------------------------------------------

def build_word_pipeline(config_path: Optional[str] = None) -> RedactionOrchestrator:
    """
    构建标准 Word 文档脱敏流水线。

    返回配置好的 RedactionOrchestrator，包含：
      - WordTextExtractor（从 redact_word.py 提取的公共逻辑）
      - HybridEntityDetector（entity_detector.py）
      - WordRedactionEngine（从 redact_word.py 提取的公共逻辑）
      - WordXMLWriter
    """
    from entity_detector import build_llm_detector

    # 使用 entity_detector 作为检测器（支持 LLM + RoleWord）
    detector_impl = build_llm_detector(config_path=config_path)

    # 各阶段实现待从 redact_word.py 提取
    # 暂用占位符，完整迁移后替换
    extractor_impl = None
    engine_impl = None
    writer_impl = None

    return RedactionOrchestrator(
        extractor=extractor_impl,
        detector=detector_impl,
        engine=engine_impl,
        writer=writer_impl,
    )
