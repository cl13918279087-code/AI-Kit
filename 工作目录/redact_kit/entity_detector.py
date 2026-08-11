"""
混合实体检测器
LLM增强脱敏工具包 - Phase 1

三层检测：
1. LLM 层：智能理解上下文，发现规则无法覆盖的实体
2. Regex 层：兜底处理标准格式（日期/手机/身份证/账号）
3. 角色词规则：精确匹配已知的姓名库和机构名
"""

from __future__ import annotations

import json
import re
import logging
from typing import Optional
from manifest import (
    RedactionManifest, SensitiveEntity, Location,
    ImageSensitiveItem, UnresolvedItem, RejectedItem
)
from llm_client import LLMClient
# 多语言扩展（lazy import，避免循环依赖）
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from extensions.multi_lang import MultiLangDetector
from prompts import (
    build_entity_extraction_prompt,
    ENTITY_EXTRACTION_SYSTEM_PROMPT,
    FALSE_POSITIVE_CHECK_SYSTEM,
    IMAGE_CONTENT_CHECK_PROMPT,
    FINAL_VERIFICATION_PROMPT,
)

logger = logging.getLogger("entity_detector")


# ============================================================
# 正则规则（Phase 2 会补充更完整的规则）
# ============================================================

# 日期正则（已修复版，避免十月匹配失败）
_MONTH_PAT = (
    r'(?:'
    r'0?[1-9]|1[0-2]|'
    r'[一二三四五六七八九](?=月)|'
    r'十(?=月)|'
    r'十一(?=月)|十二(?=月)|'
    r'正(?=月)'
    r')'
)
_DAY_PAT = r'[^月]+(?=日)'
_YEAR4_CN = r'[〇二三四五六七八九0-9]{4}'
_YEAR4_AR = r'20[12][0-9]'

# 日期范围/周期检测（P1.3 合规修复）
# 区分：时间点（可替换）、时间段（保留相对关系）、时间序列（保留顺序词）
DATE_RANGE_PATTERNS = [
    # 2022年4月8日至2022年4月10日（中文日期范围）
    (re.compile(
        rf'({_YEAR4_CN}年{_MONTH_PAT}{_DAY_PAT}日)'
               r'(至|至|——|——)'
        rf'({_YEAR4_CN}年{_MONTH_PAT}{_DAY_PAT}日)'
    ), "YYYY年MM月DD日", "date_range"),
    # 2022/04/08 至 2022/04/10（斜杠日期范围）
    (re.compile(
        rf'({_YEAR4_AR}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))'
        r'(\s*(?:至|——|[-~])\s*)'
        rf'({_YEAR4_AR}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))'
    ), "YYYY/MM/DD", "date_range"),
    # 2022-04-08 ~ 2022-04-10（横杠日期范围）
    (re.compile(
        rf'({_YEAR4_AR}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))'
        r'(\s*(?:至|——|[-~])\s*)'
        rf'({_YEAR4_AR}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))'
    ), "YYYY-MM-DD", "date_range"),
    # 时间序列词（第1次、第2次、第N轮）
    (re.compile(
        r'(?:第\s*[一二三四五六七八九十百0-9]+\s*次|'
        r'第\s*[一二三四五六七八九十百0-9]+\s*轮|'
        r'第\s*[一二三四五六七八九十百0-9]+\s*阶段)'
    ), None, "time_sequence"),
]

DATE_PATTERNS = [
    # YYYY年MM月DD日（中文数字+阿拉伯数字）
    (re.compile(
        rf'{_YEAR4_CN}年{_MONTH_PAT}{_DAY_PAT}日'
    ), "YYYY年MM月DD日（中文）", "date_full"),
    # YYYY/MM/DD
    (re.compile(rf'{_YEAR4_AR}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])'),
     "YYYY/MM/DD", "date_full"),
    # YYYY-MM-DD
    (re.compile(rf'{_YEAR4_AR}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])'),
     "YYYY-MM-DD", "date_full"),
    # 独立年月（中文数字，无日）
    (re.compile(
        rf'{_YEAR4_CN}年{_MONTH_PAT}(?![\s\d\u4e00-\u9fff日])'
    ), "YYYY年MM月（中文）", "date_month_only"),
    # 阿拉伯数字年月（独立）
    (re.compile(
        rf'{_YEAR4_AR}年(?:0?[1-9]|1[0-2])(?!月?[0-9日])'
    ), "YYYY年MM月（阿拉伯）", "date_month_only"),
    # 阿拉伯数字年月（独立，无日）
    (re.compile(
        rf'{_YEAR4_AR}年(0?[1-9]|1[0-2])月(?!日)'
    ), "YYYY年MM月", "date_month_only"),
]

PHONE_PATTERN = re.compile(
    r'(?:1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4})'
)
ID_PATTERN = re.compile(
    r'\b([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])\b'
)
# 账号正则（P1.2 合规修复）
# 旧版：\b(\d{10,20})\b → 误伤流水号/业务编号/页码，已废弃
# 新版：要求明确的上下文标识（账号：/ 账户：/ 卡号：），且账号需符合银行标准格式
# 银行标准账号：行号(4位) + 币种(1位) + 账号类型(1位) + 序号(10-12位) = 16-18位
ACCOUNT_CONTEXTUAL_MARKERS = [
    "账号", "账户", "卡号", "账 号", "帐 号",
    "账号为", "账户为", "卡号为", "账号是", "账户是",
]
ACCOUNT_STANDARD_PATTERN = re.compile(
    r'(?:'
    r'(?:' + '|'.join(re.escape(m) for m in ACCOUNT_CONTEXTUAL_MARKERS) + r')'
    r'\s*'
    r'(\d{16,19})'  # 银行标准账号 16-19 位
    r'|'
    r'(\d{10,19})'  # 直接出现在账号/账户关键词后的数字
    r'(?=\s*[,，。；;]\s*\d{4,})'  # 后面跟 4+ 位数字（流水号场景，保留）
    r')',
    re.IGNORECASE
)
# 仅在无上下文时兜底匹配（超长纯数字，大概率为账号）
ACCOUNT_FALLBACK = re.compile(r'\b\d{16,19}\b')

# 常见百家姓（用于姓名推断）
SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻"
    "柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕"
    "殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝"
    "明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林"
    "刁钟丘徐骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣邓单洪包廖左右"
    "司马上官欧阳诸葛令狐独孤林门龙段郑孔牛童浦施零厉刘"
)

# 已知需排除的词（通用术语，不是敏感信息）
# 注意：P1.4 修复——"海峡"已从全局排除词移除，
#       改为 _is_geographic_haixia() 上下文感知判断
EXCLUDED_COMMON_WORDS = {
    "客户经理", "项目经理", "产品经理", "部门经理", "总经理",
    "风险经理", "合规经理", "运营经理", "技术经理",
    "操作员", "管理员", "柜员", "大堂经理",
    "总行", "分行", "支行", "网点", "营业部", "管理部",
    "项目组", "工作组", "牵头行", "代理行",
    "台湾海峡", "海峡两岸",   # 明确的地理含义，保留排除
    "系统", "业务系统", "核心系统", "外围系统",
    "流程", "操作流程", "业务流程", "审批流程",
    "演练", "业务演练", "切换演练", "灾备演练",
    "版本", "V1.0", "V0.2", "V0.1", "V2.0",
    "附件", "附件1", "附件2", "附件3",
    "密码", "登录密码", "交易密码", "U盾密码",
    "用户名", "用户", "账号", "账户",
    "总行支持人员", "联系人", "负责人",
    "牵头行", "主办行", "参加行",
}


def _is_geographic_haixia(text: str, context: str = "") -> bool:
    """
    判断"海峡"是否为地理含义（P1.4 合规修复）

    规则：
    - "台湾海峡" / "海峡两岸" / "海峡地区" → 地理含义，排除
    - "海峡银行" / "海峡XXX银行" → 银行名，不排除，触发替换
    - 单独"海峡"且上下文无银行关联词 → 排除（默认地理）
    """
    geo_suffixes = {"台湾", "两岸", "地区", "经济", "水域"}
    bank_suffixes = {"银行", "金融", "股份"}

    stripped = text.strip()

    # 已知地理组合词
    if stripped in {"台湾海峡", "海峡两岸", "海峡地区", "海峡水域"}:
        return True

    # 含"海峡" + 有银行后缀 → 不是地理含义
    for suffix in bank_suffixes:
        if suffix in context or any(suffix in stripped for _ in [1]):
            if "银行" in stripped or "金融" in stripped:
                return False

    # 单独"海峡"，默认视为地理
    if stripped == "海峡":
        return True

    return False


class EntityDetector:
    """
    混合实体检测器

    结合 LLM 智能识别 + 正则规则兜底 + 角色词上下文推断
    """

    def __init__(self, llm_client: LLMClient, config: dict):
        self.llm = llm_client
        self.config = config
        self.redaction_cfg = config.get("redaction", {})
        self.industry: str = ""  # P2.7 行业上下文，供 LLM system prompt 注入

    def detect_from_text(self, text: str) -> RedactionManifest:
        """
        从文本内容检测所有敏感实体

        四路并行：
        0. 多语言扩展（en/ja/混合文档）— P2.6 新增
        1. LLM 检测（银行名、新姓名、语义实体）
        2. Regex 兜底（日期、手机、身份证、账号）
        3. 角色词上下文（姓名二次发现）
        """
        manifest = RedactionManifest()

        # 路径0: 多语言扩展（P2.6）— 自动检测语言并路由到对应检测逻辑
        multi_lang_entities = self._multi_lang_detect(text)
        for entity in multi_lang_entities:
            manifest.add_entity(entity)

        # 路径1: LLM 智能检测
        llm_result = self._llm_detect(text)
        for entity in llm_result:
            manifest.add_entity(entity)

        # 路径2: Regex 兜底（日期等标准格式）
        regex_entities = self._regex_detect_dates(text)
        for entity in regex_entities:
            manifest.add_entity(entity)

        # 路径2b: Regex 兜底（日期范围/周期/时间序列）— P1.3 合规修复
        for entity in self._regex_detect_date_ranges(text):
            manifest.add_entity(entity)

        # 路径2b: Regex 兜底（手机/身份证/账号）
        for entity in self._regex_detect_standard(text):
            manifest.add_entity(entity)

        # 路径3: 角色词上下文（姓名二次发现）
        for entity in self._role_word_names(text):
            manifest.add_entity(entity)

        # 过滤误脱风险项
        self._filter_false_positives(manifest)

        return manifest

    def _multi_lang_detect(self, text: str) -> list[SensitiveEntity]:
        """
        多语言扩展检测（P2.6）

        支持中/英/日/混合文档：
        - 中文（zh）：主要走 LLM 检测路径，此处仅补充英文/日文残留片段
        - 英文（en）：使用 EN_BANK_PATTERN / EN_PERSON_PATTERN 等正则
        - 混合语言：逐段落检测语言，分别路由

        Returns:
            检测到的实体列表（已转为 SensitiveEntity）
        """
        try:
            from extensions.multi_lang import MultiLangDetector
        except ImportError:
            return []  # 扩展未安装，静默跳过

        try:
            detector = MultiLangDetector(self)
            result = detector.detect(text)

            primary = result.get("primary_lang", "zh")
            logger.info(f"🌐 多语言检测: 主语言={primary}, 实体数={len(result.get('merged_entities', []))}")

            # MultiLangDetector 返回 dict list，转为 SensitiveEntity
            entities = []
            for item in result.get("merged_entities", []):
                entities.append(SensitiveEntity(
                    text=item.get("text", ""),
                    replacement=item.get("replacement", "XXX"),
                    confidence=float(item.get("confidence", 0.80)),
                    source="multi_lang",
                    category=item.get("category", "unknown"),
                    evidence=item.get("evidence", f"multi_lang:{primary}"),
                ))
            return entities

        except Exception as e:
            logger.warning(f"多语言检测失败（不影响主流程）: {e}")
            return []

    def _llm_detect(self, text: str) -> list[SensitiveEntity]:
        """使用 LLM 检测实体"""
        logger.info("🤖 调用 LLM 进行实体识别...")

        # 分段输入（LLM 上下文有限制），带 200 字符重叠防止边界实体漏检
        chunk_infos = self._split_for_llm(text, max_chars=6000, overlap_chars=200)
        logger.info(f"   文本分为 {len(chunk_infos)} 个片段（每片段重叠 200 字符）")

        all_entities = []

        for i, chunk_info in enumerate(chunk_infos):
            chunk_text = chunk_info["text"]
            # 第 2+ 个 chunk：注入前一个 chunk 末尾 200 字符作为上下文，避免边界实体漏检
            if i > 0:
                overlap_context = (
                    f"\n\n【前段衔接上下文（参考前段末尾，已脱敏处理）：】\n"
                    f"{chunk_info['overlap_suffix']}"
                )
                prompt = build_entity_extraction_prompt(chunk_text + overlap_context)
            else:
                prompt = build_entity_extraction_prompt(chunk_text)

            # P2.7: 构建 system prompt，支持行业专用扩展
            system_prompt = ENTITY_EXTRACTION_SYSTEM_PROMPT
            if self.industry:
                try:
                    from extensions.templates import TemplateManager
                    tm = TemplateManager()
                    system_prompt = system_prompt + tm.build_industry_prompt(self.industry)
                    logger.info(f"   行业扩展已注入: {self.industry}")
                except ImportError:
                    pass  # 扩展未安装，降级到通用 prompt

            response = self.llm.chat(
                prompt=prompt,
                system=system_prompt,
                response_format="json",
            )

            if response.error:
                logger.error(f"   Chunk {i+1} LLM 调用失败: {response.error}")
                continue

            try:
                data = self.llm.parse_json_response(response)

                # 解析银行名
                for item in data.get("bank_names", []):
                    all_entities.append(self._dict_to_entity(item, "bank_names"))

                # 解析姓名
                for item in data.get("persons", []):
                    all_entities.append(self._dict_to_entity(item, "persons"))

                # 解析日期（由 regex 主导，LLM 只补充格式特殊的）
                for item in data.get("dates", []):
                    all_entities.append(self._dict_to_entity(item, "dates"))

                # 解析其他
                for key, cat in [
                    ("phone_numbers", "phone_numbers"),
                    ("id_numbers", "id_numbers"),
                    ("accounts", "accounts"),
                    ("emails", "emails"),
                ]:
                    for item in data.get(key, []):
                        all_entities.append(self._dict_to_entity(item, cat))

                # 无法自动处理的
                for item in data.get("unresolved", []):
                    logger.warning(f"   ⚠️ LLM 无法确定: {item.get('text','')} (置信度 {item.get('confidence',0)})")

                logger.info(f"   Chunk {i+1}: 发现 {len(all_entities)} 个实体")

            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"   Chunk {i+1} 解析失败: {e}")

        # 去重
        seen = set()
        unique = []
        for e in all_entities:
            key = (e.text, e.replacement, e.category)
            if key not in seen:
                seen.add(key)
                unique.append(e)

        logger.info(f"   LLM 共发现 {len(unique)} 个唯一实体")
        return unique

    def _regex_detect_date_ranges(self, text: str) -> list[SensitiveEntity]:
        """
        检测日期范围和周期（P1.3 合规修复）

        日期范围示例：2022年4月8日至2022年4月10日
        - 旧行为：替换为 YYYY年MM月DD日至YYYY年MM月DD日（丢失"3天周期"语义）
        - 新行为：替换为 YYYY/MM/DD至YYYY/MM/DD，保留"至"连接符和相对时长感知

        时间序列示例：第1次演练、第2次演练
        - 不替换"第N次"词本身，只替换其中的日期数字
        """
        entities = []

        for pattern, date_fmt, category in DATE_RANGE_PATTERNS:
            if category == "time_sequence":
                # 时间序列：保留"第N次/第N轮"词，标记但暂不替换
                for m in pattern.finditer(text):
                    seq_word = m.group()
                    entity = SensitiveEntity(
                        text=seq_word,
                        replacement=seq_word,  # 保留原词，不替换
                        confidence=0.95,
                        source="regex",
                        category="date_sequence",
                        evidence="正则匹配时间序列词",
                    )
                    entity.locations.append(Location(
                        block_id=-1, type="text",
                        begin=m.start(), end=m.end(),
                        context=text[max(0, m.start()-20):m.end()+20],
                        verified=True,
                    ))
                    entities.append(entity)
                continue

            # 日期范围：保留连接符，两端日期统一替换
            for m in pattern.finditer(text):
                full_match = m.group()
                parts = m.groups()
                connector = parts[1] if len(parts) > 1 else "至"
                # connector 是日期间的连接词（至/——/-/~等），原文保留
                connector_placeholder = "[DATERANGE_CONNECTOR]"
                # 生成替换文本：START + CONNECTOR + END
                start_date = parts[0]
                end_date = parts[2] if len(parts) > 2 else parts[1]
                start_repl = self._date_replacement(start_date, date_fmt)
                end_repl = self._date_replacement(end_date, date_fmt)
                # 恢复连接符（原文的至/-/~等直接保留，不替换）
                replacement = f"{start_repl}{connector}{end_repl}"

                entity = SensitiveEntity(
                    text=full_match,
                    replacement=replacement,
                    confidence=0.99,
                    source="regex",
                    category="date_range",
                    evidence="正则匹配日期范围，保留连接符",
                )
                entity.locations.append(Location(
                    block_id=-1, type="text",
                    begin=m.start(), end=m.end(),
                    context=text[max(0, m.start()-20):m.end()+20],
                    verified=True,
                ))
                entities.append(entity)

        return entities

    def _regex_detect_dates(self, text: str) -> list[SensitiveEntity]:
        """Regex 检测日期（最可靠的兜底层）"""
        entities = []

        for pattern, format_str, category in DATE_PATTERNS:
            for m in pattern.finditer(text):
                matched_text = m.group()
                if matched_text in EXCLUDED_COMMON_WORDS:
                    continue

                # 避免与已知非日期的词重复
                replacement = self._date_replacement(matched_text, format_str)

                entity = SensitiveEntity(
                    text=matched_text,
                    replacement=replacement,
                    confidence=0.99,
                    source="regex",
                    category=category,
                    evidence=f"正则匹配: {pattern.pattern[:40]}",
                )
                entity.locations.append(Location(
                    block_id=-1,
                    type="text",
                    begin=m.start(),
                    end=m.end(),
                    context=text[max(0, m.start()-20):m.end()+20],
                    verified=True,
                ))
                entities.append(entity)

        return entities

    def _regex_detect_standard(self, text: str) -> list[SensitiveEntity]:
        """Regex 检测标准格式：手机、身份证、账号"""
        entities = []

        # 手机号
        for m in PHONE_PATTERN.finditer(text):
            orig = m.group()
            # 格式化：138xxxx1234 → 138****1234
            digits = re.sub(r'\D', '', orig)
            if len(digits) == 11:
                replacement = f"{digits[:3]}****{digits[7:]}"
            else:
                replacement = f"****{digits[-4:]}"
            entities.append(SensitiveEntity(
                text=orig, replacement=replacement,
                confidence=0.99, source="regex",
                category="phone_number",
                evidence="11位手机号格式",
            ))

        # 身份证
        for m in ID_PATTERN.finditer(text):
            orig = m.group()
            replacement = f"{orig[:3]}***********{orig[-4:]}"
            entities.append(SensitiveEntity(
                text=orig, replacement=replacement,
                confidence=0.99, source="regex",
                category="id_number",
                evidence="18位身份证格式",
            ))

        return entities

    def _role_word_names(self, text: str) -> list[SensitiveEntity]:
        """
        角色词上下文推断姓名
        在'总行支持人员：''联系人：''系统负责人：'等角色词后面
        找姓氏开头的2-4字片段作为姓名。

        同时处理"廖腾华/张力"这样以斜杠分隔的多姓名段，逐个验证。
        """
        entities = []

        role_words = [
            "总行支持人员", "联系人", "系统负责人", "业务负责人",
            "技术经理", "客户经理", "项目经理", "产品经理",
            "技术负责人", "项目负责人", "分行负责人", "支行负责人",
        ]

        for role in role_words:
            # 分隔符：全角冒号/冒号/空格/全角逗/半角逗
            # 用全角逗字面量 \uff0c，避免 uff0c 被 Python 当 unicode escape 解析
            sep_chars = rf'[\s：:，,\uff0c]'
            pattern = re.compile(
                rf'{re.escape(role)}'
                + sep_chars
                + r'([^\n]{0,30})'
            )
            for m in pattern.finditer(text):
                segment = m.group(1).strip()
                if not segment:
                    continue

                # 切分可能的多个姓名（用 / 、 、； / 全角逗 分隔）
                raw_names = re.split(r'[、/；;\uff0c]', segment)
                for raw in raw_names:
                    raw = raw.strip()
                    if not raw:
                        continue

                    # 过滤已知非姓名词
                    if raw in EXCLUDED_COMMON_WORDS:
                        continue

                    # 验证：是否是2-4个汉字（且首字在百家姓）
                    if self._looks_like_name(raw):
                        replacement = (
                            self.redaction_cfg.get("person_replacement", "XXX")
                            if hasattr(self, "redaction_cfg") and self.redaction_cfg
                            else "XXX"
                        )
                        entity = SensitiveEntity(
                            text=raw,
                            replacement=replacement,
                            confidence=0.95,
                            source="rule",
                            category="person",
                            evidence=f"角色词'{role}'上下文提取",
                        )
                        entity.locations.append(Location(
                            block_id=-1,
                            type="text",
                            begin=m.start(),
                            end=m.end(),
                            context=text[max(0, m.start()-20):m.end()+20],
                            verified=True,
                        ))
                        entities.append(entity)

        return entities

    def _looks_like_name(self, s: str) -> bool:
        """
        判断 s 是否像一个人名：
        - 2-4个汉字
        - 首字在百家姓集合中
        - 不在排除词表
        - 不是占位符姓名（如"张某某某"、"王某某"）
        """
        if not s:
            return False
        # 必须是2-4个汉字
        if not re.fullmatch(r'[\u4e00-\u9fff]{2,4}', s):
            return False
        # 首字必须是百家姓
        if s[0] not in SURNAMES:
            return False
        # 排除词
        if s in EXCLUDED_COMMON_WORDS:
            return False
        # 过滤占位符姓名：张某某某 / 李某某 / 王某某
        # 规则：第二字是"某"，或者连续多个"某"
        if '某' in s:
            return False
        return True

    def _filter_false_positives(self, manifest: RedactionManifest):
        """过滤误脱风险项（基于规则 + LLM 双重验证）"""
        all_entities = (
            manifest.bank_names + manifest.persons +
            manifest.dates + manifest.phone_numbers +
            manifest.id_numbers + manifest.accounts
        )

        to_reject = []

        for entity in all_entities:
            # 规则过滤：已知非敏感词
            if entity.text in EXCLUDED_COMMON_WORDS:
                # P1.4 修复："海峡"需要上下文感知，不能全局排除
                if entity.text == "海峡":
                    # 尝试从 location.context 获取上下文，判断是否为地理含义
                    ctx = ""
                    if entity.locations:
                        ctx = entity.locations[0].context
                    if _is_geographic_haixia(entity.text, ctx):
                        entity.rejected = True
                        entity.reject_reason = "地理术语（海峡），非银行名称"
                    # else: 银行名上下文，不 reject，继续处理
                    if entity.rejected:
                        to_reject.append(entity)
                else:
                    entity.rejected = True
                    entity.reject_reason = "通用业务术语，不属于敏感信息"
                    to_reject.append(entity)

            # LLM 低置信度 → 降级到 unresolved
            if entity.confidence < self.redaction_cfg.get("confidence_medium", 0.70):
                if not entity.rejected:
                    manifest.unresolved.append(UnresolvedItem(
                        text=entity.text,
                        reason=f"LLM 置信度过低 ({entity.confidence:.2f})，需人工确认",
                        suggestions=[entity.replacement],
                    ))
                    entity.rejected = True
                    entity.reject_reason = f"置信度 {entity.confidence:.2f} < 0.70"

        # 加入 rejected 列表
        for e in to_reject:
            manifest.rejected.append(RejectedItem(
                text=e.text,
                rejected_reason=e.reject_reason,
                confidence=e.confidence,
                source=e.source,
            ))

    def _split_for_llm(self, text: str, max_chars: int = 6000,
                       overlap_chars: int = 200) -> list[dict]:
        """
        将文本切分为适合 LLM 上下文窗口的片段（带字符级重叠）。

        Args:
            text: 待分割文本
            max_chars: 每个 chunk 最大字符数
            overlap_chars: 相邻 chunk 之间的重叠字符数（默认 200，防止边界实体漏检）

        Returns:
            list[dict]: 每项含 {"text": str, "overlap_suffix": str}
            - text: chunk 主体文本
            - overlap_suffix: 该 chunk 末尾的 200 字符（供下一个 chunk 使用重叠上下文）
        """
        if len(text) <= max_chars:
            return [{"text": text, "overlap_suffix": ""}]

        chunks = []
        start = 0

        while start < len(text):
            end = min(start + max_chars, len(text))

            if end < len(text):
                # 不是最后一个 chunk，保留末尾 overlap_chars 作为下一个的衔接上下文
                overlap_suffix = text[end - overlap_chars:end]
            else:
                overlap_suffix = ""

            chunk_text = text[start:end]
            chunks.append({
                "text": chunk_text,
                "overlap_suffix": overlap_suffix,
            })

            # 跳到下一个 chunk 起点（减去重叠部分，避免重复）
            start = end - overlap_chars
            if start >= end:  # 防止死循环（极端情况）
                start = end

        return chunks if chunks else [{"text": text, "overlap_suffix": ""}]

    def _dict_to_entity(self, item: dict, default_category: str) -> SensitiveEntity:
        """将 LLM 返回的 dict 转换为 SensitiveEntity"""
        return SensitiveEntity(
            text=item.get("text", ""),
            replacement=item.get("replacement", "XXX"),
            confidence=float(item.get("confidence", 0.80)),
            source="llm",
            category=item.get("category", default_category),
            evidence=item.get("evidence", ""),
        )

    def _date_replacement(self, matched_text: str, format_str: str) -> str:
        """根据日期格式决定替换格式"""
        cfg = self.redaction_cfg

        if "YYYY年MM月DD日" in format_str or "YYYY年MM月" in format_str:
            return cfg.get("date_format_chinese", "YYYY年MM月DD日")
        else:
            return cfg.get("date_format", "YYYY/MM/DD")

    def verify_redaction(self, original_text: str, redacted_text: str) -> dict:
        """
        验证脱敏质量（LLM 误脱检测）
        返回验证结果
        """
        prompt = f"""## 原始文档片段
---
{original_text[:3000]}
---

## 脱敏后对应片段
---
{redacted_text[:3000]}
---

请进行误脱检测。"""

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
