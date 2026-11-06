#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# degradation.py - 降级事件台账（架构评审 P0-3）
# ---------------------------------------------------------------------------
#
# 【背景】
#   架构评审 P0-3：`.doc` → `.docx` 有三级降级链（LibreOffice → textutil →
#   antiword），`redact_word.py` 内部已正确产出 `degraded_level` / `degraded_note`，
#   但**未透传**到 manifest / 批量报告 / 退出码。
#   后果：antiword 降级意味着**表格结构整体丢失**——表格里的姓名可能随结构一起
#   消失，产出物看似脱敏完成、实则内容残缺且无痕。这是典型的「静默失真」，
#   在档案归集环节极难发现。
#
# 【口径】
#   1. 降级事件在本模块统一登记（三处入口：脱敏处理器 / 人工检查清单 / LLM 侧），
#      保证「有降级必留痕」；
#   2. 降级分级（level）：
#        · info     —— 可选能力缺失，不影响产物完整性（如 LLM 未启用）
#        · warn     —— 产物仍完成脱敏，但**保真度下降**（如 textutil 转换、
#                      局部图片/嵌入对象需人工复核）
#        · critical —— 产物**内容可能失真或缺失**，必须人工复核
#                      （如 antiword 转换丢失表格结构、无转换器直接复制原文件）
#   3. 登记内容**只含组件名、降级级别、原因与人工复核提示，不含任何文档原文**；
#   4. 审计链三处同时呈现：manifest.json（`degradations` 段）、
#      批量报告（「⚠️ 降级产物清单」章节）、退出码（2 = 存在降级产物）。
#
# 【跨进程】
#   批量处理使用 ProcessPoolExecutor（子进程），本台账为**进程内**状态。
#   因此调用方必须在**子进程内、处理刚结束时**读取（见 pipeline._process_*），
#   再随 RedactResult 回传父进程汇总——不得在父进程直接查询。
# ---------------------------------------------------------------------------

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 降级级别（由轻到重）
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"

#: 级别排序权重（用于取「最重级别」）
LEVEL_WEIGHT = {LEVEL_INFO: 1, LEVEL_WARN: 2, LEVEL_CRITICAL: 3}

#: 级别中文名（报告/manifest 用）
LEVEL_LABEL = {
    LEVEL_INFO: "提示",
    LEVEL_WARN: "告警",
    LEVEL_CRITICAL: "严重",
}

#: 进程内降级事件台账
_LEDGER: List[Dict[str, Any]] = []


def record(component: str, level: str, detail: str = "",
           note: str = "") -> Dict[str, Any]:
    """登记一条降级事件，返回该事件记录。

    参数
        component : 降级组件名（如 "doc_converter" / "manual_check" / "llm"）
        level     : LEVEL_INFO / LEVEL_WARN / LEVEL_CRITICAL
        detail    : 降级细节（如 "textutil" / "图片遮盖失败"）
        note      : 人工复核提示（**不得含文档原文**）
    """
    lvl = level if level in LEVEL_WEIGHT else LEVEL_INFO
    event = {
        "component": str(component),
        "level": lvl,
        "detail": str(detail or ""),
        "note": str(note or ""),
    }
    _LEDGER.append(event)
    return event


def get_degradations() -> List[Dict[str, Any]]:
    """读取当前进程的降级事件（副本）。"""
    return [dict(e) for e in _LEDGER]


def clear_degradations() -> None:
    """清空台账（每次处理开始时调用，避免跨文件串味）。"""
    _LEDGER.clear()


def worst_level(events: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """取事件集合中最重的级别；无事件返回 None。"""
    evs = _LEDGER if events is None else events
    if not evs:
        return None
    return max((e.get("level", LEVEL_INFO) for e in evs),
               key=lambda lv: LEVEL_WEIGHT.get(lv, 1))


def has_critical(events: Optional[List[Dict[str, Any]]] = None) -> bool:
    """是否存在 critical 级降级（产物内容可能失真，必须人工复核）。"""
    return any(e.get("level") == LEVEL_CRITICAL
               for e in (_LEDGER if events is None else events))


def summarize(events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """降级事件汇总（供 manifest / 报告使用，不含原文）。"""
    evs = list(_LEDGER if events is None else events)
    by_level: Dict[str, int] = {}
    for e in evs:
        by_level[e.get("level", LEVEL_INFO)] = by_level.get(e.get("level", LEVEL_INFO), 0) + 1
    return {
        "count": len(evs),
        "by_level": by_level,
        "worst_level": worst_level(evs),
        "events": [dict(e) for e in evs],
    }
