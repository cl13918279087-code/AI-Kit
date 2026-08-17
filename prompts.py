#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# prompts.py - LLM Prompt 模板
# doc-redact-project / v1.0.0
# ---------------------------------------------------------------------------

ENTITY_EXTRACTION_SYSTEM_PROMPT = """你是一个金融文档脱敏专家，只识别文档中的人名和银行名称。

【只做以下脱敏，其他一律不处理】
- 人员姓名 → "XXX"（不可逆匿名）
- 银行名称 → "XX银行"（通用占位）

【严格禁止】
- 不要脱敏：系统名、项目名、产品名、机构名、部门名、职务名
- 不要脱敏：日期、金额、账号、手机号、地址（regex 层会处理）
- 不要误脱正常业务术语（如"内部试营业"、"问题处理"、"业务处理"等）

【判断标准】
- 人名：必须是人的姓氏+名字组合，或单独姓氏+常见名字用字
- 银行名：必须是"XX银行"、"XX总行"、"XX分行"等完整银行机构名
- 宁可漏脱，不可误脱

【输出格式】
{
  "entities": [
    {
      "text": "原始文本",
      "replacement": "脱敏后文本",
      "category": "person_name | bank_name",
      "confidence": 0.0-1.0
    }
  ]
}

如果文本不含人名或银行名，返回空的 entities 数组：{"entities": []}"""


FALSE_POSITIVE_CHECK_SYSTEM = """你是一个金融文档脱敏质量审核员。

给定原始文档和脱敏后文档，检查脱敏结果是否有以下问题：
1. 误脱：把非敏感信息错误地替换了
2. 漏脱：敏感信息没有被发现和替换
3. 格式错误：替换后格式不正确

返回 JSON：
{
  "false_positives": [{"original": "...", "redacted": "...", "reason": "..."}],
  "missed": [{"text": "...", "category": "...", "reason": "..."}],
  "format_issues": [{"text": "...", "issue": "..."}],
  "overall_quality": "good | acceptable | needs_improvement"
}
"""


def build_entity_extraction_prompt(text: str, context: str = "") -> str:
    """构建实体提取 Prompt"""
    ctx = f"\n\n【上下文】（帮助理解文档背景）\n{context}" if context else ""
    return f"""请从以下金融文档片段中，识别所有**自然人姓名**（如张三、李四、王五等），用XXX替换。

重要规则：
1. 只识别**人名**，不识别机构名、地名、职位名、项目名
2. 银行名称（如"福建海峡银行"）、分行名、支行名、网点名 不要替换
3. "总行"、"我行"、"本行" 不要替换
4. "建设项目组"、"业务处理"、"问题处理"、"范围" 不要替换
5. 日期、账号、证件号、金额 不要替换

【待分析文本】
---
{text[:6000]}
---

只返回人名，不要返回其他类型的敏感信息。"""


def build_batch_summary_prompt(results: list) -> str:
    """批量处理完成后生成总结"""
    total = len(results)
    passed = sum(1 for r in results if r.get("status") == "success")
    failed = total - passed
    return f"""批量脱敏完成：

- 总文件数：{total}
- 成功：{passed}
- 失败：{failed}

请生成一份简短的总结报告。"""
