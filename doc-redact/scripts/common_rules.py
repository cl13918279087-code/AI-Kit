#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# common_rules.py - 脱敏规则中心（所有脚本共享）
# ---------------------------------------------------------------------------
"""
统一管理所有文档格式脱敏的规则和常量。
所有 redact_*.py 脚本均从此模块导入，保持常量完全一致。

执行顺序（链式替换，防短文本误伤）：
  邮箱 -> 地址 -> 身份证 -> 银行卡 -> 日期 -> 手机/固话 -> 银行名称 -> 组织名(组/部/公司等) -> 人员姓名
"""

import re
from typing import List, Tuple

# ---------------------------------------------------------------------------
# 一、脱敏常量对照表（与 SKILL.md 完全一致）
# ---------------------------------------------------------------------------
REDACTIONS: List[Tuple[str, str]] = [
    ("EMAIL",          "XXXXX@XXXXX"),
    ("ADDRESS",        "XX省XX市XX区XXXX"),
    ("ID_CARD",        "XXXXXXXXXXXXXXXXXX"),
    ("BANK_CARD",      "XXXXXXXXXXXXXXXX"),
    ("DATE",           "YYYY/MM/DD"),
    ("MOBILE",         "XXXXXXXXXXX"),
    ("PHONE",          "0XX-XXXXXXXX"),
    ("BANK",           "XX银行"),
    ("ORG",            "XXXX"),          # 组织名（组/部/公司等），在姓名之前处理
    ("NAME",           "XXX"),
]

# ---------------------------------------------------------------------------
# 二、正则表达式（对应 REDACTIONS 顺序）
# ---------------------------------------------------------------------------
PATTERNS: List[Tuple[re.Pattern, str]] = [
    # 1 电子邮箱
    (re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
     "XXXXX@XXXXX"),

    # 2 详细地址
    (re.compile(
        r'[^\x00-\xFF]{2,6}(?:省|自治区|市)?[^\x00-\xFF]{0,10}'
        r'(?:市|区)?[^\x00-\xFF]{0,10}'
        r'(?:街|路|道|巷|弄|号|大道|大街|东路|西路|南路|北路)[^\x00-\xFF]{0,30}'
    ), "XX省XX市XX区XXXX"),

    # 3 身份证号
    (re.compile(
        r'\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b'
    ), "XXXXXXXXXXXXXXXXXX"),

    # 4 银行卡号
    (re.compile(r'\b(?:\d{16}|\d{17}|\d{18}|\d{19})\b'),
     "XXXXXXXXXXXXXXXX"),

    # 5 日期
    (re.compile(
        r'\b(?:19|20)\d{2}[-/年](?:0[1-9]|1[0-2])[-/月](?:0[1-9]|[12]\d|3[01])日?\b\s*'
        r'|(?:19|20)\d{2}年(?:0?[1-9]|1[0-2])月(?:0?[1-9]|[12]\d|3[01])日\b'
    ), "YYYY/MM/DD"),

    # 6 手机号码
    (re.compile(r'\b1[3-9]\d{9}\b'), "XXXXXXXXXXX"),

    # 7 固定电话
    (re.compile(r'0\d{2,3}[-\s]?\d{7,8}'), "0XX-XXXXXXXX"),

    # 8 银行名称（银行全称前缀 → XX银行）
    #    策略：优先匹配较长银行名（全称如"海峡银行"），再匹配较短城市前缀（如"华夏"）
    #    使用非捕获组 + 贪婪匹配确保正确替换顺序
    (re.compile(
        r'(?:(?:中国|交通|招商|浦发|兴业|民生|华夏|平安|光大|广发|浙商|渤海|恒丰|'
        r'农业|建设|工商|南京|宁波|杭州|深圳|上海|北京|广州|郑州|重庆|天津|成都|西安|'
        r'苏州|武汉|长沙|青岛|济南|大连|沈阳|哈尔滨|长春|石家庄|福州|厦门|南昌|合肥|'
        r'昆明|贵阳|南宁|海口|太原|兰州|呼和浩特|乌鲁木齐)银行|'
        r'(?:农信社|信用社|农商银行|合作银行|人民银行|'
        r'海峡|农商|城商|村镇|股份|合行))'
        r'(?:[^\x00-\xFF]{0,20}银行)?'
    ), "XX银行"),

    # 9 组织名（关键：在姓名之前！）
    #    策略：仅在独立词边界（前面有分隔符）且前缀为纯汉字时替换
    #    (a) 前缀必须是2-4个纯汉字（非ASCII/数字）
    #    (b) 前面必须有分隔符（标点/空格/换行）或文本开头
    #    (c) 排除"有限公司"、"合作公司"——公司类型后缀，不应脱敏
    #    (d) 单独"公司"：前面需2+汉字，防止"作公司"误匹配
    (re.compile(
        r'(?:(?:(?:(?<=[^\x00-\xFF])|(?<=^))'
        r'(?!有限公司)(?!合作公司)(?!UAT)'
        r'[^\x00-\xFF]{2,4}'
        r'(?:组|部|科|室|处|中心|运营(?![部科]))))|'
        r'(?:(?:(?<=[^\x00-\xFF])|(?<=^))'
        r'(?!有限公司)(?!合作公司)'
        r'[^\x00-\xFF]{2,4}'
        r'(?:分行|支行|事业部))|'
        r'(?:(?:(?<=[^\x00-\xFF])|(?<=^))'
        r'(?!有限公司)(?!合作公司)'
        r'[^\x00-\xFF]{2,4}公司(?=公司))'
    ), "XXXX"),

    # 10 人员姓名（2~4个汉字，前后非字母数字）
    #    排除以"公司"/"司"结尾的情况，防止"有限公司"等被误识别为姓名
    (re.compile(
        r'(?<![a-zA-Z0-9\u4e00-\u9fa5])'
        r'[\u4e00-\u9fa5]{2,4}'
        r'(?!(?:公司|司))(?:(?![a-zA-Z0-9\u4e00-\u9fa5])|$)'
    ), "XXX"),
]


def apply_redactions(text: str) -> str:
    """
    执行链式脱敏替换。
    
    保护性替换策略：
      1. 先将"合作公司"/"有限公司"等常见误匹配短语临时替换为占位符
      2. 执行常规 PATTERNS 替换
      3. 恢复占位符（占位符本身不会被任何 pattern 匹配）
    """
    if not text or not isinstance(text, str):
        return text
    
    # 占位符（UUID，确保不会与原文冲突）
    _PROTECTED = {
        '\x00有XXXX': '有限公司',
        '\x00合作XXXX': '合作公司',
    }
    
    # Step 1: 保护易误匹配短语
    result = text
    for placeholder, original in _PROTECTED.items():
        result = result.replace(original, placeholder)
    
    # Step 2: 执行常规替换
    for pattern, replacement in PATTERNS:
        result = pattern.sub(replacement, result)
    
    # Step 3: 恢复被保护的短语
    for placeholder, original in _PROTECTED.items():
        result = result.replace(placeholder, original)
    
    return result


def add_custom_replacement(old: str, new: str) -> None:
    PATTERNS.insert(-1, (re.compile(re.escape(old)), new))


# ---------------------------------------------------------------------------
# 三、各敏感类型的描述
# ---------------------------------------------------------------------------
REDACTION_LABELS = {
    "EMAIL":    "邮箱",
    "ADDRESS":  "地址",
    "ID_CARD":  "身份证",
    "BANK_CARD":"银行卡",
    "DATE":     "日期",
    "MOBILE":   "手机",
    "PHONE":    "固话",
    "BANK":     "银行名",
    "ORG":      "组织名",
    "NAME":     "姓名",
}


def count_redactions(text: str) -> dict:
    if not text:
        return {}
    counts = {}
    for key, (pattern, _) in zip([r[0] for r in REDACTIONS], PATTERNS):
        found = pattern.findall(text)
        if found:
            counts[REDACTION_LABELS.get(key, key)] = len(found)
    return counts
