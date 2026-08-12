#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# manifest.py - 脱敏清单与报告生成
# doc-redact-project / v1.0.0
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from pathlib import Path


@dataclass
class SensitiveEntity:
    """检测到的敏感实体"""
    text: str                          # 原始文本
    replacement: str                   # 替换结果
    category: str                      # 敏感类型
    confidence: float = 0.80           # 置信度 0-1
    source: str = "regex"              # 来源：regex / llm / role_word
    evidence: str = ""                 # 证据/上下文
    page: Optional[int] = None         # 页码（如有）
    line: Optional[int] = None         # 行号（如有）


@dataclass
class ImageSensitiveItem:
    """图片中的敏感项"""
    text: str
    replacement: str
    bbox: tuple           # (x1, y1, x2, y2)
    method: str           # mosaic / blur / black
    page: Optional[int] = None


@dataclass
class UnresolvedItem:
    """无法自动处理的项（需人工确认）"""
    text: str
    context: str          # 上下文
    reason: str           # 无法处理原因
    page: Optional[int] = None


@dataclass
class RejectedItem:
    """被判定为误报的项"""
    text: str
    reject_reason: str
    confidence: float = 0.0
    source: str = ""


@dataclass
class Location:
    """位置信息"""
    page: Optional[int] = None
    paragraph: Optional[int] = None
    line: Optional[int] = None
    xpath: Optional[str] = None


class RedactionManifest:
    """
    脱敏清单主类
    收集所有检测到的敏感项，生成报告
    """

    def __init__(self, filename: str = ""):
        self.filename = filename
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.entities: List[SensitiveEntity] = []
        self.image_items: List[ImageSensitiveItem] = []
        self.unresolved: List[UnresolvedItem] = []
        self.rejected: List[RejectedItem] = []
        self.stats: Dict[str, int] = {}
        self.total_characters = 0
        self.redacted_characters = 0
        self.file_size = 0
        self.llm_calls = 0

    def add_entity(self, entity: SensitiveEntity) -> None:
        self.entities.append(entity)

    def add_image_item(self, item: ImageSensitiveItem) -> None:
        self.image_items.append(item)

    def add_unresolved(self, item: UnresolvedItem) -> None:
        self.unresolved.append(item)

    def add_rejected(self, item: RejectedItem) -> None:
        self.rejected.append(item)

    def set_stats(self, **kwargs) -> None:
        self.stats.update(kwargs)

    def finalize(self) -> None:
        """汇总统计"""
        cat_count: Dict[str, int] = {}
        for e in self.entities:
            cat_count[e.category] = cat_count.get(e.category, 0) + 1
        for item in self.image_items:
            cat_count["图片脱敏"] = cat_count.get("图片脱敏", 0) + 1
        self.stats["categories"] = cat_count
        self.stats["total_entities"] = len(self.entities)
        self.stats["total_image_items"] = len(self.image_items)
        self.stats["total_unresolved"] = len(self.unresolved)
        self.stats["total_rejected"] = len(self.rejected)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "created_at": self.created_at,
            "entities": [asdict(e) for e in self.entities],
            "image_items": [asdict(i) for i in self.image_items],
            "unresolved": [asdict(u) for u in self.unresolved],
            "rejected": [asdict(r) for r in self.rejected],
            "stats": self.stats,
            "total_characters": self.total_characters,
            "redacted_characters": self.redacted_characters,
            "file_size": self.file_size,
            "llm_calls": self.llm_calls,
        }

    def to_markdown(self) -> str:
        """生成 Markdown 格式报告"""
        self.finalize()
        lines = [
            f"# 📋 文档脱敏报告",
            f"",
            f"**文件名**: `{self.filename}`",
            f"**生成时间**: {self.created_at}",
            f"**文件大小**: {self._format_size(self.file_size)}",
            f"**文档字符数**: {self.total_characters:,}",
            f"",
            f"---",
            f"",
            f"## 📊 脱敏统计",
            f"",
            f"| 敏感类型 | 数量 |",
            f"|----------|------|",
        ]

        cats = self.stats.get("categories", {})
        for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
            lines.append(f"| {cat} | {count} |")

        lines.extend([
            f"| **合计** | **{sum(cats.values())}** |",
            f"",
            f"| 指标 | 值 |",
            f"|------|---|",
            f"| LLM 调用次数 | {self.llm_calls} |",
            f"| 图片脱敏项 | {len(self.image_items)} |",
            f"| 待人工确认 | {len(self.unresolved)} |",
            f"| 误报排除 | {len(self.rejected)} |",
            f"| 脱敏后字符数 | {self.redacted_characters:,} |",
            f"",
        ])

        if self.entities:
            lines.extend([
                f"## 🔍 敏感实体详情",
                f"",
                f"| # | 原始文本 | 脱敏结果 | 类型 | 置信度 | 来源 |",
                f"|---|---------|---------|------|--------|------|",
            ])
            for i, e in enumerate(self.entities, 1):
                mask = e.text[:4] + "****" if len(e.text) > 4 else "****"
                lines.append(
                    f"| {i} | `{mask}` | `{e.replacement}` | "
                    f"{e.category} | {e.confidence:.0%} | {e.source} |"
                )
            lines.append("")

        if self.unresolved:
            lines.extend([
                f"## ⚠️ 待人工确认",
                f"",
            ])
            for i, u in enumerate(self.unresolved, 1):
                lines.append(
                    f"**{i}.** `{u.text[:20]}{'...' if len(u.text)>20 else ''}`  "
                    f"— {u.reason}  \n"
                    f"   上下文：...{u.context[:50]}..."
                )
            lines.append("")

        if self.rejected:
            lines.extend([
                f"## ✅ 误报排除项",
                f"",
            ])
            for r in self.rejected:
                lines.append(f"- ~~`{r.text[:20]}`~~ — {r.reject_reason}")
            lines.append("")

        lines.extend([
            f"---",
            f"",
            f"*本报告由 doc-redact-project 自动生成*",
        ])

        return "\n".join(lines)

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def save_json(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def save_markdown(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_markdown())
