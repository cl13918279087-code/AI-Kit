#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# common_rules.py - 统一脱敏规则中心
# doc-redact-project / v1.0.0
#
# 设计原则：
#   1. 链式顺序替换（长文本先处理，短文本后处理，防止短文本误伤）
#   2. 所有脚本共享此模块，保证规则完全一致
#   3. 配置驱动（config.json），改规则不需改代码
#   4. 日期范围语义保留（P1.3 合规修复）
# ---------------------------------------------------------------------------

from __future__ import annotations

import re
import json
import os
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def _load_config() -> Dict[str, Any]:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    config_paths = [
        Path(__file__).parent / "config.json",
        Path(__file__).parent.parent / "config.json",
        Path("config.json"),
    ]
    for p in config_paths:
        if p.exists():
            raw = p.read_text(encoding="utf-8")
            # 支持 ${ENV_VAR} 环境变量插值
            raw = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), raw)
            _CONFIG_CACHE = json.loads(raw)
            return _CONFIG_CACHE

    # fallback：使用内置默认值
    _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def get_config() -> Dict[str, Any]:
    return _load_config()


def get_replacement(key: str, default: str = "XXX") -> str:
    cfg = _load_config()
    return cfg.get("replacement", {}).get(key, default)


# ---------------------------------------------------------------------------
# 百家姓（用于姓名推断，排除已知非姓名场景）
# ---------------------------------------------------------------------------

def _build_surname_set() -> set:
    cfg = _load_config()
    pool = cfg.get("surname_pool", "")
    if not pool:
        pool = (
            "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
            "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
            "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
            "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜"
            "阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐骆高夏蔡田樊胡凌霍虞万支"
            "柯昝管卢莫经房裘缪干解应宗丁宣邓单洪包诸左石崔吉钮龚林门龙段郑孔牛"
            "童浦施零厉刘"
        )
    return set(pool)


SURNAME_SET = _build_surname_set()

# ---------------------------------------------------------------------------
# 排除词表（全局通用术语，不是敏感信息）
# ---------------------------------------------------------------------------

EXCLUDED_COMMON_WORDS: set = {
    # 职位/角色词（不在姓名层排除，但在角色词上下文中有特殊处理）
    "客户经理", "项目经理", "产品经理", "部门经理", "总经理",
    "风险经理", "合规经理", "运营经理", "技术经理", "大堂经理",
    "操作员", "管理员", "柜员",
    # 机构层级（单独出现时不脱敏）
    "总行", "分行", "支行", "网点", "营业部", "管理部",
    "项目组", "工作组", "牵头行", "代理行",
    # 系统/流程术语
    "系统", "业务系统", "核心系统", "外围系统",
    "流程", "操作流程", "业务流程", "审批流程",
    "演练", "业务演练", "切换演练", "灾备演练",
    "版本", "V1.0", "V0.2", "V0.1", "V2.0",
    "附件", "附件1", "附件2", "附件3",
    "密码", "登录密码", "交易密码", "U盾密码",
    "用户名", "用户", "账号", "账户",
    "总行支持人员", "联系人", "负责人",
    "牵头行", "主办行", "参加行",
    # 地理词（需上下文判断，"台湾海峡"保留，"海峡银行"触发替换）
    "台湾海峡", "海峡两岸", "海峡地区",
}


# ---------------------------------------------------------------------------
# 正则规则构建（从 config.json 动态加载，支持扩展）
# ---------------------------------------------------------------------------

def _build_patterns() -> List[Tuple[re.Pattern, str]]:
    cfg = _load_config()
    rep = cfg.get("replacement", {})
    bank_names: List[str] = cfg.get("bank_names", [])
    org_suffixes: List[str] = cfg.get("org_suffixes", [])

    patterns: List[Tuple[re.Pattern, str]] = []

    # ---------- 1. 邮箱 ----------
    patterns.append((
        re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
        rep.get("EMAIL", "XXXXX@XXXXX")
    ))

    # ---------- 2. IP 地址 ----------
    patterns.append((
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        rep.get("IP", "X.X.X.X")
    ))

    # ---------- 3. MAC 地址 ----------
    patterns.append((
        re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"),
        rep.get("MAC", "XX:XX:XX:XX:XX:XX")
    ))

#     # ---------- 4. 详细地址 ----------
#     patterns.append((
#         re.compile(
#             r'[^\x00-\xFF]{2,6}(?:省|自治区|市)?[^\x00-\xFF]{0,10}'
#             r'(?:市|区)?[^\x00-\xFF]{0,10}'
#             r'(?:街|路|道|巷|弄|号|大道|大街|东路|西路|南路|北路|栋|楼)[^\x00-\xFF]{0,30}'
#         ),
#         rep.get("ADDRESS", "XX省XX市XX区XXXX")
#     ))
# 
    # ---------- 5. 身份证号（18位/15位） ----------
    patterns.append((
        re.compile(
            r"\b([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
            r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])\b"
        ),
        rep.get("ID_CARD", "XXXXXXXXXXXXXXXXXX")
    ))
    # 15位身份证
    patterns.append((
        re.compile(r"\b([1-9]\d{5}\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3})\b"),
        rep.get("ID_CARD", "XXXXXXXXXXXXXXXXXX")
    ))

    # ---------- 6. 银行卡号（已禁用：标准版不脱敏账号） ----------
    # 上下文感知版：账号/账户/卡号 关键词后的 16-19 位数字
    # ctx_markers = ["账号", "账户", "卡号", "账 号", "帐 号",
    #                "账号为", "账户为", "卡号为", "账号是", "账户是"]
    # patterns.append((
    #     re.compile(
    #         r'(?:' + '|'.join(re.escape(m) for m in ctx_markers) + r')'
    #         r'\s*(\d{16,19})',
    #         re.IGNORECASE
    #     ),
    #     rep.get("BANK_CARD", "XXXXXXXXXXXXXXXX")
    # ))
    # 无上下文兜底：超长纯数字（可能是账号）
    # patterns.append((
    #     re.compile(r"\b\d{16,19}\b"),
    #     rep.get("BANK_CARD", "XXXXXXXXXXXXXXXX")
    # ))

    # ---------- 7. 手机号码 ----------
    patterns.append((
        re.compile(r"\b1[3-9]\d{9}\b"),
        rep.get("MOBILE", "XXXXXXXXXXX")
    ))

    # ---------- 8. 固定电话（已禁用：标准版不使用） ----------
    # patterns.append((
    #     re.compile(r"0\d{2,3}[-\s]?\d{7,8}"),
    #     rep.get("PHONE", "0XX-XXXXXXXX")
    # ))

    # ---------- 9. 日期范围（P1.3 合规修复：保留连接符和相对关系） ----------
    year4_cn = r'[〇二三四五六七八九0-9]{4}'
    year4_ar = r'20[12][0-9]'
    month_pat = (
        r'(?:'
        r'0?[1-9]|1[0-2]|'
        r'[一二三四五六七八九](?=月)|'
        r'十(?=月)|'
        r'十一(?=月)|十二(?=月)|'
        r'正(?=月)'
        r')'
    )
    day_pat = r'(?:月)?[^月\s]+(?=日)'

    # 中文日期范围：2022年4月8日至2022年4月10日
    patterns.append((
        re.compile(
            rf'({year4_cn}年{month_pat}{day_pat}日)'
            r'(至|至|——|——)'
            rf'({year4_cn}年{month_pat}{day_pat}日)'
        ),
        rep.get("DATE_CHINESE", "YYYY年MM月DD日") + r'\2' + rep.get("DATE_CHINESE", "YYYY年MM月DD日")
    ))
    # 斜杠日期范围：2022/04/08 至 2022/04/10
    patterns.append((
        re.compile(
            rf'({year4_ar}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))'
            r'(\s*(?:至|——|[-~])\s*)'
            rf'({year4_ar}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])$)'
        ),
        rep.get("DATE", "YYYY/MM/DD") + r'\2' + rep.get("DATE", "YYYY/MM/DD")
    ))
    # 横杠日期范围：2022-04-08 ~ 2022-04-10
    patterns.append((
        re.compile(
            rf'({year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))'
            r'(\s*(?:至|——|[-~])\s*)'
            rf'({year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])$)'
        ),
        rep.get("DATE", "YYYY/MM/DD") + r'\2' + rep.get("DATE", "YYYY/MM/DD")
    ))

    # ---------- 10. 日期（独立） ----------
    # 中文数字日期（但排除"应为2022年3月31日"等场景中的日期）
    # 中文数字日期（但排除"应为2022年3月31日"等场景中的日期）
    # 前置排除用后处理替代，避免变长 lookbehind 语法问题
    patterns.append((
        re.compile(rf'{year4_cn}年{month_pat}{day_pat}日'),
        rep.get("DATE_CHINESE", "YYYY年MM月DD日")
    ))
    # YYYY/MM/DD
    patterns.append((
        re.compile(rf'{year4_ar}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])$'),
        rep.get("DATE", "YYYY/MM/DD")
    ))
    # YYYY-MM-DD
    patterns.append((
        re.compile(rf'{year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])$'),
        rep.get("DATE", "YYYY/MM/DD")
    ))
    # 中文年月（独立）
    patterns.append((
        re.compile(rf'{year4_cn}年{month_pat}(?![\s\d\u4e00-\u9fff日])'),
        rep.get("DATE_CHINESE", "YYYY年MM月")
    ))
    # 阿拉伯数字年月
    patterns.append((
        re.compile(rf'{year4_ar}年(?:0?[1-9]|1[0-2])(?!月?[0-9日])'),
        rep.get("DATE", "YYYY/MM")
    ))

    # ---------- 11. 银行名称（从配置动态加载，完全动态化） ----------
    if bank_names:
        # 动态提取所有银行名前缀，按长度降序排列确保长匹配优先
        prefixes_raw = []
        for b in bank_names:
            if "银行" not in b:
                continue
            idx = b.index("银行")
            prefix = b[:idx]
            if prefix:
                prefixes_raw.append((len(prefix), prefix))
        # 去重并按长度降序（长前缀优先匹配，防止"海峡"先于"福建海峡"）
        seen = set()
        prefixes_sorted = []
        for plen, pfx in sorted(prefixes_raw, key=lambda x: -x[0]):
            if pfx not in seen:
                seen.add(pfx)
                prefixes_sorted.append(re.escape(pfx))
        if prefixes_sorted:
            bank_pattern = rf'(?:{"|".join(prefixes_sorted)})银行'
            patterns.append((
                re.compile(bank_pattern),
                rep.get("BANK", "XX银行")
            ))

        # ---------- 11b. 分支行名称（动态从 bank_names 提取） ----------
        # 提取城市/地区前缀 + 支行/营业部/分行等后缀
        branch_suffixes = ["支行", "营业部", "分行", "网点"]
        branch_prefixes = set()
        # 复合后缀集合（如 "分行营业部" = "分行" + "营业部"）
        compound_suffixes = set()
        for b in bank_names:
            for i, suf1 in enumerate(branch_suffixes):
                if b.endswith(suf1):
                    remaining = b[:-len(suf1)]
                    for suf2 in branch_suffixes:
                        if remaining.endswith(suf2):
                            # 找到复合后缀：suf2 + suf1
                            compound = suf2 + suf1
                            if len(b) - len(compound) >= 2:
                                compound_suffixes.add(compound)
                            break
                    pfx = b[:-len(suf1)]
                    if len(pfx) >= 2:
                        branch_prefixes.add(pfx)
                    break
        if branch_prefixes:
            branch_prefix_re = "|".join(
                re.escape(p) for p in sorted(branch_prefixes, key=len, reverse=True)
            )
            # 优先匹配复合后缀，再匹配简单后缀
            all_branch_suf = sorted(
                list(compound_suffixes) + branch_suffixes,
                key=len, reverse=True
            )
            branch_suf_re = "|".join(re.escape(s) for s in all_branch_suf)
            branch_pattern = rf'(?:{branch_prefix_re})(?:{branch_suf_re})'
            # 函数式替换：保留后缀，只替换前缀
            def _branch_repl(m):
                matched = m.group(0)
                for suf in sorted(all_branch_suf, key=len, reverse=True):
                    if matched.endswith(suf):
                        return f"XX{suf}"
                return "XX银行"
            patterns.append((re.compile(branch_pattern), _branch_repl))

        # ---------- 11c. 独立分支行词（无前缀，单独出现） ----------
        # 裸 '分行/支行/营业部'（前后非汉字非ASCII）→ XX+后缀，如 '分行' → 'XX分行'
        # 已带 XX 前缀的结果（XX支行/XX营业部）不会被二次替换；
        # '分支行人员' 中 '分行'/'支行' 前后是汉字，不会误伤
        standalone_suffixes = cfg.get("bank_branch_suffixes") or ["分行", "支行", "营业部"]
        if standalone_suffixes:
            standalone_alt = "|".join(
                re.escape(s) for s in sorted(standalone_suffixes, key=len, reverse=True)
            )
            patterns.append((
                re.compile(
                    # 左边界：不能是汉字(含全角标点)、ASCII字母数字
                    # 右边界：不能是汉字(含全角标点)、ASCII字母数字
                    # 全角标点(、，。 etc)不阻断匹配，如"总行、分行"中分行应被替换
                    rf'(?<![一-龥a-zA-Z0-9])(?:{standalone_alt})(?![一-龥a-zA-Z0-9\u3000-\u303f\uff00-\uffef])'
                ),
                lambda m: "XX" + m.group(0)
            ))

    # ---------- 12. 密码信息 ----------
    # 匹配"密码"后紧跟的数字串，替换为 ******
    # 同时兼容"密码为/密码：/密码 " 等格式
    password_patterns = [
        (r'密码[：:\s]*\d{6,}', '******'),
        (r'pwd[：:\s]*[a-zA-Z0-9]{6,}', '******'),
        (r'passwd[：:\s]*[a-zA-Z0-9]{6,}', '******'),
        (r'口令[：:\s]*[a-zA-Z0-9]{4,}', '******'),
        (r'pin[码]?[：:\s]*\d{4,}', '******'),
    ]
    for ptn, repl in password_patterns:
        patterns.append((re.compile(ptn, re.IGNORECASE), repl))

    # ---------- 13. 组织名（在姓名之后，避免阻挡姓名） ----------
    # 仅匹配真正的组织层级：明确机构词，或 2个汉字+组织后缀（需前后都不是汉字）
    if org_suffixes:
        suffix_alt = '|'.join(re.escape(s) for s in org_suffixes)
        patterns.append((
            re.compile(
                rf'(?:分行|支行|营业部|科技部|运营部|管理部|董事会|监事会|管委会|事业部)|'
                rf'(?<![一-龥])(?:{suffix_alt})(?![一-龥])|'
                rf'(?<![一-龥])[一-龥]{{2}}(?:部|科|中心|管委会|办公室)(?![一-龥])'
            ),
            rep.get("ORG", "XXXX")
        ))


# ---------- 13. 人员姓名（2-3个汉字，粗筛） ----------
    # Python 3.9 不支持变长 lookbehind，移除 excluded 检查
    # 姓氏后必须跟 1-2 个名字汉字，防止单字被误判
    # 名字用字池从 config.json name_pool 动态读取
    surname_alt = '|'.join(re.escape(s) for s in SURNAME_SET)
    name_pool_chars = cfg.get("name_pool", "")
    if not name_pool_chars:
        name_pool_chars = ("伟强志建华文静宇轩浩然俊杰明辉晨曦鹏飞洪红霞丽娟秀英敏芳兰婷玉军平立业德永海波涛"
                          "清北胜利福生财腾广坤传王泓郭艳林微陈卓怡君佩瑶心如梦雨萱晓思彤欣涵晖润峰山宏翠"
                          "冰勃项韬鑫昊毅春为斌少凡梅娥昀铄智诗标芸仁铭侃楷肇乐曹潘李张欧詹郑丁周娜萍燕雅雯"
                          "菲彩佳倩洁慧琳芬蓉澜蕊黛媛娇璐豪超刚勇磊龙荣逸朗天行健自不息东宁瑀")
    # 名字汉字类（供两个规则使用）
    name_char_class = '[' + name_pool_chars + ']'

    # 规则A：姓氏 + 1-2个名字汉字（前后非ASCII非汉字，防止中文词内部被误判）
    # 右边界：阻止 ASCII 或 CJK 汉字字母紧跟（如 '时不'→'执'、'史明'→'细'、
    # '支行'→'可/人' 均为词内部误切），但允许中文标点（，。、：等不在
    # \u4e00-\u9fa5 区间）和字符串结尾（如 '毛航行，'、'毛航行'）
    patterns.append((
        re.compile(
            rf'(?<![a-zA-Z0-9])'
            rf'(?:{surname_alt}){name_char_class}{{1,2}}'
            rf'(?![a-zA-Z0-9\u4e00-\u9fa5])'
        ),
        rep.get("NAME", "XXX")
    ))

    # 规则B：姓氏 + 2个名字汉字（左侧有中文词/冒号，右侧无ASCII/非汉字）
    # 处理"总行支持人员：汪晶晶"等场景（冒号后的姓名，左边界是中文冒号）
    # 2-char 名字确保不会误匹配"安卓"等单字人名
    # 全角冒号 \uff1a 不在 [一-龥] 范围，需显式加入
    patterns.append((
        re.compile(
            rf'(?<=[\u4e00-\u9fa5\uff1a])(?:{surname_alt}){name_char_class}{{2}}(?![a-zA-Z0-9\u4e00-\u9fa5])'
        ),
        rep.get("NAME", "XXX")
    ))

    
    return patterns


# 全局规则（延迟构建）
_PATTERNS: Optional[List[Tuple[re.Pattern, str]]] = None


def _get_patterns() -> List[Tuple[re.Pattern, str]]:
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = _build_patterns()
    return _PATTERNS


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def apply_redactions(text: str) -> str:
    """
    对文本执行全量脱敏替换（链式顺序）。
    返回脱敏后的文本。
    """
    if not text or not isinstance(text, str):
        return text

    result = text
    for pattern, replacement in _get_patterns():
        result = pattern.sub(replacement, result)

    # 后处理：纠正已知误脱敏
    # 1. 姓名模式误匹配中文词
    # 2. DATE pattern 误替换"YYYY年MM月DD日"（应保留原始日期，不应替换"应为2022年3月31日前"中的日期）
    import re as _re
    post_fixes = [
        ('清XXX下', '清单如下'),
        ('营运XXX部', '营运计财部'),
        ('XXX务部', '计财财务部'),
    ]
    for wrong, correct in post_fixes:
        if wrong in result:
            result = result.replace(wrong, correct)

    # 注：Rule A 右边界修复后（阻止 ASCII/CJK 汉字紧跟），
    # '时不'→'执行'、'史明'→'明细'、'支行'→'可根据' 等词内误切已不再发生，
    # 原先针对这些误切的还原后处理已移除。

    # DATE 后处理：DATE pattern 把日期替换为 YYYY年MM月DD日，
    # 但"应为2022年3月31日"中的日期是通用日期描述不应被替换。
    # 策略：扫描文本中所有"YYYY年MM月DD日"，若其前3字是"应为|必须为|须于"，
    # 则替换为 XXXX年XX月XX日（保持脱敏但不暴露原始日期）
    _date_placeholder = 'YYYY年MM月DD日'
    _prefixes = ('应为', '必须为', '须于')
    _pos = 0
    while True:
        _idx = result.find(_date_placeholder, _pos)
        if _idx < 0:
            break
        _before = result[max(0, _idx-3):_idx]
        if _before in _prefixes:
            result = result[:_idx] + 'XXXX年XX月XX日' + result[_idx+len(_date_placeholder):]
        _pos = _idx + 1

    return result


def add_custom_replacement(old: str, new: str, position: int = -1) -> None:
    """
    动态添加自定义替换规则（运行时生效）。
    position=-1 表示添加在姓名规则之前。
    """
    global _PATTERNS
    _PATTERNS = _build_patterns()  # 重建以包含新规则
    _PATTERNS.insert(
        position if position != -1 else len(_PATTERNS) - 1,
        (re.compile(re.escape(old)), new)
    )


def reset_patterns() -> None:
    """重置规则缓存（config.json 变更后需调用）"""
    global _PATTERNS
    _PATTERNS = None


# ---------------------------------------------------------------------------
# 统计类
# ---------------------------------------------------------------------------

REDACTION_LABELS: Dict[str, str] = {
    "EMAIL":     "邮箱",
    "ADDRESS":   "地址",
    "ID_CARD":   "身份证",
    "BANK_CARD": "银行卡",
    "DATE":      "日期",
    "MOBILE":    "手机",
    "PHONE":     "固话",
    "BANK":      "银行名",
    "ORG":       "组织名",
    "NAME":      "姓名",
    "IP":        "IP地址",
    "MAC":       "MAC地址",
    "AMOUNT":    "金额",
}


def count_redactions(text: str) -> Dict[str, int]:
    """统计文本中各类敏感信息的出现次数"""
    if not text:
        return {}

    counts: Dict[str, int] = {}
    patterns = _get_patterns()

    # 使用 REDACTION_LABELS 的 key 顺序统计
    for (key,), (pattern, _) in zip(
        [[k] for k in REDACTION_LABELS.keys()],
        [[p] for p in patterns]
    ):
        pass  # 先统计再映射

    # 简化：按类型名统计
    labels = list(REDACTION_LABELS.values())
    for i, (pattern, _) in enumerate(patterns):
        found = pattern.findall(text)
        if found:
            label = labels[i] if i < len(labels) else f"类型{i}"
            counts[label] = counts.get(label, 0) + len(found)

    return counts


def get_redaction_map() -> List[Tuple[str, str]]:
    """返回当前 (pattern, replacement) 列表，用于外部展示"""
    return _get_patterns()
