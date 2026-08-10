"""
RedactionManifest 数据结构定义
LLM增强脱敏工具包 - Phase 1 核心数据结构
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum
from datetime import datetime


class EntityCategory(Enum):
    """敏感实体类别"""
    BANK_NAME_FULL = "bank_name_full"
    BANK_NAME_ABBR = "bank_name_abbr"
    BANK_BRANCH = "bank_branch"
    PERSON = "person"
    PERSON_CONTACT = "contact_person"
    PERSON_SUPPORT = "support_staff"
    DATE_FULL = "date_full"
    DATE_MONTH = "date_month_only"
    PHONE = "phone_number"
    ID_NUMBER = "id_number"
    ACCOUNT = "account"
    EMAIL = "email"
    ADDRESS = "address"


class Source(Enum):
    """实体来源"""
    LLM = "llm"
    REGEX = "regex"
    RULE = "rule"
    USER = "user"


class Confidence(Enum):
    """置信度等级"""
    HIGH = "high"      # >= 0.90
    MEDIUM = "medium"  # 0.70-0.90
    LOW = "low"        # < 0.70


class ImageAction(Enum):
    """图片处理动作"""
    MOSAIC = "mosaic"
    KEEP = "keep"
    BLUR = "blur"
    ANNOTATE = "annotate"


@dataclass
class Location:
    """实体在文档中的位置"""
    block_id: int
    type: str  # "paragraph" | "table"
    begin: int
    end: int
    context: str  # 上下文（前20字+实体+后20字）
    verified: bool = False  # 是否经规则引擎验证


@dataclass
class SensitiveEntity:
    """单个敏感实体"""
    text: str
    replacement: str
    locations: list[Location] = field(default_factory=list)
    confidence: float = 1.0
    source: str = "rule"
    category: str = "unknown"
    evidence: str = ""
    rejected: bool = False
    reject_reason: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["confidence_level"] = self._confidence_level()
        return d

    def _confidence_level(self) -> str:
        if self.confidence >= 0.90:
            return "HIGH"
        elif self.confidence >= 0.70:
            return "MEDIUM"
        return "LOW"


@dataclass
class ImageSensitiveItem:
    """图片内敏感项"""
    index: int
    image_type: str  # "header" | "footer" | "screenshot" | "logo" | "other"
    contains_sensitive: bool
    sensitive_items: list[str] = field(default_factory=list)
    action: str = "keep"
    confidence: float = 0.0
    source: str = "llm"
    ocr_text: str = ""


@dataclass
class UnresolvedItem:
    """无法自动处理的项（需要人工确认）"""
    text: str
    reason: str
    location: Optional[Location] = None
    suggestions: list[str] = field(default_factory=list)


@dataclass
class RejectedItem:
    """被过滤的误脱风险项"""
    text: str
    rejected_reason: str
    confidence: float
    source: str


@dataclass
class RedactionManifest:
    """
    完整脱敏清单 - LLM与规则引擎协作的核心数据结构

    工作流程：
    1. LLM 识别实体 → 构建 manifest
    2. 规则引擎验证 + 补充
    3. 置信度分级处理
    4. editor_sdk 执行替换
    5. LLM 质量验证
    """
    version: str = "1.0"
    document_name: str = ""
    document_path: str = ""
    total_blocks: int = 0
    generated_at: str = ""

    # 各类型实体
    bank_names: list[SensitiveEntity] = field(default_factory=list)
    persons: list[SensitiveEntity] = field(default_factory=list)
    dates: list[SensitiveEntity] = field(default_factory=list)
    phone_numbers: list[SensitiveEntity] = field(default_factory=list)
    id_numbers: list[SensitiveEntity] = field(default_factory=list)
    accounts: list[SensitiveEntity] = field(default_factory=list)
    emails: list[SensitiveEntity] = field(default_factory=list)
    addresses: list[SensitiveEntity] = field(default_factory=list)

    # 图片
    images: list[ImageSensitiveItem] = field(default_factory=list)

    # 无法自动处理的项
    unresolved: list[UnresolvedItem] = field(default_factory=list)
    # 过滤掉的误脱风险项
    rejected: list[RejectedItem] = field(default_factory=list)

    # 元数据
    llm_calls: int = 0
    regex_calls: int = 0
    total_entities_found: int = 0
    total_locations: int = 0

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now().isoformat()
        self._recalc_stats()

    def _recalc_stats(self):
        """重新计算统计信息"""
        all_entities = (
            self.bank_names + self.persons + self.dates +
            self.phone_numbers + self.id_numbers + self.accounts +
            self.emails + self.addresses
        )
        self.total_entities_found = len(all_entities)
        self.total_locations = sum(len(e.locations) for e in all_entities)

    def add_entity(self, entity: SensitiveEntity):
        """向对应类别添加实体（自动去重）"""
        category_map = {
            "bank_name_full": self.bank_names,
            "bank_name_abbr": self.bank_names,
            "bank_branch": self.bank_names,
            "contact_person": self.persons,
            "support_staff": self.persons,
            "person": self.persons,
            "date_full": self.dates,
            "date_month_only": self.dates,
            "phone_number": self.phone_numbers,
            "id_number": self.id_numbers,
            "account": self.accounts,
            "email": self.emails,
            "address": self.addresses,
        }
        bucket = category_map.get(entity.category, self.persons)

        # 去重：同文本 + 同替换值
        for existing in bucket:
            if existing.text == entity.text and existing.replacement == entity.replacement:
                # 合并 locations
                for loc in entity.locations:
                    if loc not in existing.locations:
                        existing.locations.append(loc)
                # 保留更高置信度
                if entity.confidence > existing.confidence:
                    existing.confidence = entity.confidence
                return

        bucket.append(entity)
        self._recalc_stats()

    def merge(self, other: RedactionManifest):
        """合并另一个 Manifest（用于分段处理结果合并）"""
        for entity_list_key in [
            "bank_names", "persons", "dates", "phone_numbers",
            "id_numbers", "accounts", "emails", "addresses"
        ]:
            our_list = getattr(self, entity_list_key)
            for entity in getattr(other, entity_list_key):
                # 去重合并
                found = False
                for existing in our_list:
                    if existing.text == entity.text and existing.replacement == entity.replacement:
                        for loc in entity.locations:
                            if loc not in existing.locations:
                                existing.locations.append(loc)
                        found = True
                        break
                if not found:
                    our_list.append(entity)

        self.images.extend(other.images)
        self.unresolved.extend(other.unresolved)
        self.rejected.extend(other.rejected)
        self.llm_calls += other.llm_calls
        self.regex_calls += other.regex_calls
        self._recalc_stats()

    def get_high_confidence(self) -> list[SensitiveEntity]:
        """获取高置信度实体（>=0.90，直接执行）"""
        return [e for e in self._all_entities() if e.confidence >= 0.90 and not e.rejected]

    def get_medium_confidence(self) -> list[SensitiveEntity]:
        """获取中置信度实体（0.70-0.90，规则验证）"""
        return [e for e in self._all_entities()
                if 0.70 <= e.confidence < 0.90 and not e.rejected]

    def get_low_confidence(self) -> list[SensitiveEntity]:
        """获取低置信度实体（<0.70，人工确认）"""
        return [e for e in self._all_entities() if e.confidence < 0.70 and not e.rejected]

    def _all_entities(self) -> list[SensitiveEntity]:
        return (
            self.bank_names + self.persons + self.dates +
            self.phone_numbers + self.id_numbers + self.accounts +
            self.emails + self.addresses
        )

    def to_dict(self) -> dict:
        """转换为可序列化字典"""
        result = {
            "version": self.version,
            "document_name": self.document_name,
            "document_path": self.document_path,
            "total_blocks": self.total_blocks,
            "generated_at": self.generated_at,
            "stats": {
                "llm_calls": self.llm_calls,
                "regex_calls": self.regex_calls,
                "total_entities": self.total_entities_found,
                "total_locations": self.total_locations,
                "high_confidence": len(self.get_high_confidence()),
                "medium_confidence": len(self.get_medium_confidence()),
                "low_confidence": len(self.get_low_confidence()),
            },
            "entities": {
                "bank_names": [e.to_dict() for e in self.bank_names],
                "persons": [e.to_dict() for e in self.persons],
                "dates": [e.to_dict() for e in self.dates],
                "phone_numbers": [e.to_dict() for e in self.phone_numbers],
                "id_numbers": [e.to_dict() for e in self.id_numbers],
                "accounts": [e.to_dict() for e in self.accounts],
                "emails": [e.to_dict() for e in self.emails],
                "addresses": [e.to_dict() for e in self.addresses],
            },
            "images": [asdict(img) for img in self.images],
            "unresolved": [asdict(u) for u in self.unresolved],
            "rejected": [asdict(r) for r in self.rejected],
        }
        return result

    def save_json(self, path: str):
        """保存为 JSON 文件"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load_json(cls, path: str) -> RedactionManifest:
        """从 JSON 文件加载"""
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return cls._from_dict(d)

    @classmethod
    def _from_dict(cls, d: dict) -> RedactionManifest:
        """从字典构造 Manifest"""
        manifest = cls(
            version=d.get("version", "1.0"),
            document_name=d.get("document_name", ""),
            document_path=d.get("document_path", ""),
            total_blocks=d.get("total_blocks", 0),
            generated_at=d.get("generated_at", ""),
            llm_calls=d.get("stats", {}).get("llm_calls", 0),
            regex_calls=d.get("stats", {}).get("regex_calls", 0),
        )

        entity_map = {
            "bank_names": "bank_names",
            "persons": "persons",
            "dates": "dates",
            "phone_numbers": "phone_numbers",
            "id_numbers": "id_numbers",
            "accounts": "accounts",
            "emails": "emails",
            "addresses": "addresses",
        }

        for key, attr in entity_map.items():
            for e_dict in d.get("entities", {}).get(key, []):
                locations = [Location(**loc) for loc in e_dict.get("locations", [])]
                entity = SensitiveEntity(
                    text=e_dict["text"],
                    replacement=e_dict["replacement"],
                    locations=locations,
                    confidence=e_dict.get("confidence", 1.0),
                    source=e_dict.get("source", "rule"),
                    category=e_dict.get("category", "unknown"),
                    evidence=e_dict.get("evidence", ""),
                    rejected=e_dict.get("rejected", False),
                    reject_reason=e_dict.get("reject_reason", ""),
                )
                getattr(manifest, attr).append(entity)

        for img_dict in d.get("images", []):
            manifest.images.append(ImageSensitiveItem(**img_dict))

        for u_dict in d.get("unresolved", []):
            manifest.unresolved.append(UnresolvedItem(
                text=u_dict["text"],
                reason=u_dict["reason"],
                suggestions=u_dict.get("suggestions", []),
            ))

        for r_dict in d.get("rejected", []):
            manifest.rejected.append(RejectedItem(**r_dict))

        manifest._recalc_stats()
        return manifest

    def summary(self) -> str:
        """生成可读摘要"""
        lines = [
            f"脱敏清单摘要 - {self.document_name}",
            f"生成时间: {self.generated_at}",
            "",
            "【统计】",
            f"  LLM 调用次数: {self.llm_calls}",
            f"  正则调用次数: {self.regex_calls}",
            f"  实体总数: {self.total_entities_found}",
            f"  位置总数: {self.total_locations}",
            "",
            "【高置信（直接执行）】",
            f"  银行名: {len([e for e in self.bank_names if e.confidence >= 0.90])}",
            f"  姓名: {len([e for e in self.persons if e.confidence >= 0.90])}",
            f"  日期: {len([e for e in self.dates if e.confidence >= 0.90])}",
            "",
            "【需人工确认】",
            f"  低置信实体: {len(self.get_low_confidence())}",
            f"  无法自动处理: {len(self.unresolved)}",
            f"  误脱过滤: {len(self.rejected)}",
        ]
        return "\n".join(lines)
