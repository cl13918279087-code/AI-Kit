#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# prompts.py - LLM Prompt 模板
# doc-redact-project / v1.0.0
# ---------------------------------------------------------------------------

ENTITY_EXTRACTION_SYSTEM_PROMPT = """你是一个金融文档脱敏专家，专门识别文档中的敏感信息并生成安全的脱敏替换文本。

你的任务是：识别文本中的敏感实体，并给出脱敏替换结果。

【脱敏规则】
- 银行名称 → "XX银行"（通用占位）
- 人员姓名 → "XXX"（不可逆匿名）
- 身份证号 → "XXXXXXXXXXXXXXXXXX"（保留长度特征）
- 手机号 → "XXXXXXXXXXX"
- 银行卡号 → "XXXXXXXXXXXXXXXX"
- 邮箱 → "XXXXX@XXXXX"
- 日期 → "YYYY/MM/DD"（数字格式）或 "YYYY年MM月DD日"（中文格式）
- 地址 → "XX省XX市XX区XXXX"
- 金额 → "[金额]"
- 系统名/项目名/产品名 → "[系统名]" / "[项目名]"

【重要原则】
1. 只替换真正敏感的信息，不要误脱正常业务术语
2. 替换结果必须保持原文的格式结构（标点、空格、换行）
3. 如果无法判断，宁可保留也不要误脱
4. 金额只替换具体数字，保留单位（元/万/千等）
5. 银行名称如果是"中国工商银行"这类已知全称，直接替换为"XX银行"
6. 如果文本不含任何敏感信息，返回空的 entities 数组

【输出格式】
必须返回 JSON 格式：
{
  "entities": [
    {
      "text": "原始敏感文本",
      "replacement": "脱敏后文本",
      "category": "bank_name | person_name | id_card | phone | bank_card | email | date | address | amount | system_name | org_name | other",
      "confidence": 0.0-1.0,
      "evidence": "识别依据或上下文"
    }
  ]
}
"""


FALSE_POSITIVE_CHECK_SYSTEM = """你是一个金融文档脱敏质量审核员。

给定原始文档和脱敏后文档，检查脱敏结果是否有以下问题：
1. 误脱：把非敏感信息错误地替换了（如把正常业务术语当成银行名/姓名替换了）
2. 漏脱：敏感信息没有被发现和替换
3. 格式错误：替换后格式不正确（如日期格式不统一、金额单位丢失）

返回 JSON：
{
  "false_positives": [{"original": "...", "redacted": "...", "reason": "..."}],
  "missed": [{"text": "...", "category": "...", "reason": "..."}],
  "format_issues": [{"text": "...", "issue": "..."}],
  "overall_quality": "good | acceptable | needs_improvement"
}
"""


IMAGE_CONTENT_CHECK_PROMPT = """请检查这张图片内容是否包含以下敏感信息：
1. 银行名称/Logo
2. 人员姓名
3. 身份证号、手机号
4. 详细地址
5. 具体金额

图片内容：
{image_description}

如果包含敏感信息，返回：
{
  "has_sensitive": true/false,
  "sensitive_types": ["..."],
  "locations": [{"x1": 0, "y1": 0, "x2": 100, "y2": 50, "type": "..."}]
}
"""


FINAL_VERIFICATION_PROMPT = """对以下脱敏后的文档片段进行最终质量检查：

【原始片段】
{original}

【脱敏后片段】
{redacted}

请检查：
1. 是否还有残留的敏感信息？
2. 是否有明显的误脱？
3. 格式是否保持一致？

返回 JSON：
{
  "pass": true/false,
  "remaining_issues": ["..."],
  "suggestions": ["..."]
}
"""


def build_entity_extraction_prompt(text: str, context: str = "") -> str:
    """构建实体提取 Prompt"""
    ctx = f"\n\n【上下文】（帮助理解文档背景）\n{context}" if context else ""
    return f"""请分析以下金融文档片段，识别所有敏感信息：

【待分析文本】
---
{text[:6000]}
---
{ctx}

只处理这个片段中的内容，不要引入外部知识。"""


def build_batch_summary_prompt(results: list) -> str:
    """批量处理完成后生成总结"""
    total = len(results)
    passed = sum(1 for r in results if r.get("status") == "success")
    failed = total - passed
    return f"""批量脱敏完成：

- 总文件数：{total}
- 成功：{passed}
- 失败：{failed}

请生成一份简短的总结报告。
"""
