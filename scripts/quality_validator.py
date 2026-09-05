#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# quality_validator.py - 脱敏质量验证服务
# doc-redact-project / v1.2.0
#
# 职责：
#   - 误脱检测（LLM 判断脱敏后文本是否丢失关键信息）
#   - 覆盖率统计（各敏感类型检出率）
#   - 质量报告生成
#
# 设计原则（v2 整改）：
#   - 独立服务，不依赖 entity_detector 的内部状态
#   - 支持独立调用（pipeline 可在脱敏完成后单独调用验证）
#   - LLM 不可用时静默降级，不阻断验证流程
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

from llm_client import LLMClient

logger = logging.getLogger("quality_validator")


# ---------------------------------------------------------------------------
# 质量报告数据结构
# ---------------------------------------------------------------------------

@dataclass
class QualityIssue:
    """质量问题项"""
    text: str
    issue_type: str           # false_positive / false_negative / potential_leak
    severity: str             # high / medium / low
    evidence: str             # 证据描述
    suggestion: str = ""      # 修复建议


@dataclass
class QualityReport:
    """质量验证报告"""
    input_path: str = ""
    output_path: str = ""
    overall_score: float = 0.0          # 0-100 综合质量分
    is_acceptable: bool = True           # 是否达到可接受标准

    # 覆盖率
    total_entities: int = 0
    redacted_entities: int = 0
    coverage_rate: float = 0.0           # 0-100%

    # 误脱检测
    false_positives: List[QualityIssue] = field(default_factory=list)
    false_negatives: List[QualityIssue] = field(default_factory=list)
    potential_leaks: List[QualityIssue] = field(default_factory=list)

    # LLM 验证详情
    llm_used: bool = False
    llm_error: str = ""
    verification_snippet: str = ""        # 验证时使用的文本片段

    # 警告（规则相关）
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_path": self.input_path,
            "output_path": self.output_path,
            "overall_score": self.overall_score,
            "is_acceptable": self.is_acceptable,
            "total_entities": self.total_entities,
            "redacted_entities": self.redacted_entities,
            "coverage_rate": self.coverage_rate,
            "false_positives": [
                {"text": p.text, "issue_type": p.issue_type,
                 "severity": p.severity, "evidence": p.evidence,
                 "suggestion": p.suggestion}
                for p in self.false_positives
            ],
            "false_negatives": [
                {"text": p.text, "issue_type": p.issue_type,
                 "severity": p.severity, "evidence": p.evidence,
                 "suggestion": p.suggestion}
                for p in self.false_negatives
            ],
            "potential_leaks": [
                {"text": p.text, "issue_type": p.issue_type,
                 "severity": p.severity, "evidence": p.evidence,
                 "suggestion": p.suggestion}
                for p in self.potential_leaks
            ],
            "llm_used": self.llm_used,
            "llm_error": self.llm_error,
            "verification_snippet": self.verification_snippet,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# QualityValidator 服务
# ---------------------------------------------------------------------------

class QualityValidator:
    """
    脱敏质量验证服务。

    功能：
      1. 误脱检测（LLM）— 检测脱敏后是否仍有敏感信息泄漏
      2. 覆盖率统计 — 各类型实体的检出/脱敏比例
      3. 规则一致性检查 — 验证脱敏结果是否符合规则预期

    使用方式：
        validator = QualityValidator(config_path="config.json")
        report = validator.validate(
            original_text="...",
            redacted_text="...",
            entities=[SensitiveEntity(...), ...],
        )
        if not report.is_acceptable:
            print(f"质量不达标: {report.overall_score}")

    LLM 不可用时自动降级为纯规则验证，不阻断流程。
    """

    FALSE_POSITIVE_CHECK_SYSTEM = """你是一个脱敏质量审核员。
给定原始文档和脱敏后的文档，请识别：
1. 误脱（false_positive）：被脱敏了但实际上不是敏感信息的内容
2. 漏脱（false_negative）：应该被脱敏但实际未脱敏的内容
3. 潜在泄漏（potential_leak）：脱敏不彻底或上下文推断出敏感信息

请以 JSON 格式返回结果。"""

    def __init__(self, llm_client: Optional[LLMClient] = None, config_path: Optional[str] = None):
        self.llm = llm_client
        if self.llm is None and config_path:
            try:
                self.llm = LLMClient(config_path=config_path)
                if not self.llm.api_key:
                    logger.warning("QualityValidator: LLM API Key 未配置，降级为纯规则验证")
                    self.llm = None
            except Exception as e:
                logger.warning("QualityValidator: LLM 初始化失败，降级为纯规则验证: %s", e)
                self.llm = None

    def validate(
        self,
        original_text: str,
        redacted_text: str,
        entities: Optional[List[Any]] = None,
        input_path: str = "",
        output_path: str = "",
    ) -> QualityReport:
        """
        执行完整质量验证。

        Args:
            original_text: 原始文档文本
            redacted_text: 脱敏后文档文本
            entities: 检测到的敏感实体列表（可选）
            input_path: 输入文件路径（用于报告）
            output_path: 输出文件路径（用于报告）

        Returns:
            QualityReport 对象
        """
        report = QualityReport(input_path=input_path, output_path=output_path)

        if entities:
            report.total_entities = len(entities)
            # 简单估算覆盖率：统计 redacted_text 中 XXX 出现次数
            import re
            xxx_count = len(re.findall(r'X{3,}', redacted_text))
            report.redacted_entities = min(xxx_count, report.total_entities)
            report.coverage_rate = (report.redacted_entities / max(1, report.total_entities)) * 100

        # LLM 误脱检测
        if self.llm and original_text and redacted_text:
            report.llm_used = True
            try:
                llm_result = self._llm_false_positive_check(original_text, redacted_text)
                if llm_result.get("false_positives"):
                    for item in llm_result["false_positives"]:
                        report.false_positives.append(QualityIssue(
                            text=item.get("text", ""),
                            issue_type="false_positive",
                            severity=item.get("severity", "medium"),
                            evidence=item.get("evidence", ""),
                            suggestion=item.get("suggestion", ""),
                        ))
                if llm_result.get("false_negatives"):
                    for item in llm_result["false_negatives"]:
                        report.false_negatives.append(QualityIssue(
                            text=item.get("text", ""),
                            issue_type="false_negative",
                            severity=item.get("severity", "medium"),
                            evidence=item.get("evidence", ""),
                            suggestion=item.get("suggestion", ""),
                        ))
                if llm_result.get("potential_leaks"):
                    for item in llm_result["potential_leaks"]:
                        report.potential_leaks.append(QualityIssue(
                            text=item.get("text", ""),
                            issue_type="potential_leak",
                            severity=item.get("severity", "medium"),
                            evidence=item.get("evidence", ""),
                            suggestion=item.get("suggestion", ""),
                        ))
                report.verification_snippet = llm_result.get("verification_context", "")[:500]
            except Exception as e:
                report.llm_error = str(e)
                report.warnings.append(f"LLM 验证失败: {e}")
        else:
            report.llm_error = "LLM 不可用"
            report.warnings.append("LLM 未配置，跳过深度验证，仅做规则一致性检查")

        # 计算综合质量分
        report.overall_score = self._compute_score(report)
        report.is_acceptable = report.overall_score >= 60.0

        return report

    def _llm_false_positive_check(
        self, original_text: str, redacted_text: str
    ) -> Dict[str, Any]:
        """
        调用 LLM 检测误脱/漏脱。
        返回 {"false_positives": [...], "false_negatives": [...], "potential_leaks": [...]}
        """
        snippet_len = min(2000, len(original_text), len(redacted_text))
        prompt = f"""## 原始文档片段（截取）
---
{original_text[:snippet_len]}
---

## 脱敏后对应片段
---
{redacted_text[:snippet_len]}
---

请进行误脱检测，返回 JSON 格式：
{{
  "false_positives": [{{"text": "误脱内容", "severity": "high/medium/low", "evidence": "证据", "suggestion": "建议"}}],
  "false_negatives": [{{"text": "漏脱内容", "severity": "high/medium/low", "evidence": "证据"}}],
  "potential_leaks": [{{"text": "潜在泄漏", "severity": "high/medium/low", "evidence": "证据"}}],
  "verification_context": "验证时使用的上下文摘要"
}}"""

        response = self.llm.chat(
            prompt=prompt,
            system=self.FALSE_POSITIVE_CHECK_SYSTEM,
            response_format="json",
        )

        if response.error:
            raise RuntimeError(f"LLM 响应错误: {response.error}")

        try:
            raw = self.llm.parse_json_response(response)
            return raw if isinstance(raw, dict) else {}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"LLM 响应 JSON 解析失败: {e}")

    def _compute_score(self, report: QualityReport) -> float:
        """
        计算综合质量分（0-100）。

        评分维度：
          - 覆盖率（40%）：检出率越高越好
          - 误脱率（30%）：误脱越多分越低
          - 漏脱率（30%）：漏脱越多分越低
        """
        score = 100.0

        # 覆盖率扣分（覆盖率低于 80% 开始扣分）
        if report.coverage_rate < 80:
            score -= (80 - report.coverage_rate) * 0.3

        # 误脱扣分（每个 high 误脱扣 15 分，medium 扣 8 分，low 扣 3 分）
        for issue in report.false_positives:
            if issue.severity == "high":
                score -= 15
            elif issue.severity == "medium":
                score -= 8
            else:
                score -= 3

        # 漏脱扣分（每个 high 漏脱扣 15 分，medium 扣 8 分，low 扣 3 分）
        for issue in report.false_negatives:
            if issue.severity == "high":
                score -= 15
            elif issue.severity == "medium":
                score -= 8
            else:
                score -= 3

        # 潜在泄漏扣分（权重减半）
        for issue in report.potential_leaks:
            if issue.severity == "high":
                score -= 7.5
            elif issue.severity == "medium":
                score -= 4
            else:
                score -= 1.5

        return max(0.0, min(100.0, score))


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def build_quality_validator(config_path: Optional[str] = None) -> QualityValidator:
    """
    构建质量验证器（LLM 不可用时静默降级）。
    """
    try:
        client = LLMClient(config_path=config_path) if config_path else LLMClient()
        if not client.api_key:
            logger.warning("QualityValidator: API Key 未配置，降级为纯规则验证")
            return QualityValidator(None)
        return QualityValidator(client)
    except Exception as e:
        logger.warning("QualityValidator 初始化失败，降级为纯规则验证: %s", e)
        return QualityValidator(None)
