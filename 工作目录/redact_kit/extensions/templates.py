"""
Phase 4: 行业模板
extensions/templates.py

垂直行业专用模板：
- 银行：支行/分行/村镇银行/农商银行/信用社
- 证券：基金/券商/交易所
- 保险：保险公司/代理人
- 政务：政府机构/街道办事处/居委会
- 通用：全行业通用术语
"""

from __future__ import annotations

import re
from typing import Optional
from manifest import SensitiveEntity, EntityCategory


# ============================================================
# 行业术语库
# ============================================================

class IndustryTermLibrary:
    """
    行业术语库

    每个行业有：
    - entity_keywords: 实体关键词（触发 LLM 检测的上下文）
    - known_entities: 已知实体字典（精确匹配）
    - replacement_rules: 替换规则
    """

    def __init__(self, industry: str = "bank"):
        self.industry = industry
        self._load_industry(industry)

    def _load_industry(self, industry: str):
        """加载对应行业术语库"""

        # ── 银行 ──────────────────────────────────────────
        if industry == "bank":
            self.entity_keywords = [
                # 角色词（触发姓名识别）
                "总行支持人员", "分行支持人员", "支行支持人员",
                "系统负责人", "业务负责人", "技术负责人",
                "技术经理", "业务经理", "项目经理",
                "客户经理", "账户经理", "风险经理",
                "联系人", "经办人", "复核人", "审批人",
                "运营主管", "柜员", "大堂经理",
                # 机构词（触发银行名识别）
                "主办行", "牵头行", "参加行", "代理行",
                "账户行", "清算行", "结算行",
            ]

            self.bank_suffixes = [
                "银行", "农商银行", "农村商业银行", "村镇银行",
                "信用社", "农村信用合作社", "合作银行",
                "分行", "支行", "营业部", "管理部",
                "数据中心", "科技部", "运营中心",
                "清算中心", "客户服务中心",
            ]

            self.known_banks = {
                # 福建海峡银行 — 所有形式统一替换为 XX银行（不加地域前缀）
                "海峡银行": "XX银行",
                "福建海峡银行": "XX银行",
                "福建海峡银行股份有限公司": "XX银行",
                "海峡行": "XX银行",
                "中国银行": "XX银行",
                "工商银行": "XX银行",
                "建设银行": "XX银行",
                "农业银行": "XX银行",
                "交通银行": "XX银行",
                "招商银行": "XX银行",
                "兴业银行": "XX银行",
                "民生银行": "XX银行",
                "浦发银行": "XX银行",
                "光大银行": "XX银行",
                "平安银行": "XX银行",
            }

            self.excluded_terms = {
                "客户经理", "项目经理", "产品经理", "部门经理", "总经理",
                "技术经理", "运营经理", "风险经理", "合规经理",
                "操作员", "管理员", "柜员", "大堂经理",
                "总行", "分行", "支行", "网点", "营业部", "管理部",
                "项目组", "工作组",
                # "海峡" 已从排除词移除（P1.4 合规修复）：
                # 上下文感知：地理含义（台湾海峡）排除，银行名触发替换
                "台湾海峡", "海峡两岸", "海峡地区",
                "系统", "业务系统", "核心系统", "外围系统",
                "流程", "业务流程", "审批流程",
                "演练", "业务演练", "切换演练", "灾备演练",
            }

        # ── 证券 ──────────────────────────────────────────
        elif industry == "securities":
            self.entity_keywords = [
                "基金经理", "投资经理", "研究分析师",
                "托管人", "基金管理人", "基金托管人",
                "合规负责人", "风控总监", "交易员",
                "联系人", "经办人",
            ]

            self.bank_suffixes = [
                "基金管理公司", "证券公司", "期货公司",
                "资产管理公司", "投资管理公司",
                "基金", "资产管理计划",
            ]

            self.known_banks = {
                "中信证券": "XX证券",
                "华泰证券": "XX证券",
                "国泰君安": "XX证券",
                "招商证券": "XX证券",
                "海通证券": "XX证券",
                "广发证券": "XX证券",
            }

            self.excluded_terms = {
                "基金经理", "投资经理", "研究分析师",
                "交易员", "风控总监",
                "系统", "基金", "流程",
            }

        # ── 保险 ──────────────────────────────────────────
        elif industry == "insurance":
            self.entity_keywords = [
                "保险公司", "保险代理人", "保险经纪人",
                "承保人", "理赔员", "核保人",
                "联系人", "经办人",
            ]

            self.bank_suffixes = [
                "保险公司", "保险经纪公司", "保险代理公司",
                "分公司", "支公司", "营业部",
            ]

            self.known_banks = {
                "中国人寿": "XX保险",
                "中国平安": "XX保险",
                "中国太保": "XX保险",
                "新华保险": "XX保险",
                "泰康保险": "XX保险",
                "人保财险": "XX保险",
            }

            self.excluded_terms = {
                "保险公司", "保险经纪人",
                "承保人", "理赔员",
                "系统", "流程",
            }

        # ── 政务 ──────────────────────────────────────────
        elif industry == "government":
            self.entity_keywords = [
                "局长", "处长", "科长", "主任",
                "书记", "副书记", "党组成员",
                "联络人", "经办人", "负责人",
            ]

            self.bank_suffixes = [
                "人民政府", "街道办事处", "居委会",
                "村委会", "镇政府", "区政府", "市政府",
                "公安局", "教育局", "民政局", "财政局",
                "卫生局", "人社局", "市场监管局",
            ]

            self.known_banks = {}

            self.excluded_terms = {
                "局长", "处长", "科长", "主任",
                "书记", "书记员", "系统", "流程",
            }

        # ── 通用 ──────────────────────────────────────────
        else:
            self.entity_keywords = [
                "联系人", "负责人", "经办人", "审批人",
            ]
            self.bank_suffixes = []
            self.known_banks = {}
            self.excluded_terms = {}

    def is_excluded(self, text: str) -> bool:
        """判断是否属于排除词（非敏感信息）"""
        return text in self.excluded_terms

    def get_bank_replacement(self, text: str) -> Optional[str]:
        """获取银行名替换值"""
        if text in self.known_banks:
            return self.known_banks[text]
        for known, replacement in self.known_banks.items():
            if known in text:
                return text.replace(known, replacement)
        return None

    def detect_bank_from_suffix(self, text: str) -> bool:
        """判断文本是否可能为银行名（通过后缀判断）"""
        for suffix in self.bank_suffixes:
            if text.endswith(suffix) or suffix in text:
                return True
        return False


# ============================================================
# 行业模板管理器
# ============================================================

class TemplateManager:
    """
    行业模板管理器

    管理各行业的脱敏模板，提供：
    1. 自动识别文档行业
    2. 加载对应术语库
    3. 生成行业专用 Prompt
    """

    INDUSTRIES = {
        "bank": "银行",
        "securities": "证券",
        "insurance": "保险",
        "government": "政务",
        "generic": "通用",
    }

    def __init__(self):
        self._libraries = {}

    def get_library(self, industry: str) -> IndustryTermLibrary:
        """获取行业术语库（懒加载）"""
        if industry not in self._libraries:
            self._libraries[industry] = IndustryTermLibrary(industry)
        return self._libraries[industry]

    def detect_industry(self, text: str) -> str:
        """
        自动识别文档行业

        基于关键词出现频率判断
        """
        scores = {}

        industry_keywords = {
            "bank": [
                "银行", "分行", "支行", "柜员", "账户",
                "存款", "贷款", "清算", "核心系统", "运营中心",
            ],
            "securities": [
                "基金", "证券", "资产管理", "托管人", "基金管理人",
                "净值", "份额", "申购", "赎回", "交易所",
            ],
            "insurance": [
                "保险", "保单", "承保", "理赔", "投保人",
                "受益人", "保费", "核保", "保险经纪人",
            ],
            "government": [
                "人民政府", "街道办事处", "居委会", "区政府",
                "局长", "处长", "办公室", "公章",
            ],
        }

        for industry, keywords in industry_keywords.items():
            score = sum(1 for kw in keywords if kw in text)
            scores[industry] = score

        if not scores or max(scores.values()) == 0:
            return "generic"

        best = max(scores, key=scores.get)
        logger.info(f"🏭 识别行业: {best} (分数: {scores[best]})")
        return best

    def build_industry_prompt(self, industry: str) -> str:
        """
        构建行业专用 System Prompt

        在基础 Prompt 之上，增加行业特定知识和术语
        """
        lib = self.get_library(industry)

        industry_specific = ""

        if industry == "bank":
            industry_specific = f"""
## 银行业特定知识

### 银行机构全称模式
- {{省/市}}{{银行名}}：如"福建海峡银行"、"中国建设银行"
- {{地区名}}{{银行类型}}：如"福州分行"、"漳州分行"、"温州分行"
- {{地区名}}{{银行名}}{{支行类型}}：如"福州杨桥支行"、"龙岩新罗支行"

### 常见银行简称（应替换为XX银行）
海峡银行、建设银行、农业银行、工商银行、中国银行、交通银行、
招商银行、兴业银行、民生银行、浦发银行、光大银行、平安银行、
华夏银行、中信银行、浙商银行、江苏银行、杭州银行、宁波银行、
厦门银行、厦门国际银行、泉州银行、三明农商银行、福州农商银行

### 常见排除词（不应替换）
客户经理、项目经理、产品经理、部门经理、总经理、
技术经理、运营经理、风险经理、合规经理、
总行、分行、支行、网点、营业部、管理部

### 替换规则（统一替换为XX银行，不保留地域前缀）
- 全称"福建海峡银行股份有限公司" → "XX银行"
- "福建海峡银行" → "XX银行"
- 简称"海峡银行" → "XX银行"
- 简称"海峡行" → "XX银行"
- 分行"福州分行" → "XX分行"
- 支行"福州杨桥支行" → "XX支行"
"""

        elif industry == "securities":
            industry_specific = """
## 证券行业特定知识

### 证券机构名称模式
- {{名称}}基金管理有限公司
- {{名称}}证券股份有限公司
- {{名称}}期货有限公司

### 常见排除词
基金经理、投资经理、研究分析师、交易员、风控总监
"""

        elif industry == "insurance":
            industry_specific = """
## 保险行业特定知识

### 保险公司名称模式
- {{名称}}人寿保险股份有限公司
- {{名称}}财产保险股份有限公司
- {{名称}}保险经纪有限公司

### 常见排除词
保险公司、保险经纪人、承保人、理赔员、核保人
"""

        return industry_specific
