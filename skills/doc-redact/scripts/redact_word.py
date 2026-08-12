#!/usr/bin/env python3
"""
redact_word.py - Word 文档脱敏脚本 v3
支持 .docx / .doc（含 .doc 需先转为 .docx）
依赖: python-docx, Pillow
改进: 姓名双层验证(姓氏库+上下文), 文件名同步脱敏, 银行Logo替换
"""

import sys, re, zipfile, shutil, tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 第一部分：姓名识别（姓氏库 + 上下文标签，双层验证）
# ---------------------------------------------------------------------------

# 单字姓氏库（约330个常用姓氏，已含：周/柳/舒/栗/亓/郝/眭/师/滑等）
SINGLE_SURNAMES = set(
    "李王张刘陈杨黄赵吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧"
    "程曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜"
    "范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段雷钱汤"
    "尹黎易常武乔贺赖龚文安欧郑阮阳韦蒋周柳舒栗亓代和洪鲜衣闵童焦"
    "鲁韦昌马苗凤花俞任袁柳酆鲍史乐于孟眭师滑"
)

# 复姓库（常见两字姓氏）
COMPOUND_SURNAMES = {
    "欧阳", "上官", "司马", "诸葛", "东方", "独孤", "南宫", "万俟",
    "澹台", "皇甫", "尉迟", "公羊", "公冶", "宗政", "濮阳", "单于",
    "太叔", "申屠", "公孙", "仲孙", "轩辕", "令狐", "钟离", "宇文",
    "长孙", "慕容", "鲜于", "闾丘", "司徒", "司空", "亓官", "司寇",
    "子车", "颛孙", "端木", "巫马", "公西", "漆雕", "乐正", "壤驷",
    "公良", "拓跋", "夹谷", "百里", "东郭", "南门", "呼延", "羊舌",
    "微生", "梁丘", "左丘", "东门", "西门", "夏侯",
}
COMPOUND_SURNAMES = set(COMPOUND_SURNAMES)

# 高频名字用字（覆盖罕见姓氏对应的常见名如培/争/慧/丽/瑛等）
# 策略：优先使用宽松的大字符集，确保绝大多数真实姓名能被保守模式捕获
COMMON_GIVEN_CHARS = set(
    # 常见高频字
    "伟刚勇毅俊峰强军平荣华杰志国涛成康辉光明健浩飞红亮玉珍秀英慧巧静淑贤惠佳蓉青燕萍桂花琪敏娜丽娟秀兰凤芬盈盈璐月"
    "琴美媛艳英妹霞桂芳莲琼琳彬磊浩川龙虎欣怡思远静雅婷波涛博超建杰志刚志强志明永斌立峰海波彦峰鹏宇泽翰星嘉航帆"
    "文彬博勇峰荣华光明健康浩飞红亮玉珍英慧巧静淑贤惠佳蓉青燕桂花琪敏欣怡思远静雅婷瑶莉媛艳丽霞芳丹妮娜婷婷"
    "雪梅冬梅菊竹桂香萱苗芳萍花卓尔卓越卓然不凡德厚德荣德清明德文子琪子轩子墨子衿瑞霖瑞轩瑞泽瑞祥峻熙希熙哲熙媛"
    "泽宇泽轩泽然泽语浩然浩轩浩天博宇博轩博然博远俊豪俊熙俊楠俊逸瑾萱瑾瑜瑾琳韵宁韵华韵清涵蓄涵养诗涵思涵思远"
    "思念梓涵天佑天赐天赋安琪安宁安然俊杰俊秀俊朗嘉豪嘉祥嘉瑞可欣可言可儿雅静雅琴雨涵雨轩雨彤鑫磊鑫悦鑫然铭铭宇"
    "铭轩怡萱怡静怡然悠然大度明轩晨曦晨光晨辉沐晴晓晨曦初晴"
    # 扩充遗漏字（按拼音排序收录常见名用字）
    "培争慧丽瑛宏亮娜婷敏玲芳萍花苗青兰凤芬桂香萱青燕秀英华"
    "刚勇健飞红亮玉珍慧巧静淑贤惠佳蓉桂花琪敏盈璐月琴美媛艳英"
    "妹霞莲琼琳彬磊川龙虎欣怡思远雅婷波超建杰志刚志强志明永斌"
    "立峰海波彦峰鹏宇泽翰星嘉航帆文彬博峰荣华光明健康浩宇浩然"
    "浩轩浩天博轩博然博远俊豪俊熙俊楠俊逸瑾瑜韵宁韵华清涵养诗"
    "思涵子涵天佑天赐安琪安宁俊秀俊朗嘉豪嘉瑞可欣可言雨涵雨彤"
    "鑫磊鑫悦鑫然铭宇铭轩怡萱怡静怡然悠然大度明轩晨曦晨光晨辉"
    "沐晴晓晨曦初晴晔炜懿炜彤昱昀晓晗暄昭晨熙"
    "媛婷岚霏枫桦杉桐桦梓榆桢楠樟桦"
    # 补充常用名用字
    "上"
    # 补充文档中真实出现过的名字用字（确保保守模式捕获）
    "进才靖阳何邹师眭春智向坤书洋南长永德龙海波峰宇风"
    # 补充文档与会人员列表中的名字用字
    "祖全琦挺言鹏飞万征炼鸽旭峥嵬菂煌"
    # 补充已知漏匹配名字用字
    "一飞又云"
)

# 后缀黑名单：姓+这些完整词/词组的，不是姓名，不单独替换
# 规则：黑名单词必须紧跟姓名才算命中（startswith，非in）
# 注意：只收录明确的职务/称呼/机构词，单字名词（如"部""的""工作"）不收录，
# 因为它们可能是部门名的一部分（如"运营部"），误伤正常姓名
NAME_SUFFIX_BLACKLIST = {
    # 机构职务
    "公司", "银行", "支行", "分行", "部门", "科室", "处室", "事务所",
    "集团", "联社", "有限公司", "股份有限公司",
    # 职务称呼（完整词）
    "主任", "经理", "工程师", "副局长", "局长", "副行长", "行长",
    "总经理", "副总经理", "董事长", "副董事长", "总监", "副总监",
    "项目部", "事业部", "工作组", "小组",
    # 人员称呼
    "负责人", "主送人", "抄送人", "编制人", "审核人", "批准人", "审批人",
    "主讲人", "汇报人", "接口人", "联络人", "经办人", "填表人", "录入人",
    "作者", "编辑", "先生", "女士", "同志", "老师", "教授",
    # 业务词组（完整词，防止姓名+这些被误脱）
    "新一代", "信息系统", "方案", "介绍", "马上", "开始", "流程", "功能", "储备",
    # 常见组合词保护（前缀与姓名连用时，整体不应被拆分替换）
    "责任人", "负责人", "主办人",
}

# 上下文标签模式：这些词后面出现的姓名可信度最高
NAME_LABEL_PATTERNS = [
    re.compile(r'(?:姓名|姓名：|姓\s*名\s*[:：]?)\s*([\u4e00-\u9fa5]{2,4})'),
    re.compile(r'(?:负责人|责任人|项目负责人|接口人|主讲人|汇报人|联系人|联络人|经办人)\s*[:：]?\s*([\u4e00-\u9fa5]{2,4})'),
    re.compile(r'(?:编制人|审核人|批准人|审批人|主送人|抄送人|作者|编辑|填表人|录入人)\s*[:：]?\s*([\u4e00-\u9fa5]{2,4})'),
]

# 构建基于姓氏库的姓名正则
# 策略：3字优先匹配（姓+名2字），2字兜底；黑名单过滤
_sc = ''.join(sorted(SINGLE_SURNAMES))
_gc = ''.join(sorted(COMMON_GIVEN_CHARS))
_cp = '|'.join(sorted(COMPOUND_SURNAMES, key=len, reverse=True))

# 宽松模式：3字优先（姓+2字名），再2字（姓+1字名）
NAME_PATTERN_LOOSE = re.compile(
    f'(?:(?:{_cp})[\u4e00-\u9fa5]{{2,3}}|[{_sc}][\u4e00-\u9fa5]{{1,3}})', re.U
)
# 保守模式：姓 + 常用名用字1~2字
NAME_PATTERN_STRICT = re.compile(
    f'(?:(?:{_cp})[{_gc}]{{1,2}}|[{_sc}][{_gc}]{{1,2}})', re.U
)

# 判断一个匹配是否来自复姓（如果是，名字部分从第4或第5个字符开始）
def _is_compound_surname_match(m) -> bool:
    """检测 match 是否为复姓匹配（复姓 2-3 字 + 名 2-3 字）"""
    full = m.group(0)
    for cs in COMPOUND_SURNAMES:
        if full.startswith(cs):
            return True
    return False

# 计算名字部分（姓氏之后的所有字符）的检验函数
def _loose_suffix_all_in_gc(m) -> bool:
    """
    LOOSE 匹配验证：名字部分（所有非姓氏字符）至少 50%（向上取整）
    在 _gc 中，防止 LOOSE 错误匹配"姓氏 + 非名字用字"组合。
    例如：
      - "方培培" suffix="培培" → 1/2=50% → ceil=1 → 1∈_gc ✓ → 接受
      - "李南" suffix="南" → 1/1=100% → ceil=1 → 1∈_gc ✓ → 接受
      - "邱向坤" suffix="向坤" → 1/2=50% → ceil=1 → 1∈_gc ✓ → 接受
      - "州银行" suffix="州银行" → 0/3=0% → ceil=2 → 0<2 ✗ → 拒绝
    """
    full = m.group(0)
    # 找出姓氏长度（复姓 2 字，或单姓 1 字）
    if _is_compound_surname_match(m):
        for cs in COMPOUND_SURNAMES:
            if full.startswith(cs):
                surname_len = len(cs)
                break
    else:
        surname_len = 1  # 单姓 1 字
    suffix = full[surname_len:]
    if not suffix:
        return True  # 无后缀（不可能在此函数中遇到，但保险）
    in_gc_count = sum(1 for ch in suffix if ch in _gc)
    threshold = (len(suffix) + 1) // 2  # 向上取整：2-char→1, 3-char→2
    return in_gc_count >= threshold


def apply_name_redactions(text):
    """
    姓名脱敏 v5：
      Layer1 上下文标签（高可信度，直接替换）
      Layer2 姓氏库 + 前缀边界 + 后缀黑名单 + 名字用字验证
    """
    if not text:
        return text

    # Layer 1: 上下文标签匹配（高可信度）
    for pat in NAME_LABEL_PATTERNS:
        text = pat.sub(lambda m: m.group(0).replace(m.group(1), 'XXX'), text)

    def should_replace(m):
        """
        判断姓名正则匹配结果是否应被替换为 XXX。
        拒绝条件（满足任一即拒绝）：
        1. 匹配词本身在保护完整词集中（如"马上"/"责任人"是词组，非姓名）
        2. 匹配词前面紧邻 CJK 字符，且 CJK+full 是已知组合词（如"运营部韩慧丽"中的"责任人"）
        3. 后缀黑名单：黑名单词紧跟匹配词开头（startswith）
        4. 名字用字验证：姓氏之后的所有字符必须全在 _gc 中
           （LOOSE 模式不限制后续字符，STRICT 模式已通过 _gc 限制；
           加上此检查可防止 LOOSE 错误匹配如"方案介绍"）
        """
        full = m.group(0)
        start = m.start()
        end = m.end()

        # 1. 保护完整词
        if full in {'责任人', '马上', '负责人', '主办人'}:
            return False

        # 2. 前置CJK感知：匹配词前面紧邻CJK字符，且该CJK+full是已知组合词
        if start > 0 and '\u4e00' <= text[start - 1] <= '\u9fff':
            combined = text[start - 1] + full
            if combined in {'责任人', '主办人', '承办人'}:
                return False

        # 3. 后缀黑名单：黑名单词必须紧跟（startswith）才算命中
        rest = text[end:]
        for sfx in NAME_SUFFIX_BLACKLIST:
            if rest.startswith(sfx):
                return False

        # 4. 名字用字验证：姓氏之后的所有字符必须全在 _gc 中
        #    STRICT 匹配天然满足此条件（其正则已限制名字用字为 _gc）；
        #    LOOSE 匹配若名字部分含非 _gc 字符（如"方案介绍"中的"案"）则拒绝
        suffix_chars = _loose_suffix_all_in_gc(m)
        if not suffix_chars:
            return False

        return True

    # STRICT 优先（保守模式先匹配完整常用名用字）
    text = NAME_PATTERN_STRICT.sub(lambda m: 'XXX' if should_replace(m) else m.group(0), text)
    # LOOSE 兜底（防止罕见名字被遗漏）
    text = NAME_PATTERN_LOOSE.sub(lambda m: 'XXX' if should_replace(m) else m.group(0), text)
    return text


# ---------------------------------------------------------------------------
# 第二部分：其他8类脱敏规则（按执行顺序）
# ---------------------------------------------------------------------------

def _apply_context_aware_sub(text, pattern, replacement):
    """
    对 TEXT 规则中需要上下文感知的模式做智能替换。
    对于"方案"：若其前面是CJK字符（如"工作方案"/"质量管理方案"），
    或其后面紧跟"介绍"（形成"方案介绍"完整词），则不替换。
    """
    def replacer(m):
        matched = m.group(0)
        start = m.start()
        # 前面有CJK → 可能是词组的一部分（如"工作方案"），跳过
        if start > 0 and '\u4e00' <= text[start - 1] <= '\u9fff':
            return m.group(0)
        # 特殊：若"方案"后面紧跟"介绍"（形成"方案介绍"），跳过
        if matched == '方案' and text[start + len('方案'):start + len('方案') + 2] == '介绍':
            return m.group(0)
        return replacement
    return pattern.sub(replacer, text)

TEXT_RULES = [
    # ① 邮箱
    (re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'), 'XXXXX@XXXXX'),
    # ② 详细地址
    (re.compile(
        r'[\u4e00-\u9fa5]{2,6}(?:省|自治区|市|特别行政区)?'
        r'[\u4e00-\u9fa5]{0,10}(?:市|区|县)?'
        r'[\u4e00-\u9fa5]{0,10}'
        r'(?:街|路|道|巷|弄|号|大道|大街|东路|西路|南路|北路)'
        r'[\u4e00-\u9fa50-9\-\s]{0,30}'
    ), 'XX省XX市XX区XXXX'),
    # ③ 身份证
    (re.compile(r'\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b'),
     'XXXXXXXXXXXXXXXXXX'),
    # ④ 银行卡
    (re.compile(r'\b(?:\d{16}|\d{17}|\d{18}|\d{19})\b'), 'XXXXXXXXXXXXXXXX'),
    # ⑤ 日期（多种格式统一）
    # 策略：日期+时间范围用后向引用保留时间范围部分；普通日期直接替换
    # 规则1：日期+时间范围（无空格：2017年4月11日15:30-16:30）
    # 加负向前瞻 (?!\s) 防止匹配已转换文本 "YYYY/MM/DD 15:30-16:30" 中的日期
    (re.compile(
        r'(\d{4}年\d{1,2}月\d{1,2}日)(?!\s)(\d{1,2}:\d{2}[-–]\d{1,2}:\d{2})'
    ), r'YYYY/MM/DD \2'),
    # 规则2：日期+时间范围（有空格：2017年4月11日 15:30-16:30）
    # 只处理原始文本，不处理规则1已转换的结果
    (re.compile(
        r'(\d{4}年\d{1,2}月\d{1,2}日)(\s+\d{1,2}:\d{2}[-–]\d{1,2}:\d{2})'
    ), r'YYYY/MM/DD\2'),
    # 规则3：普通日期格式（无时间）
    (re.compile(
        r'\d{4}年\d{1,2}月\d{1,2}日'
        r'|\d{4}-\d{1,2}-\d{1,2}'
        r'|\d{4}/\d{1,2}/\d{1,2}'
        r'|\d{4}\.\d{1,2}\.\d{1,2}'
    ), 'YYYY/MM/DD'),
    # ⑥ 手机号
    (re.compile(r'\b1[3-9]\d{9}\b'), 'XXXXXXXXXXX'),
    # ⑦ 固话
    (re.compile(r'0\d{2,3}[-\s]?\d{7,8}'), '0XX-XXXXXXXX'),
    # ⑧ 银行名称（上下文感知由 _apply_context_aware_sub 处理）
    (re.compile(
        r'(?:中国|交通|招商|浦发|兴业|民生|华夏|平安|光大|广发|浙商|渤海|恒丰|'
        r'农业|建设|工商|南京|宁波|杭州|深圳|上海|北京|广州|郑州|中原|苏州|天津|'
        r'重庆|成都|武汉|西安|长沙|济南|青岛|大连|沈阳|哈尔滨|长春|厦门|福州|贵阳|'
        r'昆明|南宁|合肥|南昌|太原|石家庄|兰州|乌鲁木齐|呼和浩特|洛阳|开封|'
        r'稠州|民泰|泰隆|紫金|江南|无锡|齐鲁)'
        r'(?:银行|农商银行|农村商业银行|信用社|农信社|合作银行|人民银行)|'
        r'(?:农信社|信用社|农商银行|合作银行|人民银行|农村商业银行)'
    ), 'XX银行'),
    # ⑨ 方案（独立词，非CJK前缀词组的一部分，如"工作方案"/"XX方案"脱敏；
    #       但"管理方案""质量管理方案"等CJK前缀组合不替换）
    (re.compile(r'方案'), 'XXXX'),
]


def apply_text_redactions(text):
    if not text:
        return text
    for pat, repl in TEXT_RULES:
        pat_str = pat.pattern
        # ⑧银行名称和⑨方案：上下文感知（前面是CJK则跳过，防止"XX银行方案""质量管理方案"误脱敏）
        is_bank_rule = ('银行' in pat_str and
                        ('中国' in pat_str or '农业' in pat_str or '建设' in pat_str or
                         '工商' in pat_str or '交通' in pat_str or '招商' in pat_str or
                         '浦发' in pat_str or '郑州' in pat_str or '中原' in pat_str))
        is_plan_rule = (pat_str == r'方案')
        if is_bank_rule or is_plan_rule:
            text = _apply_context_aware_sub(text, pat, repl)
        else:
            text = pat.sub(repl, text)
    return text


def apply_all_redactions(text):
    """统一入口：8类文本规则 → 姓名规则"""
    text = apply_text_redactions(text)
    text = apply_name_redactions(text)
    return text


# ---------------------------------------------------------------------------
# 第三部分：文件名脱敏
# ---------------------------------------------------------------------------
BANK_NAME_RE = re.compile(
    r'(?:中国|交通|招商|浦发|兴业|民生|华夏|平安|光大|广发|浙商|渤海|恒丰|'
    r'农业|建设|工商|南京|宁波|杭州|深圳|上海|北京|广州|郑州|中原|苏州|天津|'
    r'重庆|成都|武汉|西安|长沙|济南|青岛|大连|沈阳|哈尔滨|长春|厦门|福州|贵阳|'
    r'昆明|南宁|合肥|南昌|太原|石家庄|兰州|乌鲁木齐|呼和浩特|洛阳|开封|'
    r'稠州|民泰|泰隆|紫金|江南|无锡|齐鲁)'
    r'(?:银行|农商银行|农村商业银行|信用社|农信社|合作银行|人民银行)|'
    r'(?:农信社|信用社|农商银行|合作银行|人民银行|农村商业银行)'
)


def redact_filename(filename):
    """对文件名执行脱敏：银行名→XX银行，日期→XXXX/XX/XX，手机号→XXXXXXXXXXX"""
    redacted = filename
    redacted = BANK_NAME_RE.sub('XX银行', redacted)
    redacted = re.compile(r'\d{4}[-_年]\d{1,2}[-_月]\d{1,2}[日]?').sub('XXXX/XX/XX', redacted)
    redacted = re.compile(r'\d{4}年\d{1,2}月\d{1,2}日').sub('XXXX/XX/XX', redacted)
    redacted = re.compile(r'1[3-9]\d{9}').sub('XXXXXXXXXXX', redacted)
    return redacted


def _replace_image_with_black(image_path: Path) -> None:
    from PIL import Image
    try:
        img = Image.open(image_path)
        w, h = img.size
        Image.new('RGB', (max(w, 10), max(h, 10)), (0, 0, 0)).save(image_path)
    except Exception as e:
        print(f"  [警告] 无法处理图片 {image_path}: {e}")



def _process_xml_deep_v2(xml_text: str) -> str:
    """
    以段落为单元，先拼接所有 <w:t> 文本再全量正则处理，
    解决文字被拆到相邻 <w:r>/<w:t> 中导致的跨碎片漏匹配问题。
    """
    import re as _re

    paragraph_pattern = _re.compile(r'<w:p\b[^>]*>.*?</w:p>', _re.DOTALL)
    t_re = _re.compile(r'(<w:t[^>]*>)([^<]*)(</w:t>)')

    def _should_replace_name_v2(m, text) -> bool:
        full = m.group(0)
        start = m.start()
        end = m.end()
        if full in {'责任人', '马上', '负责人', '主办人'}:
            return False
        if start > 0 and '\u4e00' <= text[start - 1] <= '\u9fff':
            combined = text[start - 1] + full
            if combined in {'责任人', '主办人', '承办人'}:
                return False
        rest = text[end:]
        for sfx in NAME_SUFFIX_BLACKLIST:
            if rest.startswith(sfx):
                return False
        if not _loose_suffix_all_in_gc(m):
            return False
        return True

    def _collect_replacements(text):
        """收集所有脱敏替换：(orig_start, orig_end, replacement_str)"""
        results = []

        def _collect(pat, repl):
            for m in pat.finditer(text):
                results.append((m.start(), m.end(), repl))

        for pat, repl in TEXT_RULES:
            pat_str = pat.pattern
            is_bank = ('\u94f6\u884c' in pat_str and
                      ('\u4e2d\u56fd' in pat_str or '\u519c\u4e1a' in pat_str or
                       '\u5efa\u8bbe' in pat_str or '\u5de5\u5546' in pat_str or
                       '\u4ea4\u901a' in pat_str or '\u62db\u5546' in pat_str or
                       '\u6d66\u53d1' in pat_str or '\u90d1\u5dde' in pat_str or
                       '\u4e2d\u539f' in pat_str))
            is_plan = (pat_str == r'\u65b9\u6848')
            if is_bank or is_plan:
                for m in pat.finditer(text):
                    s, e = m.start(), m.end()
                    if is_bank and s > 0 and '\u4e00' <= text[s - 1] <= '\u9fff':
                        continue
                    if is_plan:
                        if s > 0 and '\u4e00' <= text[s - 1] <= '\u9fff':
                            continue
                        if e < len(text) and text[e:e + 2] == '\u4ecb\u7ecd':
                            continue
                    results.append((s, e, repl))
            else:
                _collect(pat, repl)

        # 姓名层1：STRICT
        strict_ranges = []
        for m in NAME_PATTERN_STRICT.finditer(text):
            if _should_replace_name_v2(m, text):
                results.append((m.start(), m.end(), 'XXX'))
                strict_ranges.append((m.start(), m.end()))
        # 姓名层2：LOOSE
        for m in NAME_PATTERN_LOOSE.finditer(text):
            if any(s <= m.start() < e for s, e in strict_ranges):
                continue
            if _should_replace_name_v2(m, text):
                results.append((m.start(), m.end(), 'XXX'))

        # 排序并过滤重叠
        results.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        filtered = []
        last_end = 0
        for s, e, r in results:
            if s >= last_end:
                filtered.append((s, e, r))
                last_end = e
        return filtered

    def process_paragraph(pm: _re.Match) -> str:
        pg_xml = pm.group(0)
        segs = [{'pre': m.group(1), 'text': m.group(2), 'suf': m.group(3)}
                for m in t_re.finditer(pg_xml)]
        if not segs:
            return pg_xml

        orig_concat = ''.join(seg['text'] for seg in segs)
        if not orig_concat:
            return pg_xml

        replacements = _collect_replacements(orig_concat)
        if not replacements:
            return pg_xml

        # 建立 orig_pos → redacted_pos 的映射
        # 对于 orig 位置 p：
        #   n = p - orig_repl_end_of_prev_repl
        #   redacted_pos = sum_of_all_chars_before_p + n
        # 其中 n 是在当前 replacement 内的偏移（如果 p 在某个 replacement 中）
        def orig_to_redacted_pos(p):
            total = 0
            prev_end = 0
            for s, e, r in replacements:
                if p < s:
                    # p 在非替换区段
                    return total + (p - prev_end)
                elif s <= p < e:
                    # p 在 replacement 区间
                    return total + (p - s)
                else:  # p >= e
                    total += (s - prev_end) + len(r)
                    prev_end = e
            return total + (p - prev_end)

        # 构建 redacted_concat（使用映射直接生成）
        redacted_len = orig_to_redacted_pos(len(orig_concat))
        redacted_concat_chars = []
        p = 0
        for s, e, r in replacements:
            if p < s:
                redacted_concat_chars.append(orig_concat[p:s])
                p = s
            redacted_concat_chars.append(r)
            p = e
        if p < len(orig_concat):
            redacted_concat_chars.append(orig_concat[p:])
        redacted_concat = ''.join(redacted_concat_chars)

        # 将 redacted_concat 按原始段长度逐一拆分
        new_texts = []
        for seg in segs:
            char_count = len(seg['text'])
            # 找到 seg 在 orig_concat 中的位置
            seg_start_in_orig = sum(len(s['text']) for s in segs[:segs.index(seg)])
            seg_end_in_orig = seg_start_in_orig + char_count
            red_start = orig_to_redacted_pos(seg_start_in_orig)
            red_end = orig_to_redacted_pos(seg_end_in_orig)
            new_texts.append(redacted_concat[red_start:red_end])

        # 重建段落 XML（从后向前替换，避免偏移）
        result_xml = pg_xml
        for seg, new_t in zip(reversed(segs), reversed(new_texts)):
            old_fragment = f"{seg['pre']}{seg['text']}{seg['suf']}"
            new_fragment = f"{seg['pre']}{new_t}{seg['suf']}"
            result_xml = result_xml.replace(old_fragment, new_fragment, 1)
        return result_xml

    return paragraph_pattern.sub(process_paragraph, xml_text)


def _process_xml_deep(input_zip_path: str, output_docx_path: str) -> int:
    """
    基于 docx（zip）深度处理所有 XML：
    正文/页眉/页脚/批注/文本框、文档属性、银行Logo图片
    """
    count = 0
    tmp_dir = Path(tempfile.mkdtemp(prefix='docx_redact_'))
    extract_dir = tmp_dir / 'extracted'
    extract_dir.mkdir()

    with zipfile.ZipFile(input_zip_path, 'r') as z:
        z.extractall(extract_dir)

    word_dir = extract_dir / 'word'

    # 所有 word/*.xml
    if word_dir.exists():
        for xml_file in word_dir.glob('*.xml'):
            c = xml_file.read_text('utf-8')
            o = c
            c = apply_all_redactions(c)
            if c != o:
                xml_file.write_text(c, 'utf-8')
                count += 1

    # 文档属性
    core_xml = extract_dir / 'docProps' / 'core.xml'
    if core_xml.exists():
        c = core_xml.read_text('utf-8')
        o = c
        c = apply_all_redactions(c)
        if c != o:
            core_xml.write_text(c, 'utf-8')
            count += 1

    # 银行Logo图片替换为纯黑图
    media_dir = word_dir / 'media'
    if media_dir.exists():
        for img_file in media_dir.iterdir():
            img_name = img_file.name.lower()
            if any(k in img_name for k in ['bank', 'logo', '\u94f6\u884c', 'icon', 'brand']):
                _replace_image_with_black(img_file)
                count += 1
                print(f"  [银行Logo] {img_file.name} -> 纯黑图")

    # 重新打包
    with zipfile.ZipFile(output_docx_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fp in extract_dir.rglob('*'):
            if fp.is_file():
                zf.write(fp, str(fp.relative_to(extract_dir)))

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return count


def redact_docx(input_path: str, output_path: str) -> None:
    from docx import Document
    import zipfile

    # Step 1: 对原始 XML 做段落级拼接脱敏（解决跨 run 漏脱敏问题）
    xml_count = 0
    tmp_dir = Path(tempfile.mkdtemp(prefix='docx_redact_'))
    extract_dir = tmp_dir / 'extracted'
    extract_dir.mkdir()

    with zipfile.ZipFile(input_path, 'r') as z:
        z.extractall(extract_dir)

    # 处理 document.xml（正文）
    doc_xml_path = extract_dir / 'word' / 'document.xml'
    if doc_xml_path.exists():
        doc_xml = doc_xml_path.read_text('utf-8')
        patched = _process_xml_deep_v2(doc_xml)
        if patched != doc_xml:
            doc_xml_path.write_text(patched, 'utf-8')
            xml_count += 1

    # 处理文档属性 core.xml
    core_xml = extract_dir / 'docProps' / 'core.xml'
    if core_xml.exists():
        c = core_xml.read_text('utf-8')
        o = c
        c = apply_all_redactions(c)
        if c != o:
            core_xml.write_text(c, 'utf-8')

    # 重新打包为 docx
    tmp_docx = tmp_dir / 'redacted.docx'
    with zipfile.ZipFile(str(tmp_docx), 'w', zipfile.ZIP_DEFLATED) as zf:
        for fp in extract_dir.rglob('*'):
            if fp.is_file():
                zf.write(fp, str(fp.relative_to(extract_dir)))

    # Step 2: 直接将 Step1 处理好的 document.xml 写回 docx
    # 注意：不能用 python-docx 重新保存（会覆盖我们精心处理的 XML）
    with zipfile.ZipFile(str(tmp_docx), 'r') as zin:
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == 'word/document.xml':
                    # 写入已处理好的 patched 版本
                    zout.writestr(item, patched.encode('utf-8'))
                else:
                    zout.writestr(item, zin.read(item.filename))

    # 清理 Step1 的临时目录
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # 银行 Logo 图片替换（直接在输出文件上操作）
    tmp_dir2 = Path(tempfile.mkdtemp(prefix='docx_logo_'))
    extract_dir2 = tmp_dir2 / 'extracted2'
    extract_dir2.mkdir()
    with zipfile.ZipFile(output_path, 'r') as z:
        z.extractall(extract_dir2)
    media_dir2 = extract_dir2 / 'word' / 'media'
    logo_replaced = False
    if media_dir2.exists():
        for img_file in media_dir2.iterdir():
            img_name = img_file.name.lower()
            if any(k in img_name for k in ['bank', 'logo', '\u94f6\u884c', 'icon', 'brand']):
                _replace_image_with_black(img_file)
                logo_replaced = True
    if logo_replaced:
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for fp in extract_dir2.rglob('*'):
                if fp.is_file():
                    zf.write(fp, str(fp.relative_to(extract_dir2)))
    shutil.rmtree(tmp_dir2, ignore_errors=True)

    print(f"[完成] 共遮盖 {xml_count} 处（正文 {xml_count} 处）\n         输出: {output_path}")


def redact_doc_to_docx(input_path: str, output_path: str) -> None:
    import subprocess
    stem = Path(input_path).stem
    tmp_docx = str(Path(input_path).with_name(f"{stem}_converted.docx"))
    result = subprocess.run(
        ['textutil', '-convert', 'docx', '-output', tmp_docx, input_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[错误] .doc 转换失败: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    redact_docx(tmp_docx, output_path)
    Path(tmp_docx).unlink(missing_ok=True)


def main():
    if len(sys.argv) < 2:
        print("用法: python3 redact_word.py <输入文件路径> [输出文件路径]")
        sys.exit(1)

    input_file = sys.argv[1]
    ext = Path(input_file).suffix.lower()
    stem = Path(input_file).stem

    # 文件名脱敏：银行名/日期/手机号
    redacted_stem = redact_filename(stem)

    if len(sys.argv) > 2:
        # 对显式输出路径的文件名部分也做脱敏
        out_path = Path(sys.argv[2])
        out_stem = redact_filename(out_path.stem)
        output_file = str(out_path.with_name(f"{out_stem}{out_path.suffix}"))
    else:
        output_file = str(Path(input_file).with_name(f"{redacted_stem}_脱敏{ext}"))

    print(f"[输入] {input_file}")
    print(f"[文件名脱敏] {stem} -> {redacted_stem}")

    if ext == '.doc':
        redact_doc_to_docx(input_file, output_file)
    else:
        redact_docx(input_file, output_file)


if __name__ == '__main__':
    main()
