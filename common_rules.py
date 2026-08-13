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

    # ---------- 4. 详细地址 ----------
    patterns.append((
        re.compile(
            r'[^\x00-\xFF]{2,6}(?:省|自治区|市)?[^\x00-\xFF]{0,10}'
            r'(?:市|区)?[^\x00-\xFF]{0,10}'
            r'(?:街|路|道|巷|弄|号|大道|大街|东路|西路|南路|北路|栋|楼)[^\x00-\xFF]{0,30}'
        ),
        rep.get("ADDRESS", "XX省XX市XX区XXXX")
    ))

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

    # ---------- 6. 银行卡号（16-19位，有上下文标记时更精确） ----------
    # 上下文感知版：账号/账户/卡号 关键词后的 16-19 位数字
    ctx_markers = ["账号", "账户", "卡号", "账 号", "帐 号",
                   "账号为", "账户为", "卡号为", "账号是", "账户是"]
    patterns.append((
        re.compile(
            r'(?:' + '|'.join(re.escape(m) for m in ctx_markers) + r')'
            r'\s*(\d{16,19})',
            re.IGNORECASE
        ),
        rep.get("BANK_CARD", "XXXXXXXXXXXXXXXX")
    ))
    # 无上下文兜底：超长纯数字（可能是账号）
    patterns.append((
        re.compile(r"\b\d{16,19}\b"),
        rep.get("BANK_CARD", "XXXXXXXXXXXXXXXX")
    ))

    # ---------- 7. 手机号码 ----------
    patterns.append((
        re.compile(r"\b1[3-9]\d{9}\b"),
        rep.get("MOBILE", "XXXXXXXXXXX")
    ))

    # ---------- 8. 固定电话 ----------
    patterns.append((
        re.compile(r"0\d{2,3}[-\s]?\d{7,8}"),
        rep.get("PHONE", "0XX-XXXXXXXX")
    ))

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
            rf'({year4_ar}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))'
        ),
        rep.get("DATE", "YYYY/MM/DD") + r'\2' + rep.get("DATE", "YYYY/MM/DD")
    ))
    # 横杠日期范围：2022-04-08 ~ 2022-04-10
    patterns.append((
        re.compile(
            rf'({year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))'
            r'(\s*(?:至|——|[-~])\s*)'
            rf'({year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))'
        ),
        rep.get("DATE", "YYYY/MM/DD") + r'\2' + rep.get("DATE", "YYYY/MM/DD")
    ))

    # ---------- 10. 日期（独立） ----------
    # 中文数字日期
    patterns.append((
        re.compile(rf'{year4_cn}年{month_pat}{day_pat}日'),
        rep.get("DATE_CHINESE", "YYYY年MM月DD日")
    ))
    # YYYY/MM/DD
    patterns.append((
        re.compile(rf'{year4_ar}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])'),
        rep.get("DATE", "YYYY/MM/DD")
    ))
    # YYYY-MM-DD
    patterns.append((
        re.compile(rf'{year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])'),
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

    # ---------- 11. 银行名称（从配置动态加载） ----------
    if bank_names:
        bank_alt = [
            b.replace("中国", "").replace("银行", "") for b in bank_names
            if "银行" in b and len(b) <= 6
        ]
        # 银行名正则：直接匹配全称，优先长匹配
        bank_pattern = (
            r'(?:'
            # 全称：前缀(中国/农业/工商/...) + [0-8汉字中间词] + 银行
            r'(?:中国|农业|建设|工商|交通|招商|浦发|兴业|民生|华夏|平安|光大|广发|浙商|渤海|恒丰|'
            r'邮政储蓄|人民银行)(?:[^\x00-\xFF]{0,8})?银行|'
            # 农信/农商/村镇等 + [0-8汉字] + 银行
            r'(?:农信社|信用社|农商银行|合作银行|村镇银行|农村商业银行|海峡|城商)(?:[^\x00-\xFF]{0,8})?银行|'
            # 独立城市名+银行（无中间词）
            r'(?:北京|上海|南京|宁波|杭州|深圳|广州|郑州|重庆|天津|成都|西安|'
            r'苏州|武汉|长沙|青岛|济南|大连|沈阳|哈尔滨|长春|石家庄|福州|厦门|'
            r'南昌|合肥|昆明|贵阳|南宁|海口|太原|兰州|呼和浩特|乌鲁木齐)银行'
            r')'
        )
        patterns.append((
            re.compile(bank_pattern),
            rep.get("BANK", "XX银行")
        ))

    # ---------- 12. 组织名（在姓名之前，防止"XX部"被识别为姓名） ----------
    if org_suffixes:
        suffix_alt = '|'.join(re.escape(s) for s in org_suffixes)
        patterns.append((
            re.compile(
                rf'(?:[\u4e00-\u9fa5]{{1,4}}(?:{suffix_alt})|'
                rf'[\u4e00-\u9fa5]{{1,2}}(?:分行|支行|事业部)|'
                rf'(?<![\u4e00-\u9fa5])(?:{suffix_alt})(?![\u4e00-\u9fa5]))'
            ),
            rep.get("ORG", "XXXX")
        ))

    # ---------- 13. 人员姓名（2-3个汉字，粗筛） ----------
    # Python 3.9 不支持变长 lookbehind，移除 excluded 检查
    # 精确排除由 entity_detector 角色词层处理
    surname_alt = '|'.join(re.escape(s) for s in SURNAME_SET)
    patterns.append((
        re.compile(
            rf'(?<![a-zA-Z0-9\u4e00-\u9fa5])'
            rf'(?:{surname_alt})[\u4e00-\u9fa5]{{0,2}}'
            rf'(?![a-zA-Z0-9\u4e00-\u9fa5])'
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
