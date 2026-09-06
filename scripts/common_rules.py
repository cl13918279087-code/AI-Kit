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
    # 常见二字词（Rule A 误捕防护：姓氏+常用字构成的非姓名词）
    # 姓+名用字池中的字 → 但整体非姓名（如"骨干"/"成功"/"创业"等）
    "骨干", "成员", "成功", "创业", "兴业", "恒信", "隆昌",
    "腾飞", "卓越", "领先", "领先", "稳健", "合规", "风控",
    "运营", "管理", "发展", "建设", "推进", "落实", "完善",
    "深化", "提升", "优化", "创新", "改革", "转型", "突破",
    "协同", "联动", "共享", "共建", "共赢", "互利", "互惠",
    "有序", "有效", "有力", "全面", "整体", "系统", "重点",
    "核心", "关键", "主要", "重要", "基本", "根本", "本质",
    "主体", "主题", "主线", "主流", "主动", "主力", "主导",
    "主线", "主力", "主导", "主责", "主抓", "主办", "主推",
    # 分组标签（如 Excel 中"第六排\n（临时新增）"，姓+量词结构非人名）
    "第一", "第二", "第三", "第四", "第五", "第六", "第七",
    "第八", "第九", "第十", "第十一", "第十二",
    "排", "组", "批", "批注", "列", "行", "号",
    # ---- R1 热修（2026-09-06）：v2 改进项清单实测误伤词 ----
    # 姓氏字+名字池字构成的常用词/短语，命中片段被下列词覆盖时保留原文
    "说明书", "实施方案", "交易进度", "每周一", "每周二", "每周三",
    "每周四", "每周五", "每周六", "每周日", "方向明确", "测试阶段",
    "施工方案", "工作方案", "解决方案", "部署方案", "培训方案",
    "整改方案", "设计方案", "技术方案", "应急方案", "汇报材料",
    "操作手册", "用户手册", "指导手册", "培训手册", "管理制度",
    "管理办法", "管理规定", "工作计划", "项目计划", "测试计划",
    "实施计划", "工作安排", "进度安排", "日程安排", "会议纪要",
    "会议记录", "工作总结", "技术方案", "操作规程", "管理规范",
    "时间节点", "时间安排", "文明施工", "单元测试", "集成测试",
    "系统测试", "验收测试", "性能测试", "压力测试", "回归测试",
    "程度评估", "平时检查", "方便快捷", "时间成本", "明书",
    "骨干成员", "阶段范围", "测试阶段范围", "实施阶段", "开发阶段",
    "准备阶段", "调研阶段", "设计阶段", "部署阶段", "上线阶段",
    "启动阶段", "执行阶段", "验收阶段", "试运行阶段", "收尾阶段",
}


# ---------------------------------------------------------------------------
# 回溯式地址脱敏通道（省/市/县/区/街道/路/楼盘/大厦/支行/门牌号）
#
# 设计：从后缀（如"市""街道""大厦""支行""号"）向左回溯收集地名前缀，
#   替换为 "XX+后缀"。相比纯正则方案的优势：
#   1) 完整地址链（"福建省福州市鼓楼区湖东街道"）逐级脱敏，不受右边界限制
#   2) 不依赖地名库，覆盖"金水区""中牟县""湖东街道"等地名库外的名称
#   3) 虚词/指示词保护：市场部/城市/这栋大楼/一栋楼/办公大楼 等不误伤
# ---------------------------------------------------------------------------

_ADDRESS_SUFFIXES: List[str] = [
    "街道", "大道", "大街", "写字楼", "楼盘", "小区", "公寓", "广场", "大厦", "大楼",
    "省", "市", "县", "区", "镇", "乡", "村", "街", "路", "巷", "弄", "支行", "分行", "号",
]
# 指示词/量词字符：回溯遇到即停止，且视为"非地名"场景（如"这栋大楼"）
_ADDRESS_DEMO_CHARS = set("这那该本各每某数几第其此另")
# 虚词/动词/通用词字符：回溯遇到即停止（作为前缀边界，如"在湖东街道""位于郑州市""银行金水支行"）
_ADDRESS_STOP_CHARS = set(
    "的了在是和与或及到从去进被把将已会能可要很更最都也就还又再才只等着过"
    "其此每各某数几第办发级分场面临于位行至向往距沿中"
    "们我你他她它"
)
# 行政区划/道路后缀字符：回溯遇到即停止（地名前缀边界，如"金水区花园路"在"区"处切分、
# "湖东街道恒力大厦"在"道"处切分）
_ADDRESS_ADMIN_BOUNDARY = set("省市县区镇乡村街路巷弄道")
# 支行/分行 允许跨越一个行政后缀（如"中牟县支行"的"县"），跨过后需再收集到地名前缀
_ADDRESS_CROSSABLE_SUFFIXES = {"支行", "分行"}
# 后缀+下一字 的组合保护：避免"市场部/区别/县长/省委"等误伤
_ADDRESS_GUARDS = {
    ("市", "场"), ("市", "面"), ("市", "委"),
    ("区", "别"), ("区", "分"), ("区", "间"),
    ("省", "长"), ("省", "级"), ("省", "委"),
    ("县", "长"), ("县", "委"),
    ("镇", "长"), ("镇", "委"),
    ("村", "民"),
}
_ADDRESS_SUFFIX_RE = re.compile(
    "|".join(re.escape(s) for s in sorted(_ADDRESS_SUFFIXES, key=len, reverse=True))
)


# ---------------------------------------------------------------------------
# R1 常用词覆盖保护（2026-09-06）：姓名规则命中片段若被排除词包含，则保留原文
# 背景：闭集"姓氏字+名字池字"正则对"说明书→说XXX"类高频词误伤率达 90%
# （见《redact_docx_v2改进项清单》改进1），本函数在姓名规则应用层做过滤。
# ---------------------------------------------------------------------------

def _is_protected_by_common_word(text: str, start: int, end: int) -> bool:
    """判断 text[start:end] 命中片段是否被某个排除常用词完整覆盖。"""
    matched = text[start:end]
    if matched in EXCLUDED_COMMON_WORDS:
        return True
    window_start = max(0, start - 8)
    window_end = min(len(text), end + 8)
    window = text[window_start:window_end]
    for word in EXCLUDED_COMMON_WORDS:
        if len(word) < 2:
            continue
        idx = window.find(word)
        while idx >= 0:
            ws = window_start + idx
            we = ws + len(word)
            # 命中片段是排除词的真子串（如"明书"⊂"说明书"）→ 常用词，保留
            if ws <= start and end <= we and (ws, we) != (start, end):
                return True
            idx = window.find(word, idx + 1)
    return False


def _name_guard_sub(pattern: re.Pattern, text: str, replacement: str) -> str:
    """带常用词保护的姓名规则替换。"""
    def _repl(m: re.Match) -> str:
        if _is_protected_by_common_word(m.string, m.start(), m.end()):
            return m.group(0)
        return replacement
    return pattern.sub(_repl, text)


def _apply_address_pass(text: str) -> str:
    """回溯式地址脱敏：扫描后缀 → 向左收集地名前缀 → XX+后缀。"""
    if not text or not isinstance(text, str):
        return text

    out: List[str] = []
    last = 0
    replaced = False
    consumed_until = -1  # 已替换区间右端（替换按从左到右进行，无重叠）

    for m in _ADDRESS_SUFFIX_RE.finditer(text):
        s_start, s_end = m.span()
        if s_start < consumed_until:
            continue  # 落在已替换区间内（如"XX省"中的字），跳过
        suf = m.group(0)

        # 门牌号：数字+号 → XX号（如"花园路39号"、"2号楼"）
        if suf == "号":
            j = s_start
            while j - 1 >= 0 and text[j - 1] in "0123456789０１２３４５６７８９":
                j -= 1
            digit_len = s_start - j
            if 1 <= digit_len <= 6:
                # 门牌号前面应是汉字（路名/楼名）或行首
                if j == 0 or ("\u4e00" <= text[j - 1] <= "\u9fff"):
                    out.append(text[last:j])
                    out.append("XX号")
                    consumed_until = s_end
                    last = s_end
                    replaced = True
            continue

        # 向左回溯收集地名前缀（最多5个汉字）
        i = s_start
        collected: List[str] = []
        stopped_by_demo = False
        crossed_admin = ""  # 支行/分行 跨越的行政后缀（如"中牟县支行"的"县"）
        while i - 1 >= 0 and len(collected) < 5:
            ch = text[i - 1]
            if not ("\u4e00" <= ch <= "\u9fff"):
                break
            if ch in _ADDRESS_DEMO_CHARS:
                stopped_by_demo = True
                break
            if ch in _ADDRESS_STOP_CHARS:
                break
            if ch in _ADDRESS_ADMIN_BOUNDARY:
                # 支行/分行 允许跨越一个行政后缀（县支行/市分行），其余作为边界
                if (suf in _ADDRESS_CROSSABLE_SUFFIXES and not crossed_admin
                        and ch in ("省", "市", "县", "区")):
                    crossed_admin = ch
                    i -= 1
                    continue
                break
            collected.append(ch)
            i -= 1
        prefix = "".join(reversed(collected))

        # 保护条件：指示词场景（这栋大楼）/ 前缀不足2字（城市/超市/山区）/ 跨行政后缀但无地名
        if stopped_by_demo or len(prefix) < 2 or (crossed_admin and len(prefix) < 2):
            continue
        # 保护：回溯后的替换起点若落入已替换区间（如"中牟县支行"的"县"已替换），跳过
        if i < consumed_until:
            continue
        # 保护：前缀紧邻已替换的 XX 占位符（避免对 "XX支行" 二次替换）
        if i - 1 >= 0 and text[i - 1] == "X":
            continue
        # 保护：前缀从文本开头开始（i==0）且后缀为单字地址词（镇/街/路/村/县/市/区/省/楼/栋）
        # 此时 text[last:i] = text[0:0] = ''，姓氏会被丢失，且单字后缀在人名中更常见 → 跳过
        if i == 0 and len(suf) == 1:
            continue
        # 保护：回溯跨越了 XML/HTML 关闭标签边界（'>'），如 <t>黄日镇</t> 中 '>' 在 collected 外
        # 若回溯停止位置 i>0 且 text[i-1] 是 '>'，说明前缀跨越了 </t> 等关闭标签，不应视为地址
        if i > 0 and text[i - 1] == ">":
            continue
        # 保护：后缀紧邻 '<'（XML 关闭标签如 </t>镇），跳过
        if s_start > 0 and text[s_start - 1] == "<":
            continue
        # 保护：后缀+下一字组合（市场部/区别/县长等）
        nxt = text[s_end] if s_end < len(text) else ""
        if (suf[-1], nxt) in _ADDRESS_GUARDS:
            continue

        out.append(text[last:i])
        out.append("XX" + crossed_admin + suf)
        consumed_until = s_end
        last = s_end
        replaced = True

    if not replaced:
        return text
    out.append(text[last:])
    return "".join(out)


# ---------------------------------------------------------------------------
# 正则规则构建（从 config.json 动态加载，支持扩展）
# ---------------------------------------------------------------------------

def _build_patterns() -> List[Tuple[re.Pattern, str]]:
    global _PATTERNS_PRE, _PATTERNS_NAME
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

    # ---------- 8. 固定电话（已启用：精确等长替换） ----------
    # 策略：分两段替换，0XX- 占3位 + 8位数字 = 11位，与原区号-号码格式等长
    # 例如：0371-85519208(12位) → 0XX-85519208(11位)，避免替换后长度变化导致 XML 节点错位
    patterns.append((
        re.compile(r"0(\d{2,3})-(\d{7,8})"),
        lambda m: f"0XX-{m.group(2)}"  # 区号部分替换为0XX，保持总长11位
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
    # R2 修复（2026-09-06）：右端 $ 锚点导致正文中日期永不命中（漏脱敏），改为 (?![0-9])
    patterns.append((
        re.compile(
            rf'({year4_ar}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))'
            r'(\s*(?:至|——|[-~])\s*)'
            rf'({year4_ar}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))(?![0-9])'
        ),
        rep.get("DATE", "YYYY/MM/DD") + r'\2' + rep.get("DATE", "YYYY/MM/DD")
    ))
    # 横杠日期范围：2022-04-08 ~ 2022-04-10
    patterns.append((
        re.compile(
            rf'({year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))'
            r'(\s*(?:至|——|[-~])\s*)'
            rf'({year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))(?![0-9])'
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
    # R2 修复：$ → (?![0-9])，正文中的日期（如"2020/5/1实施"）此前永不命中
    patterns.append((
        re.compile(rf'{year4_ar}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])(?![0-9])'),
        rep.get("DATE", "YYYY/MM/DD")
    ))
    # YYYY-MM-DD
    patterns.append((
        re.compile(rf'{year4_ar}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?![0-9])'),
        rep.get("DATE", "YYYY/MM/DD")
    ))
    # 中文年月（独立）
    # R2 修复：显式吞掉"月"字，避免阿拉伯年月规则先吃掉"2020年5"残留"月"字
    # 占位符用独立的年月键，避免取到含 DD日 的完整日期占位
    patterns.append((
        re.compile(rf'{year4_cn}年{month_pat}月(?![0-9日])'),
        rep.get("DATE_CHINESE_MONTH", "YYYY年MM月")
    ))
    # 阿拉伯数字年月（后跟"月"的场景由上一条处理）
    patterns.append((
        re.compile(rf'{year4_ar}年(?:0?[1-9]|1[0-2])(?!月|[0-9日])'),
        rep.get("DATE", "YYYY/MM")
    ))
    # R2 新增：无年份中文短日期"X月X日"（如"5月20日发布"）
    # 左边界阻止年份残留（YYYY年X月X日 已由上方完整规则先替换）与数字
    patterns.append((
        re.compile(r'(?<![0-9年])(?:0?[1-9]|1[0-2])月(?:[0-3]?[0-9])日(?![0-9])'),
        rep.get("DATE_CHINESE_SHORT", "MM月DD日")
    ))
    # R2 新增：无年份斜杠短日期"M/D"（如"截止9/1上报"）
    # 边界阻止长数字串/小数/日期已替换场景
    patterns.append((
        re.compile(r'(?<![0-9/.])(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12][0-9]|3[01])(?![0-9/])'),
        rep.get("DATE_SHORT", "MM/DD")
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

        # ---------- 11c. 银行名称兜底（R3，2026-09-06） ----------
        # 白名单外的银行名（如"宁夏银行"）此前不被命中，反被姓名规则截胡为"宁XXX行"。
        # 通用模式：2-6个汉字 + 银行。占位符"XX银行"的前缀为非汉字字符，不会二次命中。
        patterns.append((
            re.compile(r'[\u4e00-\u9fa5]{2,6}银行'),
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

        # ---------- 11d. 地址/城市名（如海峡、郑州 → XX） ----------
        # 覆盖三类模式：
        #   1) 地名+行政后缀：郑州市 → XX市，福建省 → XX省，龙岩市 → XX市
        #   2) 地名+道路后缀：福州路 → XX路，厦门街 → XX街
        #   3) 独立地名（无后缀）：海峡 → XX，齐鲁 → XX（前后非汉字/ASCII）
        # "海峡银行"、"郑州银行" 等已由银行名规则覆盖，不受本规则影响
        # "台湾海峡" 等受 EXCLUDED_COMMON_WORDS 保护
        location_names: List[str] = cfg.get("location_names", [])
        if location_names:
            loc_alt = "|".join(
                re.escape(loc) for loc in sorted(location_names, key=len, reverse=True)
            )

            # 后缀总表（供 _loc_repl 保留后缀、仅替换地名前缀）
            compound_branch = [
                "省分行", "省支行", "省营业部",
                "市分行", "市支行", "市营业部",
                "县支行", "县营业部",
                "区支行", "区营业部",
            ]
            simple_admin = ["省", "市", "县", "区", "镇", "乡", "村", "街道", "路", "街", "道", "巷", "弄", "号", "栋", "楼", "大道", "大街", "东路", "西路", "南路", "北路"]

            def _loc_repl(m):
                full = m.group(0)
                for suf in sorted(set(compound_branch + simple_admin), key=len, reverse=True):
                    if full.endswith(suf) and len(full) > len(suf):
                        return "XX" + suf
                return rep.get("LOCATION", "XX")

            # 子规则1：地名 + 复合行政/银行后缀（无需右边界，如"市分行营业部"）
            # 先匹配长的复合后缀（无右边界），再匹配行政后缀
            compound_alt = "|".join(re.escape(s) for s in sorted(compound_branch, key=len, reverse=True))
            patterns.append((
                re.compile(
                    rf'(?<![一-龥a-zA-Z0-9])'
                    rf'(?:{loc_alt})(?:{compound_alt})'
                ),
                _loc_repl
            ))

            # 子规则2：地名 + 行政/道路后缀（带右边界）
            simple_admin_alt = "|".join(re.escape(s) for s in sorted(simple_admin, key=len, reverse=True))
            patterns.append((
                re.compile(
                    rf'(?<![一-龥a-zA-Z0-9])'
                    rf'(?:{loc_alt})(?:{simple_admin_alt})'
                    rf'(?![一-龥a-zA-Z0-9])'
                ),
                _loc_repl
            ))
            patterns.append((
                re.compile(
                    rf'(?<![一-龥a-zA-Z0-9])'
                    rf'(?:{loc_alt})'
                    rf'(?![一-龥a-zA-Z0-9\u3000-\u303f\uff00-\uffef])'
                ),
                rep.get("LOCATION", "XX")
            ))
            # 子规则4：地名 + "XX"占位符（银行名/支行名已脱敏后残留的地名前缀，
            # 如"福建XX银行" → "XXXX银行"，避免省市信息经替换结构残留）
            patterns.append((
                re.compile(
                    rf'(?<![一-龥a-zA-Z0-9])(?:{loc_alt})(?=X{{2}})'
                ),
                rep.get("LOCATION", "XX")
            ))

    # ---------- 11e/11f. 地址信息（省/市/县/区/街道/路/楼盘/大厦/支行/门牌） ----------
    # 采用回溯式地址脱敏通道 _apply_address_pass()（见下方函数定义），
    # 在 apply_redactions 中于姓名规则之前执行，此处不再注册正则规则。

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
    # ↓↓↓ 姓名规则从本行开始单独归组：apply_redactions 中地址通道在其之前执行 ↓↓↓
    _pre_pattern_count = len(patterns)
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

    # 规则A：姓氏 + 1-3个名字汉字（支持二字和三字人名如"陈斌""郑学钟"）
    # 左边界：阻止 ASCII/数字前缘（防止英文单词残段匹配）
    # 右边界：仅阻止 ASCII/数字跟随，释放 CJK 跟随（使"柳长春主持"可匹配）
    # 复姓优先：2字复姓 + 1-3个名字汉字（防止复姓第二字被单姓规则误匹配）
    compound_surnames = cfg.get("compound_surnames", [])
    if compound_surnames:
        compound_alt = '|'.join(re.escape(s) for s in sorted(compound_surnames, key=len, reverse=True))
        patterns.append((
            re.compile(
                rf'(?<![a-zA-Z0-9])'
                rf'(?:{compound_alt}){name_char_class}{{1,3}}'
                rf'(?![a-zA-Z0-9])'
            ),
            rep.get("NAME", "XXX")
        ))
    patterns.append((
        re.compile(
            rf'(?<![a-zA-Z0-9])'
            rf'(?:{surname_alt}){name_char_class}{{1,3}}'
            rf'(?![a-zA-Z0-9])'
        ),
        rep.get("NAME", "XXX")
    ))

    # 规则B：姓氏 + 2个名字汉字（左侧有中文词/冒号，右侧无ASCII/非汉字）
    # 处理"总行支持人员：汪晶晶"和"组长：樊霖副组长"等场景
    # 放宽右边界：允许 CJK 汉字跟随（因为中文名字常被标题/职务词跟随）
    # 但阻止纯 ASCII 跟随（英文单词残段）
    # 全角冒号 \uff1a 不在 [一-龥] 范围，需显式加入
    patterns.append((
        re.compile(
            rf'(?<=[\u4e00-\u9fa5\uff1a])(?:{surname_alt}){name_char_class}{{2}}(?![a-zA-Z0-9])'
        ),
        rep.get("NAME", "XXX")
    ))

    # 规则C-1：半角冒号后姓名（如"联系人:方培培"）
    # 全角冒号已由 Rule C 处理；半角冒号(:)ASCII 不在 [\u4e00-\u9fa5] 范围，需单独处理
    patterns.append((
        re.compile(
            rf'(?<=:)(?:{surname_alt}){name_char_class}{{1,3}}(?![a-zA-Z0-9])'
        ),
        rep.get("NAME", "XXX")
    ))

    # 规则C：姓氏 + 1个名字汉字（左侧必须是全角冒号，解决"组长：樊霖"型漏检）
    # Rule B 要求 surname+2名字汉字，但"樊霖"只有 surname+1名字汉字，
    # 在"组长：樊霖副组长"中 Rule B 无法匹配（霖后跟CJK"副"），
    # Rule A 也无法匹配（霖后跟CJK"副"违反右边界）。
    # 本规则专门处理：全角冒号后紧跟「姓氏+1名字汉字」的场景，右边界允许CJK跟随。
    patterns.append((
        re.compile(
            rf'(?<=：)(?:{surname_alt}){name_char_class}(?![a-zA-Z0-9])'
        ),
        rep.get("NAME", "XXX")
    ))

    # 规则D：姓氏 + 名字汉字（左侧是常见介词/动词，解决"由XXX负责"型漏检）
    # 仅添加最可靠的上下文：左侧为"由"时，人名概率最高。
    # 右边界：允许CJK汉字跟随（如"由XXX负责"），阻止ASCII/数字跟随。
    patterns.append((
        re.compile(
            rf'(?<=由)(?:{surname_alt}){name_char_class}{{1,3}}(?![a-zA-Z0-9])'
        ),
        rep.get("NAME", "XXX")
    ))

    # 规则E：姓氏 + 名字汉字（左侧是常见动词如"由、为、对"等）
    # 覆盖"为XXX安排"、"对XXX负责"等场景。
    _name_follow_verbs = ['由', '为', '对', '让', '请', '告', '诉', '见', '任', '选', '用', '调', '指', '派', '承', '责', '主', '抓', '干', '经', '协']
    _verb_alt = '|'.join(re.escape(v) for v in _name_follow_verbs)
    patterns.append((
        re.compile(
            rf'(?<=(?:{_verb_alt}))(?:{surname_alt}){name_char_class}{{1,3}}(?![a-zA-Z0-9])'
        ),
        rep.get("NAME", "XXX")
    ))


    # 拆分：姓名规则单独归组，供 apply_redactions 在地址通道之后执行
    _PATTERNS_PRE = patterns[:_pre_pattern_count]
    _PATTERNS_NAME = patterns[_pre_pattern_count:]

    return patterns


# 全局规则（延迟构建）
_PATTERNS: Optional[List[Tuple[re.Pattern, str]]] = None
_PATTERNS_PRE: List[Tuple[re.Pattern, str]] = []
_PATTERNS_NAME: List[Tuple[re.Pattern, str]] = []


def _get_patterns() -> List[Tuple[re.Pattern, str]]:
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = _build_patterns()
    return _PATTERNS


def _get_pattern_groups() -> Tuple[List[Tuple[re.Pattern, str]], List[Tuple[re.Pattern, str]]]:
    """返回（姓名前规则组, 姓名规则组）。地址通道需在两组之间执行。"""
    _get_patterns()
    return _PATTERNS_PRE, _PATTERNS_NAME


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

    pre_patterns, name_patterns = _get_pattern_groups()

    result = text
    # 第一阶段：姓名之前的规则（邮箱/日期/银行/支行/地名等）
    for pattern, replacement in pre_patterns:
        result = pattern.sub(replacement, result)

    # 第二阶段：回溯式地址脱敏通道（省/市/县/区/街道/路/楼盘/大厦/支行/门牌）
    # 必须在姓名规则之前执行，防止"金水/花园"等地址成分被姓名规则误吞
    result = _apply_address_pass(result)

    # 第三阶段：姓名规则（R1：带常用词覆盖保护，"说明书→说XXX"类误伤在应用层过滤）
    _name_repl = get_replacement("NAME", "XXX")
    for pattern, replacement in name_patterns:
        result = _name_guard_sub(pattern, result, replacement)

    # 后处理：纠正已知误脱敏
    # 1. 姓名模式误匹配中文词（Rule A 右边界放宽后，可能对常用词造成误匹配）
    # 2. DATE pattern 误替换（策略：替换为 XXXX年XX月XX日 保持脱敏但不暴露原始值）
    import re as _re
    post_fixes = [
        ('清XXX下', '清单如下'),
        ('营运XXX部', '营运计财部'),
        ('XXX务部', '计财财务部'),
        # Rule A 右边界放宽后新增：时不我待（时不+我=误匹配）
        ('XXX我待', '时不我待'),
        # Rule A 误捕二字常用词（如"骨干成员"→"骨XXX员"）
        # 策略：将 XXX 替换回原始词（XXX 前后必须是原字符，且构成完整误脱词）
        ('骨干XXX员', '骨干成员'),
        # 扩大：姓氏+XXX+常用后缀 的误脱恢复
    ]
    for wrong, correct in post_fixes:
        if wrong in result:
            result = result.replace(wrong, correct)

    # 恢复被地址通道误截断的姓名（如"黄日镇"被地址通道处理为"XX镇"）
    # 策略：扫描 "姓氏+XX+名字池单字" 模式，如"黄XX镇" → "XXX"
    # 姓氏后面紧跟 XX（地址占位符），XX后是名字池中的单字（镇/璇/钟等）
    cfg = _load_config()
    name_pool_chars = cfg.get("name_pool", "")
    _surname_set_recovery = SURNAME_SET  # 引用全局姓氏集
    _name_chars = set(name_pool_chars)  # 名字用字集合
    # 扫描所有 XX{single_name_char} 的位置
    _pos = 0
    while True:
        _idx = result.find('XX', _pos)
        if _idx < 0:
            break
        # 检查 XX 前一个字符是否是姓氏
        if _idx > 0:
            _ch_before = result[_idx - 1]
            if _ch_before in _surname_set_recovery:
                # 检查 XX 后是否紧跟名字池单字
                if _idx + 2 < len(result):
                    _ch_after = result[_idx + 2]
                    if _ch_after in _name_chars:
                        # 姓氏 + XX + 名字单字 → 姓氏 + XXX（完整姓名）
                        result = result[:_idx - 1] + 'XXX' + result[_idx + 3:]
                        _pos = _idx - 1  # 从 XXX 后继续扫描
                        continue
        _pos = _idx + 1

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
    "LOCATION":   "地址",
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


# ---------------------------------------------------------------------------
# 实体检测接口（供 entity_detector 调用，避免重复实现规则）
# ---------------------------------------------------------------------------

def detect_by_patterns(text: str, patterns: List[Tuple[re.Pattern, str]]) -> List[Dict[str, Any]]:
    """
    使用指定正则模式列表从文本中检测敏感实体。

    返回格式：
        [{"text": "...", "replacement": "...", "category": "...", "source": "regex"}, ...]

    用途：entity_detector.py 的日期检测等规则层检测委托本函数，
          保证 common_rules.py 是规则的单一来源。
    """
    import re as _re

    results = []
    # 去重：同类模式已按优先级排列，同一文本段只记录首次匹配
    seen_spans = set()

    for pattern, replacement in patterns:
        for m in pattern.finditer(text):
            full = m.group(0)
            # 跳过已匹配过的区间（优先保留先匹配到的规则）
            span_key = (m.start(), m.end())
            if span_key in seen_spans:
                continue
            seen_spans.add(span_key)

            # 判断 category
            cat = _infer_category(replacement, full)
            results.append({
                "text": full,
                "replacement": replacement,
                "category": cat,
                "source": "regex",
                "confidence": 0.95,
                "evidence": f"正则匹配: {pattern.pattern[:50]}",
            })

    return results


def _infer_category(replacement: str, original: str) -> str:
    """从 replacement/原始文本推断敏感类型"""
    if "XXXXX@XXXXX" in replacement:
        return "邮箱"
    if "X.X.X.X" in replacement:
        return "IP地址"
    if "XX:XX:XX:XX:XX:XX" in replacement:
        return "MAC地址"
    if "XXXXXXXXXXXXXXXXXX" in replacement:
        return "身份证"
    if "XXXXXXXXXXXXXXXX" in replacement:
        return "银行卡"
    if "YYYY" in replacement or "MM" in replacement or "DD" in replacement:
        return "日期"
    if "XXX" in replacement and len(original) <= 4:
        return "姓名"
    if "XX银行" in replacement:
        return "银行名称"
    if "XXXX" in replacement and len(original) <= 10:
        return "组织名"
    if "XXXX" in replacement:
        return "组织名"
    if "******" in replacement:
        return "密码"
    return "其他"
