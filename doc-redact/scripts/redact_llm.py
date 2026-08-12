#!/usr/bin/env python3
"""
redact_llm.py - LLM 语义增强脱敏模块
=====================================

当正则规则无法判断时（如"刘主任"是人名还是职务、"运营"是组织还是动词），
调用 LLM 进行语义分析，识别真实实体类型，输出精确脱敏指令。

使用方式：
    from redact_llm import LLMRedactor
    redactor = LLMRedactor(api_key=os.environ.get("OPENAI_API_KEY"))
    replacements = redactor.analyze_text("郑州银行总体组刘主任负责运营工作")
    # → [
    #     ("郑州银行", "XX银行", "银行名"),
    #     ("总体组",   None,     "组织名-保留"),   # None=不脱敏
    #     ("刘主任",   "XXX",    "人名"),
    #     ("运营",     "XXXX",   "组织名"),
    #   ]

依赖：openai >= 1.0
安装：pip install openai
"""

import os
import re
import json
import http.client
from typing import List, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# LLM Prompt 模板
# ---------------------------------------------------------------------------

ENTITY_EXTRACT_PROMPT = """你是一个金融文档敏感信息识别专家。请分析以下文本，识别其中的敏感实体。

**识别类别：**
1. 人名 - 任何人的真实姓名（2-4个汉字）
2. 银行名称 - 银行全称或简称
3. 身份证号、银行卡号、手机号、邮箱 - 精确匹配
4. 日期信息 - 各种格式的日期
5. 组织名称（保留不脱敏）- 项目组名、部门名、公司名称中的组织词尾（组/部/公司/运营/分行/支行等），如"总体组"、"需求组"、"运营部"、"咨询公司"
6. 职务名称（保留不脱敏）- 如"主任"、"经理"、"行长"等单独出现的职务

**输出格式（严格JSON数组）：**
[
  {{"text": "原始文本", "replacement": "替换文本或null", "reason": "识别理由", "category": "类别"}}
]

**规则：**
- replacement 为 null 表示不脱敏（保留原文本）
- 人名统一替换为 XXX
- 银行名统一替换为 XX银行
- 组织名（X组/X部/X公司等）→ null（不脱敏）
- 职务单独出现（非姓名组合）→ null（不脱敏）

分析文本：
---
{text}
---
"""


# ---------------------------------------------------------------------------
# LLM 调用（轻量实现，支持 OpenAI 兼容接口）
# ---------------------------------------------------------------------------

def _call_llm(prompt: str, api_key: str, base_url: str = "https://api.openai.com") -> str:
    """调用 OpenAI 兼容 API，返回响应文本"""
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 环境变量未设置")

    # 判断使用的是 OpenAI 还是兼容接口
    if "api.openai.com" in base_url:
        conn = http.client.HTTPSConnection("api.openai.com", timeout=30)
        path = "/v1/chat/completions"
        payload = json.dumps({
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 2048,
        }).encode()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    else:
        # 通用 OpenAI 兼容接口（如硅基流动、火山引擎等）
        parsed = http.client.urlparse(base_url)
        conn = http.client.HTTPSConnection(parsed.netloc, timeout=60)
        path = "/v1/chat/completions"
        payload = json.dumps({
            "model": "deepseek-ai/DeepSeek-V2.5",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 2048,
        }).encode()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    conn.request("POST", path, body=payload, headers=headers)
    resp = conn.getresponse()
    if resp.status != 200:
        raise RuntimeError(f"LLM API 调用失败: HTTP {resp.status} {resp.read().decode()[:200]}")
    data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 实体解析
# ---------------------------------------------------------------------------

def _parse_llm_response(raw: str) -> List[Dict]:
    """从 LLM 响应中提取实体列表"""
    # 尝试提取 JSON 代码块
    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if code_block:
        raw = code_block.group(1)
    # 直接尝试解析
    try:
        entities = json.loads(raw)
        if isinstance(entities, list):
            return entities
    except json.JSONDecodeError:
        pass
    return []


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class LLMRedactor:
    """
    LLM 语义增强脱敏器。

    使用方法：
        redactor = LLMRedactor(api_key="sk-...")  # 或从环境变量
        result = redactor.analyze_text("刘主任负责总体组运营工作")

    也支持上下文管理器（自动关闭连接）：
        with LLMRedactor() as redactor:
            replacements = redactor.analyze_text(text)
    """

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
        self.base_url = base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com")
        if not self.api_key:
            raise RuntimeError(
                "请设置 OPENAI_API_KEY 或 LLM_API_KEY 环境变量，"
                "或直接传入 api_key 参数"
            )

    def analyze(self, text: str, max_chars: int = 3000) -> List[Dict]:
        """
        分析文本，返回实体列表。

        Args:
            text: 要分析的文本（自动截断到 max_chars）
            max_chars: 每次请求最大字符数（防止 token 溢出）

        Returns:
            List[Dict]，每个元素包含 text/replacement/reason/category
        """
        chunk = text[:max_chars]
        prompt = ENTITY_EXTRACT_PROMPT.format(text=chunk)
        raw = _call_llm(prompt, self.api_key, self.base_url)
        entities = _parse_llm_response(raw)
        return entities

    def apply_to_text(self, text: str) -> str:
        """
        对文本执行 LLM 语义脱敏，返回脱敏后的文本。

        注意：这是精确替换，对于未在 LLM 分析中识别出的内容，
        仍需配合正则规则（apply_redactions）兜底。
        """
        entities = self.analyze(text)
        result = text
        # 按原文本长度倒序替换，避免位置偏移
        for ent in sorted(entities, key=lambda e: -len(e.get("text", ""))):
            orig = ent.get("text", "")
            repl = ent.get("replacement")
            if orig and repl is not None and orig in result:
                result = result.replace(orig, repl)
        return result

    def extract_replacements(self, text: str) -> List[Tuple[str, str, str]]:
        """
        返回三元组列表：(原始文本, 替换文本, 类别)
        用于精细控制脱敏逻辑。
        """
        entities = self.analyze(text)
        return [
            (e["text"], e["replacement"], e.get("category", "未知"))
            for e in entities
            if e.get("replacement") is not None
        ]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# 命令行接口
# ---------------------------------------------------------------------------

def main():
    import sys
    if len(sys.argv) < 2:
        print("用法: python3 redact_llm.py <文本>")
        print("   或: python3 redact_llm.py --file <文件路径>")
        sys.exit(1)

    text = sys.argv[1]
    if text == "--file" and len(sys.argv) >= 3:
        with open(sys.argv[2], encoding="utf-8") as f:
            text = f.read()

    try:
        with LLMRedactor() as redactor:
            entities = redactor.analyze(text)
            print(f"\n检测到 {len(entities)} 个实体：\n")
            for e in entities:
                status = f"→ {e['replacement']}" if e.get("replacement") else "（保留）"
                print(f"  [{e.get('category','?')}] {e['text']!r} {status}")
                print(f"    理由：{e.get('reason', '-')}")
    except RuntimeError as ex:
        print(f"[错误] {ex}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
