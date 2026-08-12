#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# llm_client.py - LLM 调用封装（支持多模型可配置）
# doc-redact-project / v1.0.0
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import time
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from pathlib import Path

import requests

logger = logging.getLogger("llm_client")


@dataclass
class LLMResponse:
    content: str
    error: Optional[str] = None
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0


class LLMClient:
    """
    多模型 LLM 客户端（配置驱动）
    支持 minimax / openai / zhipu 等 provider
    """

    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            import json
            self.cfg = json.load(open(config_path, encoding="utf-8"))
        else:
            # 尝试加载项目 config.json
            for p in [
                Path(__file__).parent / "config.json",
                Path(__file__).parent.parent / "config.json",
            ]:
                if p.exists():
                    self.cfg = json.load(open(p, encoding="utf-8"))
                    break
            else:
                self.cfg = {}

        llm_cfg = self.cfg.get("llm", {})
        self.provider = llm_cfg.get("provider", "minimax")
        self.model = llm_cfg.get("model", "MiniMax-Text-01")
        self.base_url = llm_cfg.get("base_url", "https://api.minimaxi.com/v1").rstrip("/")
        api_key = llm_cfg.get("api_key", "")
        if api_key and api_key.startswith("${"):
            import os
            env_var = api_key[2:-1]
            api_key = os.environ.get(env_var, "")
        self.api_key = api_key
        self.timeout = llm_cfg.get("timeout", 120)
        self.max_retries = llm_cfg.get("max_retries", 3)
        self.fallback_models = llm_cfg.get("fallback_models", [])

    @property
    def headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.provider == "minimax":
            h["Authorization"] = f"Bearer {self.api_key}"
        elif self.provider == "openai":
            h["Authorization"] = f"Bearer {self.api_key}"
        elif self.provider == "zhipu":
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def chat(
        self,
        prompt: str,
        system: str = "",
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        response_format: Optional[str] = None,
    ) -> LLMResponse:
        """
        通用 chat 接口
        """
        url = f"{self.base_url}/chat/completions"
        use_model = model or self.model

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        start = time.time()
        last_error = ""

        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    url,
                    headers=self.headers,
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()

                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                usage = data.get("usage", {})
                latency = int((time.time() - start) * 1000)

                return LLMResponse(
                    content=content,
                    model=use_model,
                    usage=usage,
                    latency_ms=latency,
                )

            except requests.exceptions.Timeout:
                last_error = f"请求超时（尝试 {attempt+1}/{self.max_retries}）"
                logger.warning(last_error)
            except requests.exceptions.HTTPError as e:
                last_error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
                logger.warning(last_error)
                if e.response.status_code == 429:
                    time.sleep(5 * (attempt + 1))
            except Exception as e:
                last_error = str(e)
                logger.warning(f"LLM 调用异常: {last_error}")

        return LLMResponse(
            content="",
            error=f"LLM 调用失败（{self.max_retries} 次重试后）: {last_error}",
            model=use_model,
        )

    def parse_json_response(self, response: LLMResponse) -> Dict[str, Any]:
        """解析 LLM 返回的 JSON 内容"""
        if response.error:
            raise ValueError(response.error)
        try:
            return json.loads(response.content)
        except json.JSONDecodeError:
            # 尝试提取 JSON 块
            import re
            m = re.search(r"\{[\s\S]*\}", response.content)
            if m:
                return json.loads(m.group(0))
            raise ValueError(f"JSON 解析失败: {response.content[:200]}")

    def detect_sensitive_entities(self, text: str, context: str = "") -> List[Dict[str, Any]]:
        """
        使用 LLM 检测敏感实体
        返回 [{"text": "...", "replacement": "...", "category": "...", "confidence": 0.x, "evidence": "..."}]
        """
        from prompts import build_entity_extraction_prompt, ENTITY_EXTRACTION_SYSTEM_PROMPT

        prompt = build_entity_extraction_prompt(text, context)
        response = self.chat(
            prompt=prompt,
            system=ENTITY_EXTRACTION_SYSTEM_PROMPT,
            response_format="json",
            temperature=0.05,
        )

        if response.error:
            logger.error(f"LLM 检测失败: {response.error}")
            return []

        try:
            result = self.parse_json_response(response)
            entities = result.get("entities", [])
            logger.info(f"LLM 检测到 {len(entities)} 个敏感实体")
            return entities
        except Exception as e:
            logger.error(f"LLM 响应解析失败: {e}")
            return []
