#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# entity_detector.py - 混合实体检测器（三层：LLM + Regex + 角色词）
# doc-redact-project / v1.0.0
#
# 检测层级：
#   Layer 1 (LLM)     : 智能理解上下文，发现规则无法覆盖的实体
#   Layer 2 (Regex)   : 兜底处理标准格式（日期/手机/身份证/账号等）
#   Layer 3 (RoleWord): 精确匹配上下文中的姓名和组织名
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import re
import logging
from typing import Optional, List, Dict, Any

from llm_client import LLMClient
from manifest import RedactionManifest, SensitiveEntity

logger = logging.getLogger("entity_detector")


# ---------------------------------------------------------------------------
# 角色词上下文（发现文档中具体人员姓名）
# ---------------------------------------------------------------------------

ROLE_WORD_PATTERNS = [
    # 人员角色
    re.compile(r'(?:总行|分行|支行)?(?:支持人员|联系人|负责人|行长|副行长|主管|经理|总监)[:：\s]*([\u4e00-\u9fa5]{2,4})'),
    re.compile(r'(?:牵头|主办|参加)(?:行|社|公司|单位)[:：\s]*([\u4e00-\u9fa5]{2,4})'),
    re.compile(r'(?:编写|审核|批准|签发)人[:：\s]*([\u4e00-\u9fa5]{2,4})'),
    re.compile(r'[\u4e00-\u9fa5]{1,2}(?:行|社|公司)长[:：]\s*([\u4e00-\u9fa5]{2,4})'),
    re.compile(r'(?:系统|项目)(?:负责人|经理|架构师|开发|测试)[:：\s]*([\u4e00-\u9fa5]{2,4})'),
]

# 排除词（角色词匹配中不视为姓名的词）
ROLE_EXCLUDED = {
    "项目组", "工作组", "牵头行", "主办行", "参加行",
    "总行", "分行", "支行", "联社", "工作组",
    "系统", "业务系统", "核心系统",
}


def _is_excluded_word(text: str) -> bool:
    return text in ROLE_EXCLUDED or len(text) < 2


# ---------------------------------------------------------------------------
# 日期范围正则（从 common_rules 移植，确保一致性）
# ---------------------------------------------------------------------------

def _build_date_range_patterns():
    year4_cn = r'[〇二三四五六七八九0-9]{4}'
    year4_ar = r'20[12][0-9]'
    month_pat = (
        r'(?:0?[1-9]|1[0-2]|'
        r'[一二三四五六七八九](?=月)|'
        r'十(?=月)|'
        r'十一(?=月)|十二(?=月))'
    )
    day_pat = r'[^月]+(?=日)'

    return [
        (re.compile(
            rf'({year4_cn}年{month_pat}{day_pat}日)'
            r'(至|至|——|——)'
            rf'({year4_cn}年{month_pat}{day_pat}日)'
        ), 0, 2),
        (re.compile(
            rf'({year4_ar}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))'
            r'(\s*(?:至|——|[-~])\s*)'
            rf'({year4_ar}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))'
        ), 0, 2),
        (re.compile(
            rf'({year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))'
            r'(\s*(?:至|——|[-~])\s*)'
            rf'({year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))'
        ), 0, 2),
    ]


_DATE_RANGE_PATTERNS = _build_date_range_patterns()


class EntityDetector:
    """
    混合实体检测器
    结合 LLM 智能识别 + Regex 规则兜底 + 角色词上下文
    """

    def __init__(self, llm_client: Optional[LLMClient], config: Optional[Dict] = None):
        self.llm = llm_client
        self.config = config or {}
        self.redaction_cfg = self.config.get("redaction", {})
        self.llm_enabled = self.redaction_cfg.get("llm_enabled", True)
        self.chunk_size = self.redaction_cfg.get("llm_chunk_size", 6000)
        self.overlap_chars = self.redaction_cfg.get("llm_overlap_chars", 200)

    def detect(self, text: str) -> RedactionManifest:
        """
        从文本内容检测所有敏感实体
        三路并行：LLM + Regex日期范围 + 角色词
        """
        manifest = RedactionManifest()
        manifest.total_characters = len(text)

        if not text or not text.strip():
            return manifest

        # Layer 1: LLM 检测
        if self.llm and self.llm_enabled:
            llm_entities = self._llm_detect(text)
            for entity in llm_entities:
                manifest.add_entity(entity)
            manifest.llm_calls += 1

        # Layer 2: Regex 日期范围兜底
        for entity in self._regex_detect_date_ranges(text):
            manifest.add_entity(entity)

        # Layer 3: 角色词姓名发现
        for entity in self._role_word_detect(text):
            manifest.add_entity(entity)

        return manifest

    def _llm_detect(self, text: str) -> List[SensitiveEntity]:
        """LLM 智能检测：分段并行调用，汇总结果"""
        if not self.llm:
            return []

        # ---------- 分批策略：每批约 200 字，10 批并行 ----------
        import threading, time
        BATCH_SIZE = 200  # 每批字数
        MAX_BATCHES = 10   # 最多并行批次数
        TIMEOUT_PER_BATCH = 60  # 每批超时（秒）

        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        batches = []
        current_batch = ""
        for para in paragraphs:
            if len(current_batch) + len(para) + 1 <= BATCH_SIZE:
                current_batch += ("\n" if current_batch else "") + para
            else:
                if current_batch:
                    batches.append(current_batch)
                current_batch = para
                if len(batches) >= MAX_BATCHES - 1:
                    break
        if current_batch and len(batches) < MAX_BATCHES:
            batches.append(current_batch)

        results = []
        errors = []

        def call_llm(batch_text, idx, result_list, error_list):
            try:
                raw = self.llm.detect_sensitive_entities(batch_text, context="")
                result_list.append((idx, raw))
            except Exception as e:
                error_list.append((idx, str(e)))

        threads = []
        result_holder = []
        error_holder = []

        for i, batch in enumerate(batches):
            t = threading.Thread(target=call_llm, args=(batch, i, result_holder, error_holder))
            t.daemon = True
            t.start()
            threads.append(t)

        # 等待所有线程，带单批超时
        overall_deadline = time.time() + TIMEOUT_PER_BATCH * len(batches)
        for t in threads:
            remaining = max(1, overall_deadline - time.time())
            t.join(timeout=min(remaining, TIMEOUT_PER_BATCH))

        # 解析结果
        entities = []
        for idx, raw in sorted(result_holder, key=lambda x: x[0]):
            for item in raw:
                text_val = (item.get("text") or "").strip()
                if not text_val:
                    continue
                entities.append(SensitiveEntity(
                    text=text_val,
                    replacement=item.get("replacement", "XXX"),
                    confidence=float(item.get("confidence", 0.80)),
                    source="llm",
                    category=item.get("category", "other"),
                    evidence=item.get("evidence", ""),
                ))

        if error_holder:
            logger.warning(f"LLM 分批调用失败: {error_holder}")

        return entities


    def _regex_detect_date_ranges(self, text: str) -> List[SensitiveEntity]:
        """Regex 兜底：日期范围（保留连接符）"""
        entities = []
        for pattern, start_group, end_group in _DATE_RANGE_PATTERNS:
            for m in pattern.finditer(text):
                full_match = m.group(0)
                start_date = m.group(start_group)
                end_date = m.group(end_group)

                # 替换为标准化占位符
                from common_rules import get_replacement
                repl_start = get_replacement("DATE_CHINESE", "YYYY年MM月DD日")
                repl_end = get_replacement("DATE", "YYYY/MM/DD")

                # 判断是中文还是阿拉伯数字格式
                if any(c >= '\u4e00' and c <= '\u9fff' for c in full_match):
                    replacement = f"{repl_start}{m.group(start_group+1)}{repl_end}"
                else:
                    replacement = f"{repl_start}{m.group(start_group+1)}{repl_end}"

                entities.append(SensitiveEntity(
                    text=full_match,
                    replacement=replacement,
                    confidence=0.95,
                    source="regex",
                    category="date",
                    evidence=f"日期范围正则匹配",
                ))
        return entities

    def _role_word_detect(self, text: str) -> List[SensitiveEntity]:
        """角色词上下文发现具体人员姓名"""
        entities = []
        for pattern in ROLE_WORD_PATTERNS:
            for m in pattern.finditer(text):
                name = m.group(1).strip()
                if _is_excluded_word(name):
                    continue
                # 排除纯数字/符号
                if re.match(r'^[\d\s\-]+$', name):
                    continue
                # 排除过长的（可能是整段话）
                if len(name) > 5:
                    continue
                entities.append(SensitiveEntity(
                    text=name,
                    replacement="XXX",
                    confidence=0.85,
                    source="role_word",
                    category="person_name",
                    evidence=f"角色词上下文: {m.group(0)[:30]}",
                ))
        return entities

    def _split_for_llm(self, text: str) -> List[Dict[str, str]]:
        """文本分段（带重叠，避免边界实体漏检）"""
        if len(text) <= self.chunk_size:
            return [{"text": text, "overlap_suffix": ""}]

        chunks = []
        start = 0
        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            overlap_suffix = text[end - self.overlap_chars:end] if end < len(text) else ""
            chunks.append({"text": text[start:end], "overlap_suffix": overlap_suffix})
            start = end - self.overlap_chars
            if start >= end:
                start = end
        return chunks if chunks else [{"text": text, "overlap_suffix": ""}]

    def verify(self, original_text: str, redacted_text: str) -> Dict[str, Any]:
        """脱敏质量验证（LLM 误脱检测）"""
        if not self.llm:
            return {"error": "LLM 未配置，跳过验证"}

        from prompts import FALSE_POSITIVE_CHECK_SYSTEM
        from llm_client import LLMResponse

        prompt = f"""## 原始文档片段\n---\n{original_text[:3000]}\n---\n\n## 脱敏后对应片段\n---\n{redacted_text[:3000]}\n---\n\n请进行误脱检测。"""

        response = self.llm.chat(
            prompt=prompt,
            system=FALSE_POSITIVE_CHECK_SYSTEM,
            response_format="json",
        )

        if response.error:
            return {"error": response.error}

        try:
            return self.llm.parse_json_response(response)
        except json.JSONDecodeError:
            return {"error": "验证响应解析失败"}


# ---------------------------------------------------------------------------
# 工厂函数：构建 LLM 增强检测器（LLM 不可用时静默降级）
# ---------------------------------------------------------------------------

def build_llm_detector(config_path: Optional[str] = None) -> EntityDetector:
    """
    构建混合实体检测器（LLM + Regex + 角色词 三层）。

    任何初始化失败（缺配置 / 无 API Key / 网络异常）都会静默降级为
    纯规则检测器，绝不抛异常——保证脱敏流程永远可用。
    """
    cfg = None
    try:
        if config_path:
            import json as _json
            with open(config_path, encoding="utf-8") as f:
                cfg = _json.load(f)
        client = LLMClient(config_path=config_path) if config_path else LLMClient()
        if not client.api_key:
            logger.warning("LLM API Key 未配置，降级为纯规则检测（LLM 层跳过）")
            print(f'  [DEBUG build_llm_detector] NO API KEY, returning EntityDetector(None, cfg)'); return EntityDetector(None, cfg)
        return EntityDetector(client, cfg)
    except Exception as e:
        logger.warning("LLM 检测器初始化失败，降级为纯规则检测: %s", e)
        return EntityDetector(None, cfg)
