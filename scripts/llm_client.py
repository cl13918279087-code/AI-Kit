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

from llm_egress import check_egress, describe_policy

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

    【出网守卫】本客户端的 base_url **无公网默认值**，必须显式配置，
    且须通过 llm_egress.check_egress() 校验（白名单 / 内网地址）。
    未配置或校验不通过 → self.available=False，调用方降级纯规则检测，
    绝不发起任何外网请求。详见 llm_egress.py 与 config.json 的 llm 段。
    """

    def __init__(self, config_path: Optional[str] = None):
        import json as _json  # Python 3.9 compat: import inside function
        if config_path:
            self.cfg = _json.load(open(config_path, encoding="utf-8"))
        else:
            # 尝试加载项目 config.json
            for p in [
                Path(__file__).parent / "config.json",
                Path(__file__).parent.parent / "config.json",
            ]:
                if p.exists():
                    self.cfg = _json.load(open(p, encoding="utf-8"))
                    break
            else:
                self.cfg = {}

        llm_cfg = self.cfg.get("llm", {})
        self.provider = llm_cfg.get("provider", "openai-compatible")
        self.model = llm_cfg.get("model", "")
        # 【合规】无公网默认值——base_url 必须显式配置
        self.base_url = str(llm_cfg.get("base_url", "") or "").rstrip("/")
        api_key = llm_cfg.get("api_key", "")
        if api_key and api_key.startswith("${"):
            import os
            env_var = api_key[2:-1]
            api_key = os.environ.get(env_var, "")
        self.api_key = api_key
        self.timeout = llm_cfg.get("timeout", 120)
        self.max_retries = llm_cfg.get("max_retries", 3)
        self.fallback_models = llm_cfg.get("fallback_models", [])

        # ---- 出网守卫 ----
        self.enabled = bool(llm_cfg.get("enabled", False))
        self.allowed_hosts = list(llm_cfg.get("allowed_hosts", []) or [])
        self.require_private_network = bool(
            llm_cfg.get("require_private_network", True)
        )
        ok, reason = check_egress(
            self.base_url,
            self.allowed_hosts,
            self.require_private_network,
        )
        self.egress_allowed = ok
        self.egress_reason = reason
        # 无 api_key 亦不可用（LLM 侧鉴权缺失）
        self.available = bool(self.enabled and ok and self.api_key)
        if not self.available:
            if not self.enabled:
                logger.info("LLM 层未启用（config.llm.enabled=false），降级纯规则检测")
            elif not ok:
                logger.warning("LLM 出网端点校验未通过，已降级纯规则检测：%s", reason)
            else:
                logger.warning("LLM api_key 未配置，降级纯规则检测")

    @property
    def headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        # 内网推理网关统一走 OpenAI 兼容鉴权（Bearer）
        if self.provider in ("minimax", "openai", "zhipu", "openai-compatible"):
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
        # 【合规】端点未通过守卫 / 未启用 / 无鉴权 → 直接短路，不发任何请求
        if not self.enabled or not self.egress_allowed or not self.api_key:
            return LLMResponse(
                content="",
                error=(
                    f"LLM 调用被出网策略阻断："
                    f"{self.egress_reason}；策略 {describe_policy(self.allowed_hosts, self.require_private_network)}"
                ),
                model=self.model,
            )

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
