"""
LLM 客户端
LLM增强脱敏工具包 - Phase 1

支持多 LLM Provider：
- openai：OpenAI 兼容格式（可用于 Minimax、DashScope 等）
- mock：DEMO 模式，返回预设响应（无 API Key 时自动启用）
"""

from __future__ import annotations

import json
import logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger("llm_client")


# ============================================================
# DEMO 模拟数据（无 API Key 时使用）
# ============================================================

DEMO_MANIFEST_RESPONSES = [
    # 模拟福建海峡银行文档的 LLM 识别结果
    {
        "bank_names": [
            {"text": "福建海峡银行", "replacement": "福建XX银行", "confidence": 0.98,
             "category": "bank_name_full", "evidence": "银行全称"},
            {"text": "海峡银行", "replacement": "XX银行", "confidence": 0.97,
             "category": "bank_name_abbr", "evidence": "简称，前面有总行信息技术部"},
            {"text": "福州分行", "replacement": "XX分行", "confidence": 0.91,
             "category": "bank_branch", "evidence": "分行名称"},
            {"text": "漳州分行", "replacement": "XX分行", "confidence": 0.91,
             "category": "bank_branch", "evidence": "分行名称"},
            {"text": "温州分行", "replacement": "XX分行", "confidence": 0.89,
             "category": "bank_branch", "evidence": "分行名称"},
            {"text": "福州杨桥支行", "replacement": "XX支行", "confidence": 0.88,
             "category": "bank_branch", "evidence": "支行名称"},
            {"text": "宁德分行", "replacement": "XX分行", "confidence": 0.87,
             "category": "bank_branch", "evidence": "分行名称"},
            {"text": "龙岩新罗支行", "replacement": "XX支行", "confidence": 0.85,
             "category": "bank_branch", "evidence": "支行名称"},
        ],
        "persons": [
            {"text": "蔡昀煜", "replacement": "XXX", "confidence": 0.95,
             "category": "contact_person", "evidence": "联系人字段后"},
            {"text": "张力", "replacement": "XXX", "confidence": 0.92,
             "category": "support_staff", "evidence": "总行支持人员字段后"},
            {"text": "黄丽丹", "replacement": "XXX", "confidence": 0.95,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "王春山", "replacement": "XXX", "confidence": 0.95,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "林超", "replacement": "XXX", "confidence": 0.94,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "张翠娟", "replacement": "XXX", "confidence": 0.94,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "廖腾华", "replacement": "XXX", "confidence": 0.95,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "林皓", "replacement": "XXX", "confidence": 0.95,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "张俊杰", "replacement": "XXX", "confidence": 0.95,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "吴文静", "replacement": "XXX", "confidence": 0.94,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "林润", "replacement": "XXX", "confidence": 0.94,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "卓晖", "replacement": "XXX", "confidence": 0.93,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "陈卓", "replacement": "XXX", "confidence": 0.93,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "刘军", "replacement": "XXX", "confidence": 0.92,
             "category": "person", "evidence": "总行支持人员字段"},
            {"text": "黄飞洋", "replacement": "XXX", "confidence": 0.92,
             "category": "person", "evidence": "总行支持人员字段"},
        ],
        "dates": [
            {"text": "二〇二二年四月", "replacement": "YYYY年MM月", "confidence": 0.99,
             "category": "date_month_only", "evidence": "中文数字日期"},
            {"text": "2022年4月8日至2022年4月10日", "replacement": "YYYY年MM月DD日至YYYY年MM月DD日",
             "confidence": 0.99, "category": "date_full", "evidence": "日期范围"},
            {"text": "2022年3月31日", "replacement": "YYYY年MM月DD日", "confidence": 0.99,
             "category": "date_full", "evidence": "完整日期"},
            {"text": "2022年4月", "replacement": "YYYY年MM月", "confidence": 0.88,
             "category": "date_month_only", "evidence": "年月格式，在日期范围内被替换过"},
            {"text": "2022年3月", "replacement": "YYYY年MM月", "confidence": 0.85,
             "category": "date_month_only", "evidence": "年月格式"},
        ],
        "phone_numbers": [],
        "id_numbers": [],
        "accounts": [],
        "emails": [],
        "unresolved": [
            {"text": "福建海峡银行新核心建设项目组", "confidence": 0.55,
             "reason": "机构全称过长，脱敏为'XX银行新核心建设项目组'更合适"}
        ]
    }
]


@dataclass
class LLMResponse:
    """LLM 响应封装"""
    content: str
    raw: Optional[dict] = None
    error: Optional[str] = None
    model: str = ""
    usage: Optional[dict] = None


class LLMClient:
    """
    LLM 客户端，支持 OpenAI 兼容 API

    用法：
        client = LLMClient(config_path="config.json")
        response = client.chat("你好")
        print(response.content)
    """

    def __init__(self, config: dict):
        """
        Args:
            config: 从 config.json 加载的配置字典
        """
        llm_cfg = config.get("llm", {})
        self.provider = llm_cfg.get("provider", "openai")
        self.base_url = llm_cfg.get("base_url", "https://api.openai.com/v1")
        self.api_key = llm_cfg.get("api_key", "")
        self.model = llm_cfg.get("model", "gpt-4o-mini")
        self.temperature = llm_cfg.get("temperature", 0.0)
        self.max_tokens = llm_cfg.get("max_tokens", 8192)

        # 判断是否启用 Mock 模式
        self.mock_mode = not bool(self.api_key)
        if self.mock_mode:
            logger.warning("⚠️ 未配置 API Key，自动启用 DEMO_MOCK 模式")
            logger.warning("⚠️ DEMO_MOCK 模式返回预设数据，不调用真实 LLM")
            logger.warning("⚠️ 请在 config.json 中设置 llm.api_key 以启用真实 LLM")

    def chat(
        self,
        prompt: str,
        system: Optional[str] = None,
        response_format: str = "text",
    ) -> LLMResponse:
        """
        发送对话请求

        Args:
            prompt: 用户 prompt
            system: 系统 prompt（可选）
            response_format: "text" 或 "json"

        Returns:
            LLMResponse 对象
        """
        if self.mock_mode:
            return self._mock_response(prompt, system, response_format)

        return self._real_request(prompt, system, response_format)

    def _real_request(
        self,
        prompt: str,
        system: Optional[str],
        response_format: str,
    ) -> LLMResponse:
        """真实 API 请求"""
        import requests

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        req_body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        # MiniMax 等国内厂商不支持 response_format 参数，
        # 改用 prompt 指令约束 JSON 输出，不传 response_format
        if response_format == "json" and "minimaxi" not in self.base_url:
            req_body["response_format"] = {"type": "json_object"}

        try:
            resp = requests.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=req_body,
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()

            content = data["choices"][0]["message"]["content"]

            return LLMResponse(
                content=content,
                raw=data,
                model=data.get("model", self.model),
                usage=data.get("usage", {}),
            )
        except requests.exceptions.Timeout:
            return LLMResponse(
                content="", error="请求超时"
            )
        except requests.exceptions.RequestException as e:
            return LLMResponse(
                content="", error=f"请求失败: {e}"
            )
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            return LLMResponse(
                content="", error=f"响应解析错误: {e}"
            )

    def _mock_response(
        self,
        prompt: str,
        system: Optional[str],
        response_format: str,
    ) -> LLMResponse:
        """Mock 模式：返回预设数据"""
        import random, time

        # 模拟网络延迟
        time.sleep(random.uniform(0.5, 1.5))

        # 随机选择一个预设响应
        demo = DEMO_MANIFEST_RESPONSES[0]

        if response_format == "json":
            return LLMResponse(
                content=json.dumps(demo, ensure_ascii=False),
                raw=demo,
                model="DEMO_MOCK",
            )
        else:
            return LLMResponse(
                content=json.dumps(demo, ensure_ascii=False),
                raw=demo,
                model="DEMO_MOCK",
            )

    def parse_json_response(self, response: LLMResponse) -> dict:
        """解析 LLM JSON 响应，自动处理 Markdown 代码块"""
        if response.error:
            raise ValueError(f"LLM 请求失败: {response.error}")

        content = response.content.strip()

        # 去除 Markdown 代码块包装
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:])  # 去掉第一行 ```json
            if content.endswith("```"):
                content = content[:-3]  # 去掉最后一行 ```

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}")
            logger.error(f"原始内容（前200字）: {content[:200]}")
            raise
