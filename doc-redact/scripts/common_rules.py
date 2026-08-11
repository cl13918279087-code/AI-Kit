#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# common_rules.py - 脱敏规则中心（所有脚本共享）
# ---------------------------------------------------------------------------
"""
统一管理所有文档格式脱敏的规则和常量。
所有 redact_*.py 脚本均从此模块导入，保持常量完全一致。

执行顺序（链式替换，防短文本误伤）：
  邮箱 → 地址 → 身份证 → 银行卡 → 日期 → 手机/固话 → 银行名称 → 人员姓名
"""

import re
from typing import List, Tuple

# ---------------------------------------------------------------------------
# 一、脱敏常量对照表（与 SKILL.md 完全一致）
# ---------------------------------------------------------------------------
REDACTIONS: List[Tuple[str, str]] = [
    # ① 电子邮箱
    ("EMAIL",          "XXXXX@XXXXX"),
    # ② 详细地址
    ("ADDRESS",        "XX省XX市XX区XXXX"),
    # ③ 身份证号
    ("ID_CARD",        "XXXXXXXXXXXXXXXXXX"),
    # ④ 银行卡号
    ("BANK_CARD",      "XXXXXXXXXXXXXXXX"),
    # ⑤ 日期信息（多种格式）
    ("DATE",           "YYYY/MM/DD"),
    # ⑥ 手机号码
    ("MOBILE",         "XXXXXXXXXXX"),
    # ⑦ 固定电话
    ("PHONE",          "0XX-XXXXXXXX"),
    # ⑧ 银行名称（含地方银行关键词 + 全国性银行）
    ("BANK",           "XX银行"),
    # ⑨ 人员姓名（最后执行，防止误伤）
    ("NAME",           "XXX"),
]

# ---------------------------------------------------------------------------
# 二、正则表达式（对应 REDACTIONS 顺序）
# ---------------------------------------------------------------------------
PATTERNS: List[Tuple[re.Pattern, str]] = [
    # ① 电子邮箱
    (re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
     "XXXXX@XXXXX"),

    # ② 详细地址（省+市+区+路/街/号）
    (re.compile(
        r'[^\x00-\xFF]{2,6}(?:省|自治区|市)?[^\x00-\xFF]{0,10}'
        r'(?:市|区)?[^\x00-\xFF]{0,10}'
        r'(?:街|路|道|巷|弄|号|大道|大街|东路|西路|南路|北路)[^\x00-\xFF]{0,30}'
    ), "XX省XX市XX区XXXX"),

    # ③ 身份证号（18位，含X校验位）
    (re.compile(
        r'\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b'
    ), "XXXXXXXXXXXXXXXXXX"),

    # ④ 银行卡号（16~19位）
    (re.compile(r'\b(?:\d{16}|\d{17}|\d{18}|\d{19})\b'),
     "XXXXXXXXXXXXXXXX"),

    # ⑤ 日期（多种格式）
    #    2022/04/09  2022-04-09  2022年04月09日  2022年4月9日
    (re.compile(
        r'\b(?:19|20)\d{2}[-/年](?:0[1-9]|1[0-2])[-/月](?:0[1-9]|[12]\d|3[01])日?\b\s*'
        r'|(?:19|20)\d{2}年(?:0?[1-9]|1[0-2])月(?:0?[1-9]|[12]\d|3[01])日\b'
    ), "YYYY/MM/DD"),

    # ⑥ 手机号码
    (re.compile(r'\b1[3-9]\d{9}\b'), "XXXXXXXXXXX"),

    # ⑦ 固定电话
    (re.compile(r'0\d{2,3}[-\s]?\d{7,8}'), "0XX-XXXXXXXX"),

    # ⑧ 银行名称（全国性银行 + 地方银行关键词）
    #    匹配：XX银行、XX农村商业银行、XX信用社、XX人民银行 等
    (re.compile(
        r'(?:(?:中国|交通|招商|浦发|兴业|民生|华夏|平安|光大|广发|浙商|渤海|恒丰|'
        r'农业|建设|工商|南京|宁波|杭州|深圳|上海|北京|广州|郑州|重庆|天津|成都|西安|苏州|武汉|长沙|青岛|济南|大连|沈阳|哈尔滨|长春|石家庄|福州|厦门|南昌|合肥|昆明|贵阳|南宁|海口|太原|兰州|呼和浩特|乌鲁木齐)银行|'
        r'(?:农信社|信用社|农商银行|合作银行|人民银行|'
        # 地方银行关键词（海峡/农商/城商/村镇等）
        r'海峡|农商|城商|村镇|股份|合行))'
        r'(?:[^\x00-\xFF]{0,20}银行)?'
    ), "XX银行"),

    # ⑨ 人员姓名（最后执行；匹配2~4个汉字）
    #    排除含数字/字母的词，以及常见误匹配模式
    (re.compile(
        r'(?<![a-zA-Z0-9\u4e00-\u9fa5])'
        r'[\u4e00-\u9fa5]{2,4}'
        r'(?![a-zA-Z0-9\u4e00-\u9fa5])'
    ), "XXX"),
]


def apply_redactions(text: str) -> str:
    """
    对给定文本执行全套脱敏规则。
    返回脱敏后的新字符串（原字符串不变）。
    """
    if not text or not isinstance(text, str):
        return text
    result = text
    for pattern, replacement in PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def add_custom_replacement(old: str, new: str) -> None:
    """
    运行时追加自定义替换规则（用于特定项目的专有名词）。
    示例：add_custom_replacement("海峡银行", "XX银行")
    """
    PATTERNS.insert(-1, (re.compile(re.escape(old)), new))


# ---------------------------------------------------------------------------
# 三、各敏感类型的描述（用于日志输出）
# ---------------------------------------------------------------------------
REDACTION_LABELS = {
    "EMAIL":      "邮箱",
    "ADDRESS":     "地址",
    "ID_CARD":     "身份证",
    "BANK_CARD":   "银行卡",
    "DATE":        "日期",
    "MOBILE":      "手机",
    "PHONE":       "固话",
    "BANK":        "银行名",
    "NAME":        "姓名",
}


def count_redactions(text: str) -> dict:
    """统计各类敏感信息出现次数（不实际替换）"""
    if not text:
        return {}
    counts = {}
    for key, (pattern, _) in zip([r[0] for r in REDACTIONS], PATTERNS):
        found = pattern.findall(text)
        if found:
            counts[REDACTION_LABELS.get(key, key)] = len(found)
    return counts
