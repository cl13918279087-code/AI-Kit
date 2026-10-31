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
import shutil
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

# ---------------------------------------------------------------------------
# Agent 评审决策上下文（方式一：分阶段交互）
# ---------------------------------------------------------------------------
GRAY_CONFIDENCE_THRESHOLD = 0.75  # ≥ 此值 → 自动遮盖；< 此值 → 灰区交 Agent 评审

# Agent 评审决策文件结构说明（JSON）：
# [
#   {"text": "张明", "decision": "skip",  "reason": "上下文为编号，非姓名"},
#   {"text": "黎明", "decision": "redact", "reason": "确认为人名"}
# ]

# 全局 Agent 评审决策路径（由 redact_all.py 调用 set_agent_decisions_override() 设置）
_AGENT_DECISIONS_OVERRIDE: str = None

# ---------------------------------------------------------------------------
# D15（赵辉，第5批，2026-10-09）：表格列姓名预扫 → 全局注入
# 表头驱动列姓名预扫 → 注入本集合 → 全文同名替换
# 防止正文引用"王根利的B角"等不在表格列内的人名漏脱
# ---------------------------------------------------------------------------
_EXTRA_NAMES: set = set()


def inject_extra_names(names: set) -> None:
    """注入表格列预扫收集到的额外姓名，供 apply_redactions_counted 全局使用"""
    global _EXTRA_NAMES
    _EXTRA_NAMES.update(names)


def get_extra_names() -> set:
    """返回当前已注入的额外姓名集合"""
    return _EXTRA_NAMES


def clear_extra_names() -> None:
    """清空额外姓名集合（文档处理完成后调用）"""
    global _EXTRA_NAMES
    _EXTRA_NAMES.clear()


def set_agent_decisions_override(path: str) -> None:
    """设置 Agent 评审决策文件路径（由 redact_all.py 调用）"""
    global _AGENT_DECISIONS_OVERRIDE
    _AGENT_DECISIONS_OVERRIDE = path


def is_gray_entity(confidence: float) -> bool:
    """判断实体是否为灰区（需 Agent 评审）"""
    return confidence < GRAY_CONFIDENCE_THRESHOLD


def _get_skip_texts() -> set:
    """从决策文件加载需跳过的实体文本集合"""
    if not _AGENT_DECISIONS_OVERRIDE:
        return set()
    decisions = load_agent_decisions(_AGENT_DECISIONS_OVERRIDE)
    return {t for t, d in decisions.items() if d == "skip"}


def load_agent_decisions(path: str) -> Dict[str, str]:
    """
    加载 Agent 评审决策，返回 {text: decision} 映射。
    decision = "skip" 表示跳过（不遮盖），"redact" 表示遮盖。
    文件不存在或解析失败时返回空字典（不影响正常流程）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            decisions = json.load(f)
        result = {}
        for item in decisions:
            t = item.get("text", "")
            d = item.get("decision", "")
            if t and d:
                result[t] = d
        return result
    except Exception:
        return {}

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
    # Issue #49：允许通过环境变量指定配置文件路径（测试隔离用）。
    # 测试不再覆写/删除仓库内真实 config.json，避免破坏性副作用。
    env_cfg = os.environ.get("AIKIT_CONFIG_PATH", "").strip()
    if env_cfg:
        config_paths.insert(0, Path(env_cfg))
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
# M9（Issue #49，2026-10）：字池载入期清洗
#
# 背景：#45 扩池时把 17 个非汉字字符误采进 name_pool：
#   - 2 个标点（`、` `/`）—— 真正的污染。规则A 的名字字符类
#     `姓 + [name_pool]{1,3}` 会把「李思远、」当成 4 字姓名，
#     导致名单整段吞并（"项目组成员如下：XXX…" 把「负责需求梳理」一起吃掉）。
#   - 15 个大写字母（X L J S H C T N Z G E B X D K）—— 扩池时误采的冗余项，
#     会被规则A 当作"名字用字"，使「张三ABC」「李四X光」类混合串被误判为姓名。
#     注：脱敏占位符（XXX / XXXXXXXXXXXXXXXXXX / YYYY/MM/DD）实际来自
#     config.json 的 `replacement` 字段，与 name_pool 无耦合 —— 故字母污染
#     的危害独立于 M9 存在，由规则层的「姓名形态须全为汉字」约束另行处理。
#
# M9 的准确口径（见 docs/Issue-049-姓名池误伤整改方案.md §五）：
#   **移除标点类字符，保留占位符字母**；字母污染改由规则层的
#   「姓名形态判定须全为汉字」约束处理。
#
# 为什么必须在**载入期**清洗而不能只改 config.json：
#   字池是人工维护的，后续任何一次扩池都可能再次混入标点。
#   只改数据等于把"下次再犯"的成本留给未来的维护者；
#   在载入期做一次结构性拦截，才能让 M9 真正根治。
#
# 清洗规则：**只剔除标点/符号类字符（Unicode category P*/S*）**，
# 汉字与占位符大写字母一律保留 —— 字母问题的处置不在 M9 范围内。
# ---------------------------------------------------------------------------

# 正则字符类中需要转义的元字符（即便清洗后也应防御性排除）
_NAME_POOL_EXCLUDE_META = set("[]^-\\")

# 脱敏占位符字母集：与 config.json replacement 字段保持一致
# （NAME=XXX / ID_CARD=X*18 / DATE=YYYY/MM/DD / EMAIL / IP / MAC / MOBILE / PHONE）
_PLACEHOLDER_LETTERS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _is_name_pool_pollution(ch: str) -> bool:
    """判断单个字符是否为 name_pool 污染（标点/符号类）。

    保留：汉字 + 占位符大写字母（`_PLACEHOLDER_LETTERS`）。
    剔除：标点、符号，以及**小写字母**（占位符集只用大写，
    小写字母进入名字字符类会额外扩大匹配面且无占位符用途）。
    """
    if ch in _NAME_POOL_EXCLUDE_META:
        return True
    # ASCII 字母：仅保留占位符所用的大写
    if ch.isascii() and ch.isalpha():
        return ch not in _PLACEHOLDER_LETTERS
    # ASCII 可见的非字母数字字符（标点、符号）
    if ch.isascii() and not ch.isalnum():
        return True
    # Unicode 标点与符号类别（顿号、斜杠、全角标点、破折号等）
    try:
        import unicodedata
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("S"):
            return True
    except Exception:
        pass
    return False


def _clean_name_pool(pool: str) -> str:
    """清洗 name_pool：剔除标点/符号类污染，保留汉字与占位符字母。

    M9（Issue #49）。返回保持原顺序、去重后的字符序列。
    """
    if not pool:
        return ""
    seen = set()
    out = []
    for ch in pool:
        if ch in seen:
            continue
        if _is_name_pool_pollution(ch):
            continue
        seen.add(ch)
        out.append(ch)
    return "".join(out)


def get_name_pool_chars() -> str:
    """读取清洗后的 name_pool 字符序列（供规则层与 R4 共用）。"""
    cfg = _load_config()
    pool = cfg.get("name_pool", "")
    if not pool:
        return ""
    return _clean_name_pool(pool)


# D16 精确全名库（Issue #50 N-C 层需要「D16 命中」作为区域判定的种子信号）
_FULL_NAME_SET_CACHE: Optional[set] = None


def get_full_name_set() -> set:
    """返回 D16 精确全名库（已剔除字段标签型污染条目）。

    Issue #50 N-C 区域检测层把「区域内至少一项命中 D16」作为
    上下文性准则的种子信号（§4.3 准则 2 / §4.5）。
    """
    global _FULL_NAME_SET_CACHE
    if _FULL_NAME_SET_CACHE is None:
        cfg = _load_config()
        names = cfg.get("full_name_list", []) or []
        _FULL_NAME_SET_CACHE = {n for n in names if n not in _D16_NON_NAME_ENTRIES}
    return _FULL_NAME_SET_CACHE



# 内置兜底名字池：config.json 的 name_pool 缺失或清洗后为空时启用。
# 作用有二：
#   ① 保证姓名规则始终有可用的名字字符类（空池会让字符类退化为 `[]`）；
#   ② 空字符类会破坏正则分组编号，导致 _conj_repl 抛 IndexError。
_FALLBACK_NAME_POOL = (
    "伟强志建华文静宇轩浩然俊杰明辉晨曦鹏飞洪红霞丽娟秀英敏芳兰婷玉军平立业德永海波涛"
    "清北胜利福生财腾广坤传王泓郭艳林微陈卓怡君佩瑶心如梦雨萱晓思彤欣涵晖润峰山宏翠"
    "冰勃项韬鑫昊毅春为斌少凡梅娥昀铄智诗标芸仁铭侃楷肇乐曹潘李张欧詹郑丁周娜萍燕雅雯"
    "菲彩佳倩洁慧琳芬蓉澜蕊黛媛娇璐豪超刚勇磊龙荣逸朗天行健自不息东宁瑀"
)

# 原始池字符串 → 清洗后序列（含兜底）的缓存。
# 只保留最新一项，避免热路径反复清洗带来运行期开销。
_NAME_POOL_CACHE: Dict[str, str] = {}


def _get_name_pool_chars(cfg: Optional[Dict] = None) -> str:
    """取清洗后的 name_pool 字符序列；为空时回退到内置兜底池。

    M9（Issue #49）新增。规则层所有构造名字字符类的位置都应走本函数，
    避免空池导致字符类退化为 `[]`（进而破坏正则分组编号）。

    清洗按原始池字符串做缓存——XX 恢复段在每次 apply_redactions 调用中
    都会取用名字池，缓存后运行期零开销。
    """
    if cfg is None:
        cfg = _load_config()
    raw = cfg.get("name_pool", "") or ""
    cached = _NAME_POOL_CACHE.get(raw)
    if cached is not None:
        return cached
    chars = _clean_name_pool(raw) or _FALLBACK_NAME_POOL
    _NAME_POOL_CACHE.clear()
    _NAME_POOL_CACHE[raw] = chars
    return chars


def _build_name_char_class(chars: str) -> str:
    """由名字用字序列构造安全的正则字符类。

    - 空序列回退到内置兜底池（`[]` 会让 re 报 unterminated character set）；
    - 转义 `]`、`^`、`-`、反斜杠等正则元字符，避免污染字符破坏字符类边界。
    """
    chars = chars or _FALLBACK_NAME_POOL
    escaped = "".join("\\" + c if c in "\\]^-[" else c for c in chars)
    return "[" + escaped + "]"


# ---------------------------------------------------------------------------
# Issue #49 P1：分级规则集（姓氏分档 + 名字位字频闸门）
# ---------------------------------------------------------------------------
#
# 背景：#45 扩池后 surname_pool×name_pool 笛卡尔积从 ≈3 万扩到 ≈53 万，
# 其中超出真实人名增量的部分全是日常汉语常用字，导致规则A 大面积误伤。
#
# P1 的判别主杠杆是「名字位字频闸门」——只允许在真实人名里高频出现的字
# 充当名字位。字频来源是项目自带的 D16 全名库（full_name_list 7398 条）
# **自举**：统计每个名字位（name[1:]）的字频，无需外部词表、无外部依赖。
#
# 关键设计：闸门用**字符类收紧**实现（名字字符类只含高频字），
# 而不是在替换回调里做后置判定。原因是贪婪量词下后置判定会漏脱：
#   "张明和" —— 全池字符类会先匹到 "张明和"，回调判定 "和" 非高频字而整体
#   放弃 → "张明" 反而漏脱；字符类收紧则只匹 "张明"，"和" 留在外面。
#
# 姓名整体匹配（而非逐字）的取舍依据不变：漏脱属合规红线，误伤属可用性缺陷。

# 名字位字频闸门参数（可经 config.json 覆盖）
_NAME_GATE_MIN_FREQ = 12          # 名字位「热字」下限（尾字必须达到）
_NAME_GATE_WARM_MIN_FREQ = 3      # 名字位「温字」下限（非尾字允许达到）
_NAME_GATE_SURNAME_MIN_FREQ = 5   # 姓氏位字频下限（D16 库中作姓出现的次数）
_NAME_GATE_LOW_RANGE = (1, 3)     # 低危姓：姓 + 1~3 名字字
_NAME_GATE_HIGH_RANGE = (2, 3)    # 高危姓：姓 + 2~3 名字字
_NAME_GATE_ANCHOR_RANGE = (1, 2)  # 上下文锚点：锚点 + 任意姓 + 1~2 名字字

# 上下文锚点词表（复用 #38 D8/D11 角色槽位）
_NAME_GATE_ANCHORS = (
    "负责人", "联系人", "成员", "人员", "组长", "经理", "主任", "总监", "名单",
    "参会", "出席", "签名", "签字", "汇报", "由", "请", "联系", "支持人员",
    "配合人", "经办人", "办理人", "授权人", "审批人", "复核人", "申请人",
    "填报人", "签字栏",
)

# 常见复姓（Issue #49 P1 新增）：无复姓表时「司马光」「欧阳娜娜」等
# 会被拆成「司 + 马光」而漏掉末字，或让「马」单独充当姓。
_NAME_GATE_COMPOUND_SURNAMES = (
    "欧阳", "司马", "上官", "诸葛", "夏侯", "皇甫", "尉迟", "长孙", "慕容",
    "司徒", "司空", "独孤", "南宫", "西门", "东方", "令狐", "宇文", "端木",
    "公孙", "轩辕", "澹台", "闻人", "第五", "梁丘", "左丘", "东郭", "仲孙",
    "钟离", "闾丘", "夹谷", "宰父", "谷梁", "拓跋", "百里", "呼延", "东野",
    "羊舌", "微生", "公良", "太叔", "申屠", "公羊", "乐正", "壤驷", "公冶",
    "淳于", "太叔",
)

# D16 全名库中的「非人名」条目（字段标签 / 表头），必须排除出精确匹配层。
# 背景：实测 full_name_list 含「姓名」——它是文档字段名不是人名，
# 精确层会把它替换成 XXX（「请填写姓名」→「请填写XXX」），属误伤。
_D16_NON_NAME_ENTRIES = frozenset({"姓名"})

# D16 全名库名字位字频缓存（按全名库内容 key）
_NAME_CHAR_FREQ_CACHE: Dict[str, Any] = {}


def _get_name_char_freq(cfg: Optional[Dict] = None) -> Dict[str, int]:
    """从 D16 全名库自举名字位字频。

    统计 full_name_list 中每个名字位（name[1:]，跳过姓位）的字频。
    数据源完全自洽——不依赖 jieba、不依赖外部词表。

    返回 {字: 字频}；全名库缺失时返回空 dict（调用方须降级到全池）。
    """
    if cfg is None:
        cfg = _load_config()
    names = cfg.get("full_name_list") or []
    key = f"{len(names)}:{hash(tuple(names[:64]))}"
    cached = _NAME_CHAR_FREQ_CACHE.get(key)
    if cached is not None:
        return cached
    freq: Dict[str, int] = {}
    for name in names:
        for ch in name[1:]:
            freq[ch] = freq.get(ch, 0) + 1
    _NAME_CHAR_FREQ_CACHE.clear()
    _NAME_CHAR_FREQ_CACHE[key] = freq
    return freq


def _get_gated_name_chars(cfg: Optional[Dict] = None) -> str:
    """取名字位「热字」序列（已与清洗后的 name_pool 取交集）。

    - 闸门阈值：名字位字频 ≥ `name_gate_min_freq`（默认 12，对应 D16 库
      总字次的约千分之一，实测 261/1278 字通过）；
    - 与 `name_pool` 取交集：闸门只做「收紧」不做「扩张」，
      避免把 name_pool 之外的字引入字符类；
    - 交集为空时回退到全池（闸门不可用时宁可多误伤也不漏脱）。
    """
    if cfg is None:
        cfg = _load_config()
    pool_chars = _get_name_pool_chars(cfg)
    if not pool_chars:
        return ""
    threshold = cfg.get("name_gate_min_freq", _NAME_GATE_MIN_FREQ)
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        threshold = _NAME_GATE_MIN_FREQ
    if threshold <= 0:
        return pool_chars
    freq = _get_name_char_freq(cfg)
    if not freq:
        return pool_chars
    hot = {ch for ch, f in freq.items() if f >= threshold}
    gated = "".join(ch for ch in pool_chars if ch in hot)
    return gated or pool_chars


def _get_warm_name_chars(cfg: Optional[Dict] = None) -> str:
    """取名字位「温字」序列（名字位非尾字允许的字）。

    阈值 `name_gate_warm_freq`（默认 3）：D16 全名库中作为名字位出现 ≥3 次。

    **为什么需要「温字」这一档**（Issue #49 P1 实证）：
    「热字独占」判据（名字位每字都必须是热字）会漏掉真实人名 ——
    「王根利」的「根」在 D16 名字位仅出现 3 次，但它是真实人名用字；
    而「温字 + 尾字热」判据（尾字必须是热字，前 1~2 字允许温字）
    既保住「王根利」，又能挡住「流程优化」这类
    「冷姓 + 冷字 + 冷字」的常用词结构。

    实证（102 条通用语料 / 12 条真名）：
      尾字热 + 前字温（尾字热字 + 前 ≤2 温字） → 误伤 9、真名漏 1（樊霖）
      尾字热 + 前字全热                       → 误伤 12、真名漏 1
    """
    if cfg is None:
        cfg = _load_config()
    pool_chars = _get_name_pool_chars(cfg)
    if not pool_chars:
        return ""
    threshold = cfg.get("name_gate_warm_freq", _NAME_GATE_WARM_MIN_FREQ)
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        threshold = _NAME_GATE_WARM_MIN_FREQ
    if threshold <= 0:
        return pool_chars
    freq = _get_name_char_freq(cfg)
    if not freq:
        return pool_chars
    warm = {ch for ch, f in freq.items() if f >= threshold}
    gated = "".join(ch for ch in pool_chars if ch in warm)
    return gated or pool_chars


def _build_name_pos_class(hot_chars: str, warm_chars: str,
                          min_len: int, max_len: int) -> str:
    """按**名字位字数**显式构造名字位正则片段（Issue #49 P1）。

    语义（必须显式按长度展开，不能写成 `TAIL{n,m}`）：
      - 名字位长度 k 的形态 = (k-1) 个温字 + 1 个尾字；
      - 尾字必须达到**热字**阈值（名字位字频 >= `name_gate_min_freq`）；
      - 非尾字允许温字（字频 >= `name_gate_warm_freq`）。

    为什么必须按长度显式展开：
      旧实现返回 `[温]{0,2}[热]`，规则层再套 `{1,3}` -> 实际形态是
      `[温]{0,2}[热]{1,3}`，**量词落在尾字类上**而非名字位上：
        「李思远」可被拆成 温+热+热+热（4 字），尾字判定完全失效；
        「流程优化」这类「冷姓 + 冷字 + 冷字」也因尾字可重复而漏挡。
      显式展开后长度约束落在名字位整体，尾字判定与字数约束同时成立。

    返回片段示例（hot != warm 时）：
      `[温]{0,2}[热]`   -> 名字位 1~3 字，尾字热
      `[温]{1,2}[热]`   -> 名字位 2~3 字，尾字热
    当热字集与温字集相同（闸门退化到同一档）时，退化为普通长度量词。
    """
    hot = _build_name_char_class(hot_chars)
    warm = _build_name_char_class(warm_chars)
    if hot == warm:
        # 闸门不可用（热/温同档或退化到全池）时退化为普通长度量词
        return f"{hot}{{{min_len},{max_len}}}"
    lo = max(0, min_len - 1)
    hi = max(0, max_len - 1)
    return f"{warm}{{{lo},{hi}}}{hot}"


def _get_surname_char_freq(cfg: Optional[Dict] = None) -> Dict[str, int]:
    """从 D16 全名库自举姓氏位字频（name[0] 位置的字频）。

    与名字位字频分开统计：同一个字在姓位与名字位的分布截然不同
    （如「明」名字位频次 146、姓位频次仅 2）。
    """
    if cfg is None:
        cfg = _load_config()
    names = cfg.get("full_name_list") or []
    key = f"s:{len(names)}:{hash(tuple(names[:64]))}"
    cached = _NAME_CHAR_FREQ_CACHE.get(key)
    if cached is not None:
        return cached
    freq: Dict[str, int] = {}
    for name in names:
        if name:
            freq[name[0]] = freq.get(name[0], 0) + 1
    _NAME_CHAR_FREQ_CACHE[key] = freq
    return freq


def _get_gated_surnames(cfg: Optional[Dict] = None) -> List[str]:
    """取姓氏位闸门放行的姓氏（按字序列出，保持确定性顺序）。

    背景：残余误伤的真实入口是**姓氏位**，不是名字位。
    实测误伤样本的结构是「冷僻姓 + 高频名字字」：
        水平 = 水(姓位频次 0) + 平     单元 = 单(6) + 元
        沙子 = 沙(1) + 子              明天 = 明(2) + 天
    这些字作姓氏极罕见（D16 全名库中几乎不作为姓出现），
    它们在句中作首字时是**词**不是**姓**（水平/单元/沙子/明天）。

    阈值 `name_surname_min_freq` 默认 5——D16 库中作为姓氏出现 ≥5 次。
    门槛不能更高：「韩」(47)、樊(15)这类真实姓氏的频次并不高，
    卡太死会把真名挡在门外（漏脱是合规红线）。
    """
    if cfg is None:
        cfg = _load_config()
    pool = sorted(SURNAME_SET)
    threshold = cfg.get("name_surname_min_freq", _NAME_GATE_SURNAME_MIN_FREQ)
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        threshold = _NAME_GATE_SURNAME_MIN_FREQ
    if threshold <= 0:
        return pool
    freq = _get_surname_char_freq(cfg)
    if not freq:
        return pool
    gated = [s for s in pool if freq.get(s, 0) >= threshold]
    return gated or pool


def _get_name_rule_mode(cfg: Optional[Dict] = None) -> str:
    """读取姓名规则模式（M2 开关）。

    取值：
      - `graded`（Issue #49 P1 默认）：姓氏分档 + 名字位字频闸门 + 上下文锚点
      - `strict`：退回 #49 整改前的单档规则A（姓 + 名字字{1,3}），供应急回退
      - `r4`：仅启用 #17 R4 评分层（保留给社区后续裁决，行为等同 strict + R4）

    与 `r4_name_scoring.get_name_mode()`（取值 enum/r4/both）是**两个不同**的开关：
    前者控制「规则A 是否分级」，后者控制「R4 评分层是否参与」。二者正交。
    """
    if cfg is None:
        cfg = _load_config()
    mode = cfg.get("name_rule_mode", "graded")
    if mode not in ("graded", "strict", "r4"):
        mode = "graded"
    return mode

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
    "成立",  # R-⑲（v1.3.8，响应 Issue #14-附带发现）：成(姓)+立(名池)误判，如"自XX银行成立"
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
    # ---- R-③（v1.3.2，响应 Issue #6）：基线语料实测误伤词 ----
    # 章/练/和 等姓氏字与常用词撞字：命中片段被下列词覆盖时保留原文
    "章程", "演练方案", "和业务", "交易一部", "时业务",
    # ---- R-⑩（v1.3.3，响应 Issue #10-B）：尖刀语料实测误伤残余 ----
    "黎明", "时不用", "部支一七", "部提七", "金进行",
    "准备阶段", "调研阶段", "设计阶段", "部署阶段", "上线阶段",
    "启动阶段", "执行阶段", "验收阶段", "试运行阶段", "收尾阶段",
    # ---- R-⑭（v1.3.7，响应 Issue #13-②）：字符池系统性补全后实测误伤词 ----
    # 补 安 入名字池后"临时安排"被 时(姓氏)+安(名) 命中；补 名单之内的 同理
    "临时安排", "名单之内", "名单之外",
    # ---- E3（赵辉，第3批，2026-10-08）：角色词后常见动词（防姓名误判）----
    "掌握", "熟悉", "负责", "协助", "配合", "推进", "参与", "管理",
    "协调", "组织", "主导", "审核", "审批", "执行", "跟踪", "监控",
    "评估", "优化", "改进", "完善", "保障", "维护", "支持", "提供",
    # ---- H2（赵辉，第3批，2026-10-08）：姓氏同音词守卫（防"后方/后启/后续"误判为"后+名"）----
    "后方", "后启", "后续", "后勤", "后端", "后背", "后背", "后山",
    "后方", "后续", "后勤", "后端", "后背", "后山", "后裔", "后辈",
    "后方", "后启", "后续", "后勤", "后端", "后背", "后山", "后裔", "后辈",
    # ---- D3（赵辉，第3批，2026-10-08）：行业/技术/办公语义词守卫 ----
    # 姓氏字（金/安/公/通/文/包/健/宏/海/宁）在行业文档中高频出现，须整词保护
    "金融", "安全", "公共", "通用", "文件", "包含", "健康", "宏大", "海洋", "安宁",
    "安全运维", "安全层", "安全部", "安全员", "公务卡", "通用记账", "记账",
    "应用层", "功能", "界面", "产品", "业务", "错误", "运维", "运维部",
    "接口", "文件", "金额", "总金额", "余额", "本金", "利息", "手续费",
    "行方", "甲方", "乙方", "双方", "各方", "责任方", "承建方", "建设方",
    "业主方", "相关方", "公司方", "厂商方", "业务方", "实施方", "需求方",
    "提供方", "服务方", "设计方", "测试方", "运维方", "评审方", "验收方",
    # D8/F1（赵辉，第3批）：含名字池字符的角色词保护（防责/宏/海等字误捕角色词）
    "负责", "主任", "副主任", "经理", "副经理", "总监", "副总监",
    "组长", "副组长", "管理员", "操作员",
    # ---- D17（赵辉，第6批，2026-10-09）：语义词守卫扩充（假阳性收敛）----
    # 测试域（首字落百家姓：全/白/灰/黑）
    "全功能", "全界面", "全白盒", "白盒测试", "黑盒测试", "灰盒测试",
    # 通用业务域
    "全产品", "全业务", "全错误",
    # ---- H2（赵辉，第6批，2026-10-09）：姓氏"后"的动宾短语守卫（追加）----
    "后即", "后由", "后再", "后进",
    # ---- E3（赵辉，第6批，2026-10-09）：角色槽位后的动词/程度词守卫（追加）----
    "按时", "认真", "严格", "准确", "清晰", "完整", "合理", "有效",
    "积极", "持续", "推动", "安排", "指定", "确认", "同意", "汇报",
    # ---- G1（赵辉，第6批，2026-10-09）：人名段尾字豁免表须含「账」
    # 真实姓名不以「账」结尾；全8源扫描所有"XX账"词段均为业务词（总账/对账/转账/…）
    "总账", "对账", "转账", "平账", "抹账", "台账", "内部账", "明细账",
    "外部账", "往来账", "备抵账", "调整账", "结账", "记账",
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
    "省", "市", "县", "区", "镇", "乡", "村", "街", "路", "巷", "弄",
    "支行", "分行", "营业部", "分理处", "号",
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
# "湖东街道恒力大厦"在"道"处切分）。例外：支行/分行 后缀按
# _ADDRESS_COLLECTABLE_FOR_CROSSABLE 规则将 乡村街路巷弄道 收集为地名成分
_ADDRESS_ADMIN_BOUNDARY = set("省市县区镇乡村街路巷弄道")
# 支行/分行 允许跨越一个行政后缀（如"中牟县支行"的"县"），跨过后需再收集到地名前缀
# R-⑯（v1.3.7，Issue #13-④）：营业部/分理处 同属网点名后缀，一并纳入
_ADDRESS_CROSSABLE_SUFFIXES = {"支行", "分行", "营业部", "分理处"}
# R-B 修复（Issue #3/#4，2026-09-12）：支行/分行 回溯时，乡/村/街/路/巷/弄/道
# 是地名组成部分而非硬边界（"新乡分行""金水路支行"此前因'乡'/'路'被误判为
# 行政边界而整体漏检），对可跨越后缀改为正常收集。
_ADDRESS_ADMIN_BOUNDARY = set("省市县区镇")
_ADDRESS_COLLECTABLE_FOR_CROSSABLE = set("乡村街路巷弄道")
# R-B（Issue #4）：前缀词守卫——"项目/产品/工程"等被误收为地名前缀导致
# "大集中项目分行→大集中XX分行"误伤，前缀含下列词时保留原文。
# 注意不含"建设/开发/规划"等常见路名词（建设路/开发路是真实路名）
_ADDRESS_PREFIX_GUARDS = {
    "项目", "产品", "工程", "课题", "方案", "试点", "专项", "示范",
    "改造", "投产", "上线", "推广", "验收",
}
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

# ---------------------------------------------------------------------------
# R-⑨（v1.3.3，响应 Issue #10-B）：二字姓名前向双字组合守卫
#
# 背景：尖刀语料中"交易明细→交XXX细""资金周转→资XXX周"类误伤，根因是
# 姓氏池字（易/金/费/史/支/单…）与前一字构成常用双字组合（交易/资金/消费/
# 历史/收支/存单…）。命中片段被排除词逐个收录属打地鼠，本守卫在规则应用层
# 统一判断：二字姓名命中若"首字与前一字"构成常用组合则保留原文。
# 仅作用于 2 字命中，3 字姓名（如"张金进"）不受影响，避免"王进+行"类误保护。
# ---------------------------------------------------------------------------

_COMMON_BIGRAMS: set = {
    "交易", "资金", "消费", "历史", "收支", "存单", "记账", "转存",
    "部支", "业务", "会计", "结算", "清算", "汇兑", "票证", "凭证",
}


# ---------------------------------------------------------------------------
# R-㊷（Issue #49 收尾，2026-10-26）：业务词保护守卫（跨词边界误伤收敛）
#
# 【问题】尖刀语料实测的 17 处剩余误伤，除「名单块误判」外，绝大多数是
#   **跨词边界切词误伤**：姓名规则以「姓氏+名字池字」形态切出的片段，
#   实际横跨了两个业务词的边界（词的尾字 + 下一词的首字）：
#     「保证金子户」→ 切出「金子」   （保证金|子户）
#     「保函赔付金额」→ 切出「付金」 （赔付|金额）
#     「金额不能大于可转出」→ 切出「于可」（大于|可转出）
#     「逾期剩余本金」→ 切出「余本金」（剩余|本金）
#     「白名单维护」→ 切出「单维」 （名单|维护）
#     「核对池业务台账」→ 切出「池业」（核对池|业务）
#
# 【解法】业务词保护表（config.json 的 biz_guard_words）+ 三类守卫：
#   G1 跨界守卫：命中片段 [s,e) 若与某业务词的出现区间 [ws,we) 交叉
#       （即该业务词的尾字落在命中内、或命中片段首字落在该业务词内部），
#       则说明这是「切开了一个业务词」而非识别出一个人名 → 保留原文。
#   G2 整体命中守卫：命中片段本身即为某业务词（全部/单位/名单…）→ 保留原文。
#   G3 指示词守卫：命中片段由指示词/代词构成（上述所有/以下/相关…）→ 保留原文。
#
# 【词表来源】真实语料（119 份文档 + 通用语料 102 条）挖掘的高频 2~4 字业务词，
#   并**双向剔除**与 D16 全名库（7398 条）、824 条金标真名有包含关系的词——
#   即保护表内不可能含任何真名，故本守卫不会造成真名漏脱。
# ---------------------------------------------------------------------------

_BIZ_GUARD_WORDS: Optional[set] = None
_BIZ_GUARD_MAXLEN: int = 0
_BIZ_WORDS_BY_FIRST: Optional[Dict[str, set]] = None


def _get_biz_guard_words() -> set:
    """取业务词保护表（config.json → biz_guard_words），带缓存。"""
    global _BIZ_GUARD_WORDS, _BIZ_GUARD_MAXLEN
    if _BIZ_GUARD_WORDS is None:
        cfg = _load_config()
        words = {w for w in (cfg.get("biz_guard_words") or []) if isinstance(w, str) and w}
        _BIZ_GUARD_WORDS = words
        _BIZ_GUARD_MAXLEN = max((len(w) for w in words), default=0)
    return _BIZ_GUARD_WORDS


# 指示词/代词：本身绝不可能是人名，被切出来即为误伤
_BIZ_INDICATOR_WORDS: set = {
    "上述", "以下", "如下", "前述", "相关", "其他", "其它", "所有",
    "全部", "各个", "各项", "各类", "本行", "该行", "本行",
}


def _find_biz_word_spans(text: str, window: Optional[Tuple[int, int]] = None) -> List[Tuple[int, int]]:
    """在文本中找出所有业务词保护词的出现区间 [(start, end), ...]。

    【性能】按首字建倒排索引，单次扫描 O(len(seg) × 该首字下的词数)，
    避免对每个候选位置全量遍历 135+ 个词。
    【窗口】window=(lo, hi) 表示只在 text[lo:hi] 内查找，返回绝对坐标。
    """
    words = _get_biz_guard_words()
    if not words or not text:
        return []
    lo, hi = window if window else (0, len(text))
    seg = text[lo:hi]
    if not seg:
        return []
    spans: List[Tuple[int, int]] = []
    by_first = _get_biz_words_by_first()
    for i, ch in enumerate(seg):
        cands = by_first.get(ch)
        if not cands:
            continue
        for w in cands:
            if seg.startswith(w, i):
                spans.append((lo + i, lo + i + len(w)))
    return spans


def _get_biz_words_by_first() -> Dict[str, set]:
    """业务词按首字分组的倒排索引（带缓存）。"""
    global _BIZ_WORDS_BY_FIRST
    if _BIZ_WORDS_BY_FIRST is None:
        by_first: Dict[str, set] = {}
        for w in _get_biz_guard_words():
            by_first.setdefault(w[0], set()).add(w)
        _BIZ_WORDS_BY_FIRST = by_first
    return _BIZ_WORDS_BY_FIRST


def _is_protected_by_biz_word(text: str, start: int, end: int,
                              biz_spans: Optional[List[Tuple[int, int]]] = None) -> bool:
    """判断姓名命中片段 text[start:end] 是否属于业务词误伤。

    G1 跨界守卫：命中片段与某业务词区间交叉（词的尾字在命中内，
       或命中首字在词内部）→ 说明切词切到了业务词上。
    G2 整体命中：命中片段本身就是业务词。
    G3 指示词：命中片段由指示词/代词构成。
    """
    matched = text[start:end]
    if not matched:
        return False

    # G3 指示词守卫：命中片段内含指示词且整体不是姓名形态
    # （"上述所有" 含 "上述"+"所有"，但不含姓氏位）
    if any(ind in matched for ind in _BIZ_INDICATOR_WORDS):
        if matched not in _get_biz_guard_words():
            # 片段整体也可能是真名（极少），仅当不含姓氏字时才判定为指示词
            if not (matched[0] in SURNAME_SET and len(matched) <= 3):
                return True

    words = _get_biz_guard_words()
    if not words:
        return False

    # G2 整体命中守卫：命中片段本身就是业务词
    if matched in words:
        return True

    # G1 跨界守卫：命中片段与某业务词区间交叉
    # 【窗口】跨界只可能发生在命中边界附近，业务词最长 4 字，
    #   故左右各取 6 字足以覆盖跨界判定，避免全文本扫描。
    if biz_spans is not None:
        spans = biz_spans
    else:
        lo = max(0, start - 6)
        hi = min(len(text), end + 6)
        spans = _find_biz_word_spans(text, window=(lo, hi))
    for ws, we in spans:
        # 命中片段与业务词区间相交（含被完全包含）→ 切词误伤
        if ws < end and start < we:
            return True
    return False


def _is_protected_by_common_word(text: str, start: int, end: int) -> bool:
    """判断 text[start:end] 命中片段是否被某个排除常用词完整覆盖。"""
    matched = text[start:end]
    if matched in EXCLUDED_COMMON_WORDS:
        return True
    # R-⑨：二字姓名前向双字组合守卫（"交易"易明/"资金"金周 等）
    if (len(matched) == 2 and start > 0
            and text[start - 1] + matched[0] in _COMMON_BIGRAMS):
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
        # R-㊷：业务词保护守卫（G1 跨界 / G2 整体 / G3 指示词）
        if _is_protected_by_biz_word(m.string, m.start(), m.end()):
            return m.group(0)
        return replacement
    return pattern.sub(_repl, text)


def _apply_address_pass(text: str, stats: Dict[str, int] = None) -> str:
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

        # R-⑯（v1.3.7，Issue #13-④）：单字行政后缀后紧跟网点后缀时跳过该行政后缀
        # （"锦艺新时代社区支行"的"区"先于"支行"被匹配，会把"锦艺新时代社"吞并成
        # "锦XX区"，使"支行"回溯时前缀起点落入已替换区间而被整体跳过）。交由网点
        # 后缀统一处理，可跨后缀逻辑即能正确收集"社区"。
        if (len(suf) == 1 and suf in "省市县区镇乡村街路巷弄"
                and any(text.startswith(bs, s_end)
                        for bs in _ADDRESS_CROSSABLE_SUFFIXES)):
            continue

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
                    if stats is not None:
                        stats["地址"] = stats.get("地址", 0) + 1
            continue

        # 向左回溯收集地名前缀
        # R-⑯（v1.3.7，Issue #13-④）：可跨后缀（支行/分行/营业部/分理处）的网点名
        # 上限由 5 放宽至 8（"锦艺新时代社区支行"此前因收满 5 字停在"艺"→
        # "锦XX区支行"部分遮盖）；其余后缀维持 5，避免长词误收
        crossable = suf in _ADDRESS_CROSSABLE_SUFFIXES
        max_collect = 8 if crossable else 5
        i = s_start
        collected: List[str] = []
        stopped_by_demo = False
        crossed_admin = ""  # 支行/分行 跨越的行政后缀（如"中牟县支行"的"县"）
        stop_backfilled = 0  # 停用字后补计数（每处最多 1 字）
        while i - 1 >= 0 and len(collected) < max_collect:
            ch = text[i - 1]
            if not ("\u4e00" <= ch <= "\u9fff"):
                break
            if ch in _ADDRESS_DEMO_CHARS:
                stopped_by_demo = True
                break
            if ch in _ADDRESS_STOP_CHARS:
                # R-⑯：网点名场景下若已收地名不足 2 字，允许把停用字并入前缀
                # （"会展支行"的"会"在停用字表，但它是"会展"地名的首字）。
                # 仅对 支行/分行/营业部/分理处 生效，且每处最多补 1 字
                if crossable and len(collected) == 1 and stop_backfilled < 1:
                    collected.append(ch)
                    i -= 1
                    stop_backfilled += 1
                    continue
                break
            if ch in _ADDRESS_ADMIN_BOUNDARY:
                # R-⑯："社区支行"——"区"前为"社"时，"社区"是网点类型限定词
                # （锦艺新时代社区支行），作为地名成分并入遮盖段，而非跨行政后缀
                if crossable and ch == "区" and i - 2 >= 0 and text[i - 2] == "社":
                    collected.append(ch)
                    i -= 1
                    continue
                # R-B（Issue #3 漏1）：支行/分行 回溯时 乡村街路巷弄道 是地名成分
                # （"新乡分行""金水路支行"），正常收集而非边界
                if crossable and ch in _ADDRESS_COLLECTABLE_FOR_CROSSABLE:
                    collected.append(ch)
                    i -= 1
                    continue
                # 支行/分行 允许跨越一个行政后缀（县支行/市分行），其余作为边界
                if crossable and not crossed_admin and ch in ("省", "市", "县", "区"):
                    crossed_admin = ch
                    i -= 1
                    continue
                break
            collected.append(ch)
            i -= 1
        prefix = "".join(reversed(collected))

        # R-B 补充（存量缺口）：跨行政后缀场景下前缀仅1字且停在"中"时，
        # "中"是县名首字（中牟/中卫/中山…）而非"其中"虚词，并入前缀。
        # "中牟县支行"此前漏检即源于此；"其中+县支行"组合实际不存在，无误伤面
        if (crossed_admin and len(prefix) == 1 and i > 0
                and text[i - 1] == "中" and "\u4e00" <= text[i - 1] <= "\u9fff"):
            i -= 1
            prefix = "中" + prefix

        # R-B（Issue #4）保护：前缀含项目/产品/工程等非地名词时保留原文
        if any(g in prefix for g in _ADDRESS_PREFIX_GUARDS):
            continue

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
        if stats is not None:
            stats["地址"] = stats.get("地址", 0) + 1

    if not replaced:
        return text
    out.append(text[last:])
    return "".join(out)


# ---------------------------------------------------------------------------
# R-⑮（v1.3.7，响应 Issue #13-③）：无区号 7–8 位本地号码脱敏
#
# 背景：规则层对"电话：87517381"这类无区号本地号码零覆盖（G2 漏脱敏）。
# 裸 \d{7,8} 会吞工号/编号/日期(20110612)/金额，故必须"上下文限定"：
#   命中数字（含可选区号）的前置窗口内必须出现电话类关键词，
#   且不得出现工号/编号/账号/金额等排除类关键词。
# 前置窗口短（12 字），避免跨句误判；幂等（占位符无数字，不二次命中）。
# ---------------------------------------------------------------------------

_LOCAL_PHONE_CONTEXT = (
    "电话", "联系方式", "联系电话", "座机", "固话", "办公电话",
    "分机", "内线", "传真", "号码", "手机", "Tel", "tel", "TEL",
)
_LOCAL_PHONE_EXCLUDE = (
    "工号", "编号", "账号", "卡号", "订单", "金额", "合同", "流水", "批次",
    "序号", "学号", "证号", "版本", "年度", "年第", "日期", "期间", "身份证",
    "邮编", "房号", "楼号", "室号", "车牌", "发票", "卡号",
)
# 可选区号（0 + 2-3 位 + 可选分隔符）+ 7-8 位本地号码；数字边界防子串误伤
_LOCAL_PHONE_RE = re.compile(r"(?<!\d)(?:0\d{2,3}[-\s]?)?(\d{7,8})(?!\d)")
_LOCAL_PHONE_WINDOW = 12


def _apply_local_phone_pass(text: str, counts: Dict[str, int] = None) -> str:
    """上下文限定的无区号本地号码脱敏（R-⑮）。"""
    if not text or not isinstance(text, str):
        return text
    if not _LOCAL_PHONE_RE.search(text):
        return text
    replacement = get_replacement("PHONE", "0XX-XXXXXXXX")

    def _repl(m: re.Match) -> str:
        pre = text[max(0, m.start() - _LOCAL_PHONE_WINDOW):m.start()]
        digits = m.group(1)
        # 排除日期形态：8 位且形如 YYYYMMDD（20xx…）
        if len(digits) == 8 and digits[:2] == "20" and "01" <= digits[4:6] <= "12":
            return m.group(0)
        # 前置窗口必须含电话关键词（否则视为工号/编号/金额等）
        if not any(k in pre for k in _LOCAL_PHONE_CONTEXT):
            return m.group(0)
        # 前置窗口含排除词则跳过（如"工号：12345678""金额 12345678"）
        if any(k in pre for k in _LOCAL_PHONE_EXCLUDE):
            return m.group(0)
        # 数字后紧邻"号"（门牌/房间号）→ 跳过
        if m.end() < len(text) and text[m.end()] == "号":
            return m.group(0)
        if counts is not None:
            counts["固话"] = counts.get("固话", 0) + 1
        return replacement

    return _LOCAL_PHONE_RE.sub(_repl, text)


# ---------------------------------------------------------------------------
# 正则规则构建（从 config.json 动态加载，支持扩展）
# ---------------------------------------------------------------------------

def _build_patterns() -> List[Tuple[re.Pattern, str]]:
    global _PATTERNS_PRE, _PATTERNS_NAME
    cfg = _load_config()
    rep = cfg.get("replacement", {})
    bank_names: List[str] = cfg.get("bank_names", [])
    org_suffixes: List[str] = cfg.get("org_suffixes", [])
    # R-㉛（v1.3.9，响应 Issue #15-C12，赵辉）：日期脱敏口径
    # full（默认）：全掩码 YYYY年MM月DD日
    # year-only：仅脱年份，保留月日数字（如 2022年08月08日 → YYYY年08月08日）
    date_mode = cfg.get("date_mode", "full")

    patterns: List[Tuple[re.Pattern, str]] = []

    # ---------- 1. 邮箱 ----------
    patterns.append((
        re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
        rep.get("EMAIL", "XXXXX@XXXXX")
    ))

    # E6（赵辉，第3批，2026-10-08）：共享文档/网盘/内网 IP 链接
    # 场景：https://www.kdocs.cn/l/cmyr4yqzm5eP → www.kdocs.cn/XXXXXX（正文/表格）
    #       http://10.42.20.182/xxx           → 全串替换为 XXX（内网 IP 禁止外泄）
    _URL_SHARED_DOC_RE = re.compile(
        r'https?://(?:(?:www|m)\.)?(?:'
        r'kdocs\.cn|wps\.cn|docs\.qq\.com|shimo\.im|yuque\.com|'
        r'pan\.baidu\.com|alipan\.com|wecom\.qq\.com|work\.weixin\.qq\.com'
        r')[/\\][^\s\'"<>）】»]*',
        re.I
    )
    _URL_INTRA_IP_RE = re.compile(
        r'https?://(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|'
        r'172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|'
        r'192\.168\.\d{1,3}\.\d{1,3}|'
        r'127\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?:/[^\\s\'"<>）】»]*)?',
        re.I
    )
    def _url_doc_repl(m):
        # 保留域名，脱敏路径段
        import urllib.parse
        url = m.group(0)
        try:
            parsed = urllib.parse.urlparse(url)
            safe = f"{parsed.scheme}://{parsed.netloc}/XXXXXX"
            return safe
        except Exception:
            return "XXXXXXX"
    patterns.append((_URL_SHARED_DOC_RE, _url_doc_repl))
    patterns.append((_URL_INTRA_IP_RE, lambda m: "XXX"))  # 内网 IP 全串脱敏

    # ---------- 2. IP 地址 ----------
    patterns.append((
        # R-F（Issue #5 同类排查）：\b 在"汉字+数字"直连处不成立（汉字与数字均为\w），
        # "手机13812345678"此前完全漏脱敏；改为数字感知边界
        re.compile(r"(?<![0-9A-Za-z_])(?:\d{1,3}\.){3}\d{1,3}(?![0-9A-Za-z_])"),
        rep.get("IP", "X.X.X.X")
    ))

    # ---------- 3. MAC 地址 ----------
    patterns.append((
        re.compile(r"(?<![0-9A-Za-z])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Za-z])"),
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
    # R-F：\b → 数字感知边界（汉字直连身份证号此前永不命中）
    patterns.append((
        re.compile(
            r"(?<![0-9Xx])([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
            r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9Xx])"
        ),
        rep.get("ID_CARD", "XXXXXXXXXXXXXXXXXX")
    ))
    # 15位身份证
    patterns.append((
        re.compile(r"(?<!\d)([1-9]\d{5}\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3})(?!\d)"),
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
        # R-F：\b → 数字感知边界（"手机13812345678"直连此前完全漏脱敏）
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        rep.get("MOBILE", "XXXXXXXXXXX")
    ))

    # ---------- 8. 固定电话（R-D 全遮方案，2026-09-12 社区决策） ----------
    # 策略：区号与号码全部遮盖（0XX-XXXXXXXX），不保留后8位。
    # 依据：外发文档存在通过号码反查个体的合规风险（Issue #3 漏2 评估决策项）。
    # 注：历史等长替换约束已随段落级回写（R-A）取消，文本内容长度变化不影响 XML 结构
    patterns.append((
        re.compile(r"0\d{2,3}-\d{7,8}"),
        rep.get("PHONE", "0XX-XXXXXXXX")
    ))
    # R-F（Issue #5）：无分隔紧凑固话（如"方培培037185519208"，姓名与号码
    # 无分隔符时区号-号码连写）→ 全遮。10-12位且以0开头（0+区号2-3位+号码7-8位），
    # 前后边界阻止匹配长数字串（身份证/账号）的片段
    patterns.append((
        re.compile(r"(?<!\d)0\d{9,11}(?!\d)"),
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

    # R-㉛（v1.3.9，响应 Issue #15-C12，赵辉）：日期口径配置化
    # full（默认）：全掩码 YYYY年MM月DD日
    # year-only：仅脱年份，保留月日数字（如 2022年08月08日 → YYYY年08月08日）
    # 正则分组：独立日期规则 grp1=年, grp2=月, grp3=日
    #           日期范围规则 grp1=完整日期1, grp2=年1, grp3=月1, grp4=日1,
    #                         grp5=分隔符, grp6=完整日期2, grp7=年2, grp8=月2, grp9=日2
    _is_year_only = (date_mode == "year-only")

    if _is_year_only:
        # year-only 模式：仅替换年份，保留月日数字。
        # 独立规则分组：grp1=年, grp2=月+月字, grp3=日+日字（lookahead 不消费字符）
        # 日期范围分组：grp1=年, grp2=月+月字, grp3=日+日字, grp4=分隔符,
        #               grp5=年, grp6=月+月字, grp7=日+日字
        _date_chinese_full = "YYYY年MM月DD日"  # 仅供中文日期范围 full 端占位
        _date_slash_full   = "YYYY/MM/DD"
        _date_dash_full    = "YYYY-MM-DD"
        _date_chinese_month = "YYYY年MM月"
        _date_ar_month      = "YYYY/MM"
        _date_chinese_short = "MM月DD日"
        _date_short_slash   = "MM/DD"
        # 中文独立日期 year-only lambda
        # 独立中文日期 pattern 结构：(grp1=完整日期|grp2=完整日期)
        # year-only：从 m.group(0) 的"年"字（含）起取月日部分
        _date_chinese_repl = lambda m: (
            "YYYY" + m.group(0)[m.group(0).find("年"):]
        )
        _date_slash_repl   = lambda m: "YYYY/" + m.group(1) + "/" + m.group(2)
        _date_dash_repl    = lambda m: "YYYY-" + m.group(1) + "-" + m.group(2)
        _date_ar_month_repl = lambda m: "YYYY/" + m.group(2)
        # 中文日期范围：range pattern 分组 grp1=完整日期1, grp2=分隔符, grp3=完整日期2
        # m.group(1)[m.group(1).find("年"):] → 从"年"字(含)起取月日
        _range_chinese_repl = (
            lambda m: ("YYYY" + m.group(1)[m.group(1).find("年"):]
                       + m.group(2)
                       + "YYYY" + m.group(3)[m.group(3).find("年"):])
        )
        # slash 范围分组：grp1=完整1, grp2=年1, grp3=月1, grp4=日1, grp5=分隔符, grp6=完整2, grp7=年2, grp8=月2, grp9=日2
        _range_slash_repl = lambda m: ("YYYY/" + m.group(3) + "/" + m.group(4)
                                        + m.group(5)
                                        + "YYYY/" + m.group(8) + "/" + m.group(9))
        _range_dash_repl  = lambda m: ("YYYY-" + m.group(3) + "-" + m.group(4)
                                        + m.group(5)
                                        + "YYYY-" + m.group(8) + "-" + m.group(9))
    else:  # full
        _date_chinese_full = rep.get("DATE_CHINESE", "YYYY年MM月DD日")
        _date_slash_full   = rep.get("DATE", "YYYY/MM/DD")
        _date_dash_full    = rep.get("DATE", "YYYY/MM/DD")
        _date_chinese_month = rep.get("DATE_CHINESE_MONTH", "YYYY年MM月")
        _date_ar_month      = rep.get("DATE", "YYYY/MM")
        _date_chinese_short = rep.get("DATE_CHINESE_SHORT", "MM月DD日")
        _date_short_slash   = rep.get("DATE_SHORT", "MM/DD")
        _date_chinese_repl = _date_chinese_full
        _date_slash_repl   = _date_slash_full
        _date_dash_repl    = _date_dash_full
        _date_ar_month_repl = _date_ar_month
        _range_chinese_repl = _date_chinese_full + r"\2" + _date_chinese_full
        _range_slash_repl = _date_slash_full + r"\5" + _date_slash_full
        _range_dash_repl  = _date_dash_full  + r"\5" + _date_dash_full

    # 中文日期范围：2022年4月8日至2022年4月10日
    # 分组：grp1=年+月+日, grp2=分隔符, grp3=年+月+日
    patterns.append((
        re.compile(
            rf'({year4_cn}年{month_pat}{day_pat}日)'
            r'([至——][——]?)'
            rf'({year4_cn}年{month_pat}{day_pat}日)'
        ),
        _range_chinese_repl
    ))
    # 斜杠日期范围：2022/04/08 至 2022/04/10
    # 分组：grp1=完整日期1, grp2=年1, grp3=月1, grp4=日1, grp5=分隔符,
    #        grp6=完整日期2, grp7=年2, grp8=月2, grp9=日2
    patterns.append((
        re.compile(
            rf'(({year4_ar})/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01]))'
            r'(\s*(?:至|——|[-~])\s*)'
            rf'(({year4_ar})/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01]))(?![0-9])'
        ),
        _range_slash_repl
    ))
    # 横杠日期范围：2022-04-08 ~ 2022-04-10
    patterns.append((
        re.compile(
            rf'(({year4_ar})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01]))'
            r'(\s*(?:至|——|[-~])\s*)'
            rf'(({year4_ar})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01]))(?![0-9])'
        ),
        _range_dash_repl
    ))

    # ---------- 10. 日期（独立） ----------
    # 中文数字日期（但排除"应为2022年3月31日"等场景中的日期）
    # 前置排除用后处理替代，避免变长 lookbehind 语法问题
    # year-only 模式使用 year-aware 替换，保留月日
    # 注意：year4_cn/month_pat/day_pat 是非捕获组变量；此处显式构建含捕获组的正则
    # 分组：grp1=年, grp2=月+月字, grp3=日+日字
    # year-only 模式：独立中文日期
    # 独立中文日期：两分支 pattern 用 | 并列，都以捕获组 () 包裹完整日期
    # full 模式用 _date_chinese_full（含 \1 引用），year-only 用 lambda
    # 分支1：阿拉伯数字年/月/日；分支2：汉字数字年/月/日
    # 注意：阿拉伯分支月不含(?=月) lookahead（"4月"直接匹配），汉字分支含 lookahead（"三"后需"月"）
    # 独立中文日期：两分支 pattern，都以捕获组 () 包裹完整日期供 full 模式 \1 引用
    # full 模式用 _date_chinese_full（含 \1），year-only 用 lambda m.group(0)
    # 分支1：阿拉伯数字年/月/日；分支2：汉字数字年/月/日（需 lookahead 判断月字）
    _date_cn_pat = (
        rf'([〇二三四五六七八九0-9]{{4}}年(?:0?[1-9]|1[0-2])月[0-9]+日'
        r'|[〇二三四五六七八九0-9]{4}年'
        rf'(?:0?[1-9]|1[0-2]|'
        r'(?=[一二三四五六七八九十]月)[一二三四五六七八九十]|'
        r'十一(?=月)|十二(?=月)|正(?=月))月'
        r'(?:[0-9]+?(?=日)|[一二三四五六七八九十]+(?=日)日))'
    )
    patterns.append((re.compile(_date_cn_pat), _date_chinese_repl))
    # YYYY/MM/DD
    # R2 修复：$ → (?![0-9])，正文中的日期（如"2020/5/1实施"）此前永不命中
    patterns.append((
        re.compile(rf'{year4_ar}/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?![0-9])'),
        _date_slash_repl if _is_year_only else _date_slash_full
    ))
    # YYYY-MM-DD
    patterns.append((
        re.compile(rf'{year4_ar}-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])(?![0-9])'),
        _date_dash_repl if _is_year_only else _date_dash_full
    ))
    # 中文年月（独立）
    # R2 修复：显式吞掉"月"字，避免阿拉伯年月规则先吃掉"2020年5"残留"月"字
    # 占位符用独立的年月键，避免取到含 DD日 的完整日期占位
    patterns.append((
        re.compile(rf'{year4_cn}年{month_pat}月(?![0-9日])'),
        _date_chinese_month
    ))
    # 阿拉伯数字年月（后跟"月"的场景由上一条处理）
    patterns.append((
        re.compile(rf'{year4_ar}年(0?[1-9]|1[0-2])(?!月|[0-9日])'),
        _date_ar_month_repl if _is_year_only else _date_ar_month
    ))
    # R2 新增：无年份中文短日期"X月X日"（如"5月20日发布"）
    # 左边界阻止年份残留（YYYY年X月X日 已由上方完整规则先替换）与数字
    patterns.append((
        re.compile(r'(?<![0-9年])(?:0?[1-9]|1[0-2])月(?:[0-3]?[0-9])日(?![0-9])'),
        _date_chinese_short
    ))
    # R2 新增：无年份斜杠短日期"M/D"（如"截止9/1上报"）
    # 边界阻止长数字串/小数/日期已替换场景
    patterns.append((
        re.compile(r'(?<![0-9/.])(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12][0-9]|3[01])(?![0-9/])'),
        _date_short_slash
    ))
    # D14（赵辉，第3批，2026-10-08）：裸年份独立形态（YYYY年 单独出现）
    # 必须在完整日期规则之后执行；否则"2025年7月5日"先被吃掉成"YYYY年7月5日"
    # 独立年形态：20xx/年，前后非数字（非日期片段），后不跟月/日/号
    patterns.append((
        re.compile(r'(?<![0-9])(20[12]\d)年(?![0-9月日号])'),
        rep.get("DATE_YEAR_ONLY", "YYYY年")
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
        # R-㉓（v1.3.9，响应 Issue #15-C4，赵辉）：增加五层误伤防护
        #   1. 泛称表 BANK_GENERIC_KEEP（如"人民银行""开户银行"等30+不脱）
        #   2. 集合表述 BANK_COLLECTIVE_RE（"全省农商银行"等不脱）
        #   3. 业务系统/业态词 BANK_BIZ_SYSTEM_RE（"微信银行""远程视频银行"等不脱）
        #   4. "的银行"口号守卫（"一农民致富的银行"不脱）
        #   5. 占位符自保护 X{1,10}银行（防字母规则二次击中）

        # D4（赵辉，第3批，2026-10-08）：品牌词内嵌——行品牌词（云南红塔/富滇等）
        # 品牌词不完整含"银行"，须独立处理。保留地名前两字（红塔→红XX）
        _BANK_BRAND_RE = re.compile(
            r'(?:(?:(?:云南)?(?:红塔|富滇)|(?:广西)?(?:北部湾|桂林|柳州)|'
            r'(?:重庆)?(?:三峡|富民)|(?:四川)?(?:天府|攀枝花)|(?:贵州)?(?:茅台|'
            r'贵阳)|(?:浙江)?(?:泰隆|民泰|稠州)|(?:江苏)?(?:江南|紫金|苏州|'
            r'长江)|(?:安徽)?(?:徽商|新安)|(?:山东)?(?:齐商|青岛)|(?:河南)?(?:'
            r'郑州|中原)|(?:河北)?(?:河北|沧州)|(?:山西)?(?:晋商|晋城)|(?:'
            r'内蒙古)?(?:内蒙古)|(?:辽宁)?(?:盛京|大连)|(?:吉林)?(?:吉林|'
            r'长春)|(?:黑龙江)?(?:黑龙江|哈尔滨)|(?:福建)?(?:福建|海峡|'
            r'厦门)|(?:江西)?(?:江西|赣州)|(?:湖北)?(?:湖北|汉口)|(?:湖南)?(?:'
            r'湖南|长沙)|(?:广东)?(?:广东|广州|深圳|东莞|珠海)|(?:海南)?(?:'
            r'海南|海口)|(?:贵州)?(?:贵州)|(?:云南)?(?:云南)))(?!银行)'
        )
        def _bank_brand_repl(m):
            brand = m.group(0)
            return brand[:2] + "XX"
        patterns.append((_BANK_BRAND_RE, _bank_brand_repl))
        _BANK_GENERIC_KEEP = {
            "人民银行", "商业银行", "开户银行", "各农商银行", "各农村商业银行",
            "各信用社", "各农村合作银行", "各合作银行", "村镇银行", "各村镇银行",
            "农商银行", "农村商业银行", "农信社", "农村信用合作社", "农村合作银行",
            "各政策性银行", "政策性银行", "国有大型银行", "国有商业银行",
            "股份制银行", "股份制商业银行", "城市商业银行", "城市合作银行",
            "外资银行", "合资银行", "民营银行", "直销银行", "互联网银行",
            "虚拟银行", "手机银行", "网上银行", "电话银行", "视频银行",
            "远程银行", "数字银行", "智慧银行", "云银行", "开放银行",
            # D13（赵辉，第6批，2026-10-09）：业态词保留表扩充——"个人"前缀
            # "个人银行账户报送系统"整体保留；"个人商业银行客户"中"个人商业银行"保留
            "个人银行", "个人商业银行",
        }
        _BANK_COLLECTIVE_RE = re.compile(
            r"(全省|全辖|辖内|各级|各)(农商|农村商业|农信|农合)(银行|行)"
        )
        _BANK_BIZ_SYSTEM_RE = re.compile(
            r"(微信|视频|远程|电话|手机|网上|直销|数字|智慧|云|个人|电子)(?=银行)"
        )
        _BANK_DEPT_SUFFIX_RE = re.compile(
            r"[\u4e00-\u9fa5]{2,6}银行[部处科局]"
        )

        # ---- D10（赵辉，第6批，2026-10-09）：机构泛称收尾守卫
        # "业务条线及分支行" 被整段吞成"XX支行"；前缀以"分/总"收尾视为泛称保留
        # _GENERIC_ORG_PREFIX 放银行通用守卫之前（先泛称保护，再银行脱敏）
        _GENERIC_ORG_PREFIX = {
            "总分", "总分行", "分支", "分行", "总行",
            "各分行", "各支行", "各营业部", "各网点",
            "分支机构", "分支行", "分支行人员",
        }
        _GENERIC_ORG_PREFIX_RE = re.compile(
            r"(?:^|(?<=[，。、；：？！—…～\s()（）【】《》『』「」,，/]))(?:"
            + "|".join(re.escape(p) for p in sorted(_GENERIC_ORG_PREFIX, key=len, reverse=True))
            + r")"
        )

        # ---- D9（赵辉，第6批，2026-10-09）：银行名兜底的「前导词剥离」
        # "稳定性等指标是否满足红塔银行"贪婪吞掉"是否满足红塔银行"整段
        # 前导词：句法功能词（是否/满足/针对/依托/…）在银行名前时先剥离再脱敏
        _BANK_LEAD_WORDS = {
            "是否", "满足", "针对", "依托", "通过", "依据", "根据",
            "鉴于", "按照", "遵照", "参照", "参考", "包括", "涵盖",
            "涉及", "关联", "相关", "配合", "围绕", "推进", "落实",
            "保障", "服务", "支持", "对接", "深化", "强化", "加强",
        }
        _BANK_LEAD_RE = re.compile(
            r"(?:"
            + "|".join(re.escape(w) for w in sorted(_BANK_LEAD_WORDS, key=len, reverse=True))
            + r")(?=[一-龥a-zA-Z])"
        )

        def _bank_generic_guard(text: str) -> bool:
            """判断文本是否命中银行名防护层，是则不脱"""
            if text in _BANK_GENERIC_KEEP:
                return True
            if _BANK_COLLECTIVE_RE.search(text):
                return True
            if _BANK_BIZ_SYSTEM_RE.search(text):
                return True
            if _BANK_DEPT_SUFFIX_RE.fullmatch(text):
                return True
            # D10（赵辉，第6批，2026-10-09）：机构泛称收尾守卫（"总分/分支行"等泛称保留）
            # 检查匹配前1-3字是否与本字组合成泛称前缀（如"总分分行"、"分支行"）
            if _GENERIC_ORG_PREFIX_RE.search(text):
                return True
            # "的银行"口号守卫（前面是助词/逗号/句号等，上游实证农芯末页"一农民致富的银行"）
            if text.endswith("的银行"):
                return True
            # 占位符自保护：X{1,10}银行（防字母银行名规则二次击中）
            if re.fullmatch(r"X{1,10}银行", text):
                return True
            return False

        # D9（赵辉，第6批，2026-10-09）：银行名前导词剥离
        # 贪婪模式 [汉字]{2,6}银行 会吞掉前导句法词（如"满足红塔银行"→吞"满足"）
        # 修复：若匹配长度 > 4，先逐个剥离前导词，只保留真实地名前缀
        #       同时保留前导词，只脱银行名部分（"是否满足红塔银行" → "是否满足XX银行"）
        def _bank_name_repl(m):
            matched = m.group(0)
            # 1. 整体是否受泛称/业态保护
            if _bank_generic_guard(matched):
                return matched
            # 2. D10（赵辉，第6批）：泛称复合词检查——
            #    核心规则："分行"/"支行"被"分支"/"各"/"总"修饰时是泛称，不脱
            #    注意：[汉字]{2,6}银行 贪婪匹配得到：
            #      "各分行银行"（而非"分行银行"）、"条线及分支行银行"（而非"分行银行"）
            #    窗口：取原文本中 match 起点前最多6字 + matched 整体，
            #      在此窗口内找分行/支行，然后检查其前面1-3字是否形成泛称前缀
            #    "各分行银行自行处理"：m.start()=0 → 窗口="各分行银行"；
            #      idx=1(分行)，window[max(0,1-3):1]="各" → "各分行" in _GENERIC_ORG_PREFIX ✓
            #    "业务条线及分支行银行"：m.start()=2 → 窗口="业务条线及分支行银行"；
            #      idx=6(分行)，window[3:6]="及分支" → "分支行"不在
            #      window[1:6]="线及分支" → "分支行"不在
            #      window[0:6]="业务条线及分支" → "分支行"不在
            #      等等，实际上：window="业务条线及分支行银行"（20字），idx=6（分行）
            #      window[max(0,6-3):6]="及分支"，prefix+分行="分行分行" ✗
            #      window[max(0,6-2):6]="分支"，prefix+分行="分支分行" ✗
            #      window[max(0,6-1):6]="支"，prefix+分行="支行" ✗
            #    "总分行业务银行人员"：m.start()=0 → 窗口="总分行业务银行"；
            #      idx=1(分行)，window[max(0,1-2):1]="总" → "总分分行" in _GENERIC_ORG_PREFIX ✓
            window = m.string[max(0, m.start() - 6):m.start()] + matched
            for suffix in ("分行", "支行"):
                idx = matched.rfind(suffix)
                if idx == -1:
                    continue
                win_idx = (m.start() - max(0, m.start() - 6)) + idx  # suffix position in window
                for plen in range(3, 0, -1):
                    pfx_start = max(0, win_idx - plen)
                    pfx = window[pfx_start:win_idx]
                    if pfx + suffix in _GENERIC_ORG_PREFIX:
                        return matched
                break  # 找到一个 suffix 就够
            # 3. D9（赵辉，第6批）：剥离前导词，只脱银行名部分
            #    保存前导词前缀（用于拼接回最终输出）
            lead_prefix = ""
            while True:
                lead = _BANK_LEAD_RE.search(matched)
                if not lead:
                    break
                lead_start = matched.find(lead.group(0))
                if lead_start == 0:
                    # 前导词在开头 → 保存前缀，继续剥离
                    lead_prefix += matched[:len(lead.group(0))]
                    matched = matched[len(lead.group(0)):]
                    if not matched:
                        return m.group(0)
                    if _bank_generic_guard(matched):
                        return m.group(0)
                    continue
                # 前导词在中间 → 保留中间部分，只脱银行名
                matched = matched[lead_start:]
                if _bank_generic_guard(matched):
                    return m.group(0)
                break
            if _bank_generic_guard(matched):
                return m.group(0)
            # D9：返回 前导词 + 脱敏（保留前导词，只脱银行名）
            if lead_prefix:
                return lead_prefix + rep.get("BANK", "XX银行")
            return rep.get("BANK", "XX银行")

        patterns.append((
            re.compile(r'[\u4e00-\u9fa5]{2,6}银行'),
            _bank_name_repl
        ))

        # R-㉔（v1.3.9，响应 Issue #15-C5，赵辉）：银行代码缩写表 + 字母+汉字混合形态
        # \b 边界让 QRCB_ 漏脱，改用 (?<![A-Za-z0-9])…(?![A-Za-z0-9])
        _BANK_CODES = cfg.get("bank_codes", [])
        if _BANK_CODES:
            _BANK_CODE_RE = re.compile(
                r"(?<![A-Za-z0-9])({})(?![A-Za-z0-9])".format(
                    "|".join(re.escape(c) for c in _BANK_CODES)
                )
            )
            patterns.append((_BANK_CODE_RE, lambda m: "XX银行"))

        # 字母+汉字混合形态：LZ银行 / ABC银行（独立于 bank_code_re 的通用形态）
        patterns.append((
            re.compile(r"[A-Za-z]{2,6}银行"),
            lambda m: rep.get("BANK", "XX银行")
        ))

        # R-㉕（v1.3.9，响应 Issue #15-C6，赵辉）：拉丁银行全称
        _LATIN_BANK_NAMES = [
            "Bank of China", "BOC",
            "China Construction Bank", "CCB", "Construction Bank of China",
            "Industrial and Commercial Bank of China", "ICBC", "Industrial and Commercial Bank",
            "Agricultural Bank of China", "ABC", "Agricultural Bank",
            "Bank of Communications", "BCM", "Bank of Communications Co.",
            "China Merchants Bank", "CMB",
            "China Minsheng Banking Corp", "CMBC", "Minsheng Bank",
            "Industrial Bank Co.", "CIB", "Industrial Bank Co., Ltd.",
            "China Everbright Bank", "CEB", "Everbright Bank",
            "Hua Xia Bank", "HX Bank",
            "Ping An Bank", "PAB", "Ping An Bank Co.",
            "Shanghai Pudong Development Bank", "SPD Bank", "SPD",
            "China CITIC Bank", "CITIC Bank",
            "Bank of Shanghai", "BOS",
        ]
        _LATIN_BANK_RE = re.compile(
            r"(?<![A-Za-z])({})(?![A-Za-z])".format(
                "|".join(re.escape(n) for n in _LATIN_BANK_NAMES)
            ),
            re.IGNORECASE
        )
        patterns.append((_LATIN_BANK_RE, lambda m: "[Bank]"))

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
        # ---- D10（赵辉，第6批，2026-10-09）：独立后缀不得吞并银行名——
        #   当 "分行"/"支行" 后紧跟 "银行"（如"各分行银行"）或单独"行"时不替换
        #   使用 `(?!分行银行|分行支行)` 负向前瞻：后面是"分行银行"/"分行支行"时不匹配
        #   org pattern 已移除分行/支行，故两者不再冲突
        standalone_suffixes = cfg.get("bank_branch_suffixes") or ["分行", "支行", "营业部"]
        if standalone_suffixes:
            # 构建 standalone alt：按长度降序排列；
            # 分行/支行各加 `(?!分行银行|分行支行)` 负向前瞻，其他正常
            parts = []
            for s in sorted(standalone_suffixes, key=len, reverse=True):
                if s in ("分行", "支行"):
                    parts.append(f"(?!分行银行|分行支行){re.escape(s)}")
                else:
                    parts.append(re.escape(s))
            patterns.append((
                re.compile(
                    # 左边界：不能是汉字(含全角标点)、ASCII字母数字
                    # 右边界：不能是汉字(含全角标点)、ASCII字母数字
                    # 全角标点(、，。 etc)不阻断匹配，如"总行、分行"中分行应被替换
                    rf'(?<![一-龥a-zA-Z0-9])(?:' + '|'.join(parts) + rf')(?![一-龥a-zA-Z0-9\u3000-\u303f\uff00-\uffef])'
                ),
                lambda m: "XX" + m.group(0)
            ))

        # R-㉖（v1.3.9，响应 Issue #15-C7，赵辉）：项目代号脱敏（从 config.json project_codes 读取）
        # 形态1：显式词表（秦领/农芯）→ XX；形态2：数字紧邻"工程/项目"（811工程→XX工程）
        # 严禁裸数字匹配——\b811\b 会击中日期碎片 11/18 和书签 _Toc28111
        _PROJECT_CODES = cfg.get("project_codes", [])
        if _PROJECT_CODES:
            _PROJECT_CODE_RE = re.compile(
                r"(?<![A-Za-z0-9])({})(?![A-Za-z0-9])".format(
                    "|".join(re.escape(c) for c in _PROJECT_CODES)
                )
            )
            patterns.append((_PROJECT_CODE_RE, lambda m: "XX"))

        # 数字型项目代号：仅限数字紧邻"工程/项目"，且数字≥3位
        # \d{3,} 只匹配数字部分（工程在 lookahead 不消耗），替换时保留工程/项目原文
        patterns.append((
            re.compile(r"(\d{3,})(?=工程|项目)"),
            lambda m: "XX"
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

    # ========== D5（赵辉，第3批，2026-10-08）：城市列举句逐项脱敏 ==========
    # 场景："总行营业部及昆明、昭通、曲靖、大理、红河、楚雄等分行"
    #   → 当前 _CITY_BANK_RE 只匹配"楚雄等分行"（最后一项）；
    #     其余城市（昆明/昭通/曲靖/大理/红河）因后接顿号均不匹配
    # 方案：先匹配"城市1、城市2、...城市N[等]分行/支行/营业部"整体，
    #       再在替换函数中逐项脱敏城市名（用同一套城市表），保留机构名
    # 城市列表：广西/四川/云南/贵州/湖南/湖北/安徽/河南/广东 各市
    # 注意：此块必须定义在 _CITY_BANK_RE 之前，且其 patterns.append 先于 _CITY_BANK_RE，
    #       防止 _CITY_BANK_RE 的 fallback 误吞独立城市名（如"玉溪"）。
    # 注：每个省份名改为可选 (?：省份名)?，使独立城市名（如"玉溪"）也可匹配
    _CITY_ENUM_CITIES = (
        "(?:广西)?(?:桂林|柳州|南宁|梧州|北海|防城港|钦州|贵港|玉林|百色|贺州|河池|来宾|崇左)|"
        "(?:四川)?(?:成都|绵阳|德阳|南充|宜宾|泸州|内江|乐山|自贡|眉山|雅安|广元|遂宁|攀枝花|达州|孝感|黄冈)|"
        "(?:云南)?(?:昆明|大理|丽江|红河|曲靖|玉溪|保山|昭通|临沧|普洱)|"
        "(?:贵州)?(?:贵阳|遵义|毕节|六盘水|安顺|铜仁|黔东南|黔南|黔西南)|"
        "(?:湖南)?(?:长沙|株洲|湘潭|衡阳|邵阳|岳阳|常德|张家界|益阳|郴州|永州|怀化|娄底|湘西)|"
        "(?:湖北)?(?:武汉|宜昌|襄阳|荆州|黄石|十堰|孝感|荆门|鄂州|黄冈|咸宁|随州|恩施)|"
        "(?:安徽)?(?:合肥|芜湖|蚌埠|淮南|马鞍山|淮北|铜陵|安庆|黄山|阜阳|宿州|滁州|六安|宣城|池州|亳州)|"
        "(?:河南)?(?:郑州|开封|洛阳|平顶山|安阳|鹤壁|新乡|焦作|濮阳|许昌|漯河|三门峡|南阳|商丘|信阳|周口|驻马店)|"
        "(?:广东)?(?:广州|深圳|珠海|汕头|佛山|韶关|湛江|肇庆|江门|茂名|惠州|梅州|汕尾|河源|阳江|清远|东莞|中山|潮州|揭阳|云浮)|"
        "(?:其他)?(?:北京|上海|天津|重庆)"
    )
    # 城市列举：城市1顿号城市2顿号...城市N（等?分行/支行/营业部/医院/学校/交易中心）
    # 核心修复（2026-10-11）："及昆明、昭通..."中"及"与第一个城市之间可能有顿号分隔符，
    #   须用 (?:(?:、|和|与)?及)? 替代 (?:及)?，让分隔符在"及"之后可选。
    # 第一分支：含列举标记（顿号/和/与/及/等）的完整列举句
    #   场景："总行营业部及昆明、昭通、...、楚雄等分行"
    # 第二分支：单城市+后缀兜底（无列举标记时直接整体替换为 XX）
    # D5 专业后缀（不含泛化机构名）：只匹配有特定语义的场景，
    # 泛化机构名（分行/支行/营业部/分理处/信用社）由银行机构规则独立处理。
    _SUFFIX_CITY_ENUM = (
        "保证金系统|公共资源交易中心保证金系统|公共资源交易中心"
        "|交易中心|医院|学校"
    )
    _CITY_ENUM_RE = re.compile(
        rf'(?:(?:、|和|与)?及)?(?:{_CITY_ENUM_CITIES}(?:、|和|与))*'
        rf'({_CITY_ENUM_CITIES})'
        rf'(?:(?:、|和|与)({_CITY_ENUM_CITIES}))*'
        rf'(?:等)?(?:{_SUFFIX_CITY_ENUM})'
        rf'|(?:{_CITY_ENUM_CITIES})(?:{_SUFFIX_CITY_ENUM})',
        re.I
    )
    _CITY_NAME_LIST_RE = re.compile(
        rf'(?:(?:、|和|与)?{_CITY_ENUM_CITIES}(?:、|和|与))*({_CITY_ENUM_CITIES})',
        re.I
    )
    def _city_enum_repl(m: re.Match) -> str:
        """
        城市列举句：逐项脱敏城市名，保留顿号/机构名。

        has_enum=True（含顿号/和/与/及/等）+ base 以顿号结尾：
          完整列举句，去掉机构后缀后逐项脱敏城市名，再拼回后缀。
        其他情况（含"等分行"、无列举标记的单城市）：直接返回 "XX"，
          防止"分行等分行"等边界情况被错误处理。
        """
        full = m.group(0)
        has_enum = any(sep in full for sep in ("、", "和", "与", "及", "等"))
        if not has_enum:
            # 单城市兜底：整体替换为固定值，避免后缀被后续机构规则二次命中。
            # "昭通交易中心" → "XX机构"（防止 PAT[35] 再吞"中心"→"XXXXXX"）
            # "玉溪保证金系统" → "XX机构"（单城市语义已脱敏，后缀可省略）
            return "XX机构"
        # 完整列举（含顿号/和/与/及/等）：去掉机构后缀，逐项脱敏城市名
        # 与 _SUFFIX_CITY_ENUM 保持一致（不含泛化机构名）
        for suf in ("保证金系统", "公共资源交易中心保证金系统", "公共资源交易中心",
                    "交易中心", "医院", "学校"):
            if full.endswith(suf):
                base = full[:-len(suf)]
                break
        else:
            base = full
        # 逐项脱敏城市名
        def _r(m2: re.Match) -> str:
            city = m2.group(0)
            return "XX" * ((len(city) + 1) // 2)  # GBK 等长
        result = re.sub(_CITY_NAME_LIST_RE, _r, base)
        if not full.endswith(base):
            result += full[len(base):]
        return result
    # D5 先于 _CITY_BANK_RE append，防止 fallback 吞独立城市名
    patterns.append((_CITY_ENUM_RE, _city_enum_repl))

    # D5（赵辉，第3批，2026-10-08）：桂林/柳州等常见城市名（配置外补充）
    # 桂林银行、柳州银行等机构名 → XX银行；单独出现 → XX（配合地址通道）
    # 须在姓名规则之前执行，防止"桂林张明"被截胡为"桂XXX明"
    _CITY_BANK_RE = re.compile(
        r'(?:(?:(?:(?:广西)?(?:桂林|柳州|南宁|梧州|北海|防城港|钦州|贵港|玉林|'
        r'百色|贺州|河池|来宾|崇左)|(?:四川)?(?:成都|绵阳|德阳|南充|宜宾|'
        r'泸州|内江|乐山|自贡|眉山|雅安|广元|遂宁|攀枝花|达州)|(?:云南)?(?:昆明|'
        r'大理|丽江|红河|曲靖|玉溪|保山|昭通|临沧|普洱)|(?:贵州)?(?:贵阳|遵义|'
        r'毕节|六盘水|安顺|铜仁|黔东南|黔南|黔西南)|(?:湖南)?(?:长沙|株洲|湘潭|'
        r'衡阳|邵阳|岳阳|常德|张家界|益阳|郴州|永州|怀化|娄底|湘西)|(?:湖北)?(?:'
        r'武汉|宜昌|襄阳|荆州|黄石|十堰|孝感|荆门|鄂州|黄冈|咸宁|随州|恩施)|(?:'
        r'安徽)?(?:合肥|芜湖|蚌埠|淮南|马鞍山|淮北|铜陵|安庆|黄山|阜阳|宿州|滁州|'
        r'六安|宣城|池州|亳州)|(?:河南)?(?:郑州|开封|洛阳|平顶山|安阳|鹤壁|新乡|'
        r'焦作|濮阳|许昌|漯河|三门峡|南阳|商丘|信阳|周口|驻马店)|(?:广东)?(?:'
        r'广州|深圳|珠海|汕头|佛山|韶关|湛江|肇庆|江门|茂名|惠州|梅州|汕尾|河源|'
        r'阳江|清远|东莞|中山|潮州|揭州|云浮)))(?:分行|支行|营业部)|(?:(?:'
        r'南宁|桂林|柳州|梧州|北海|防城港|钦州|贵港|玉林|百色|贺州|河池|来宾|崇左)'
        r'(?![\u4e00-\u9fa5a-zA-Z0-9])))'
    )
    def _city_bank_repl(m):
        full = m.group(0)
        if full.endswith(("分行", "支行", "营业部")):
            return "XX" + full[-2:]
        return "XX"
    patterns.append((_CITY_BANK_RE, _city_bank_repl))
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
    # ---- D10（赵辉，第6批，2026-10-09）：组织名规则中的分行/支行不得吞并银行名
    #   "总分行业务银行"/"各分行银行"中"分行"不应被替换
    #   策略：org pattern 第一分支中移除分行/支行（由 standalone suffix 规则处理）
    #   standalone suffix 含 D10 保护（(?!(?:分行银行|分行支行))），不吞银行名
    if org_suffixes:
        suffix_alt = '|'.join(re.escape(s) for s in org_suffixes)
        patterns.append((
            re.compile(
                rf'(?:营业部|科技部|运营部|管理部|董事会|监事会|管委会|事业部)|'
                rf'(?<![一-龥])(?:{suffix_alt})(?![一-龥])|'
                rf'(?<![一-龥])[一-龥]{{2}}(?:部|科|中心|管委会|办公室)(?![一-龥])'
            ),
            rep.get("ORG", "XXXX")
        ))


# ---------- 13. 人员姓名（2-3个汉字，粗筛） ----------
    # ↓↓↓ 姓名规则从本行开始单独归组：apply_redactions 中地址通道在其之前执行 ↓↓↓
    _pre_pattern_count = len(patterns)

    # D16（陈黎 Issue #45，2026-10）：精确全名匹配（7398人名优先于字符池规则）
    # - 从 config.json full_name_list 加载 7398 个已知全名
    # - 长名优先排序（防止"欧阳"截断"欧阳娜娜"）
    # - 边界：仅排除 ASCII/数字（允许中文连接符如"和/在/是/等"）
    # - 执行顺序：精确匹配 → 字符池规则
    _full_name_list: List[str] = cfg.get("full_name_list", [])
    # D16 库净化：剔除「字段标签型」条目。
    # 实测污染样本：「姓名」被当作人名全名收录，导致「请填写姓名」→「请填写XXX」。
    # 这类条目不是人名，是文档字段名；用显式清单过滤而不是改数据文件，
    # 便于上游修正 full_name_list 后自动恢复。
    _full_name_list = [n for n in _full_name_list if n not in _D16_NON_NAME_ENTRIES]
    # 【执行顺序修正（Issue #49 P1，2026-10-26）】
    # D16 精确层的 pattern **延后**到 S2 名单块救援之后再 append。
    # 原因：S2 的证据是「块内含 D16 已确认真名」；若 D16 先跑，
    # 块内确认真名已被替换成 XXX → 救援信号自我销毁 → 名单块整块漏脱。
    # 典型残留：`郑云、杨瑜` → `XXX、杨瑜`（杨瑜未脱敏）。
    # D16 只做「文本精确匹配」，延迟 append 不影响其判定逻辑。

    # Python 3.9 不支持变长 lookbehind，移除 excluded 检查
    # 姓氏后必须跟 1-2 个名字汉字，防止单字被误判
    # 名字用字池从 config.json name_pool 动态读取
    surname_alt = '|'.join(re.escape(s) for s in SURNAME_SET)
    # M9（Issue #49）：载入期清洗，剔除标点/符号类污染（防后续扩池再次污染）
    name_pool_chars = _get_name_pool_chars(cfg)
    # 名字汉字类（供两个规则使用）
    name_char_class = _build_name_char_class(name_pool_chars)

    # -----------------------------------------------------------------------
    # Issue #49 P1（M2 开关）：规则A 分级改造
    # -----------------------------------------------------------------------
    #
    # name_rule_mode = "graded"（默认）：
    #   规则A（唯一**无上下文约束**的姓名规则）按姓氏分档拆成
    #     R2（低危姓 + 1~3 名字字）/ R3（高危姓 + 2~3 名字字），
    #   并施加「姓氏位字频闸门 + 名字位字频闸门」双重收紧；
    #   另加 R4 上下文锚点规则（锚点 + 任意姓 + 1~2 名字字）。
    #
    # name_rule_mode = "strict"：
    #   退回 #49 整改前的单档规则A（姓 + 名字字{1,3}，全池），供应急回退。
    #
    # 两个关键设计决定：
    # 1) 闸门用**字符类收紧**实现，而非回调后置判定。贪婪量词下后置判定
    #    会整体放弃匹配，反而漏脱姓名（「张明和」→ 漏「张明」）。
    # 2) 闸门**不作用于上下文强的规则**。规则C-1/C/D/E 的左上下文是
    #    冒号/介词/动词，置信度来源是上下文而非字频，再叠字频闸门属于
    #    连坐，会误伤真实姓名（如「王根利」——「根」在 D16 名字位仅出现
    #    3 次，但它是真实人名用字）。
    #    但规则B 的左上下文只是「任意中文字」，属**弱上下文**——它正是
    #    「流程优化/日常运营/业务发展/客户经理」4 条误伤的来源，
    #    因此规则B 也必须加闸门（实测见下）。
    #
    # 3) 名字位判据是「尾字热 + 前字温」而非「全字都热」：
    #        王根利  → 根(温,3) + 利(热,44)   ✓
    #        流程优化 → 优(2) + 化(2)         ✗
    _name_rule_mode = _get_name_rule_mode(cfg)

    # -----------------------------------------------------------------------
    # S2 名单块一致性救援规则（Issue #49 P1）
    # -----------------------------------------------------------------------
    #
    # 【机制】顿号/逗号并列的**块**内，若存在一个已确认为真名的项（D16 精确表），
    # 则同块其余项按真名处理。
    #
    # 【为什么这是最有效的救援通道】
    #   实测 119 份真实文档：名单块信号覆盖 336 个真名金标，
    #   而「字频宽松档」单独只能覆盖 428/453（且难例误伤 1817）。
    #   名单块的误伤仅 19 条（对比字频宽松档的 1817）——
    #   因为「业务词并列」不会与真名同块共现，而「同事名单」几乎必然并列真名。
    #   这是**列表一致性**这一结构性证据的价值：它与字频弱相关，且几乎不误伤。
    #
    # 【与 D16 精确层的执行关系（2026-10-26 修正）】
    #   本规则的证据是「块内含 D16 已确认真名」；若排在 D16 之后，
    #   D16 名已被替换成 XXX → 救援信号自我销毁 → 名单块整块漏脱。
    #   故本规则排在 D16 精确层**之前**。
    #
    #   但排在前面会带来第二个问题：D16 精确层的右边界是 `(?![a-zA-Z0-9])`，
    #   而替换占位符 `XXX` 恰好是 ASCII 字母 → 若把非 D16 项替换成 XXX
    #   且紧贴保留的 D16 名（`郑云XXX`），D16 层就再也匹配不到「郑云」，
    #   实测金标保留率从 88.7% 掉到 63.7%。
    #
    #   **解法：块内所有项统一替换**，不把 D16 项留给精确层。
    #   块的一致性证据已经判定「整块都是人名」，整块脱敏既正确，
    #   又让本规则与 D16 层的执行顺序彻底解耦（谁先谁后都对）。
    #
    # 【项的形态】姓 + 1~3 个名字池字（不叠字频闸门——闸门在此属于连坐）。
    _list_item = rf'(?:{surname_alt}){name_char_class}{{1,3}}'
    _list_sep = r'[、，,/／]'
    # 块 = 连续的「项(分隔符项)+」。
    #
    # 【实现缺陷修复（2026-10-26，实测发现）】
    #   原写法把「分隔符 + 项」写成**前瞻** `(?={_list_item}...)`，
    #   导致捕获组 block 只含「第一项 + 分隔符」——**整个块并未被捕获**。
    #   后果是本规则赖以成立的「块内一致性证据」整体失效：
    #     S2「块内含 D16 确认真名」退化为「该单项是否在 D16」
    #     S4「块内每一项都符合严格形态」退化为「该单项符合即可」
    #   实测症状：真实文档中「王松/吴学亮/叶新林/马娟」这类名单，
    #   叶新林（D16 确认真名）反而因块只捕获到「叶新林/」而漏脱。
    #   修正：改为**真正捕获**整块，块内一致性判据才真正生效。
    _list_block_re = re.compile(
        rf'(?<![a-zA-Z0-9])'
        rf'(?P<block>(?:{_list_item})'
        rf'(?:{_list_sep}(?:{_list_item})(?![a-zA-Z0-9]))+)'
    )
    _d16_confirmed = set(_full_name_list)
    _name_rep_block = rep.get("NAME", "XXX")

    def _list_rescue_repl(m: re.Match) -> str:
        """名单块一致性救援：块内含 D16 真名 → 同块各项整体脱敏。"""
        block = m.group("block")
        # 分隔符用捕获组保留，避免 re.split 把顿号/逗号吃掉
        items = re.split(f"({_list_sep})", block)
        # 块内是否含 D16 已确认真名？
        if not any(it.strip() in _d16_confirmed for it in items):
            return block  # 无确认真名 → 不动（避免把业务词并列误判为名单）
        if _is_protected_by_common_word(m.string, m.start(), m.end()):
            return block
        # R-㊷（业务词守卫）：块内任一项命中业务词 → 整块放弃。
        # 理由：名单块的一致性证据建立在「块内各项都是同一类东西」上，
        # 一旦出现「白名单、单位」「活期、定期」这类业务词并列，
        # 说明整个块是业务枚举而非人员名单，不应按名单脱敏。
        for it in items:
            s = it.strip()
            if s and _is_protected_by_biz_word(it, 0, len(it)):
                return block
        out = []
        for it in items:
            if re.fullmatch(_list_sep, it):
                out.append(it)          # 分隔符原样保留
                continue
            stripped = it.strip()
            if not stripped:
                out.append(it)
                continue
            out.append(it.replace(stripped, _name_rep_block))
        return "".join(out)

    patterns.append((_list_block_re, _list_rescue_repl))

    # -----------------------------------------------------------------------
    # S4 名单块「纯姓名形态」救援规则（Issue #49 P1 新增）
    # -----------------------------------------------------------------------
    #
    # 【缺口】S2 需要「块内含 D16 已确认真名」作证据，但真实文档里存在
    #   **整组同事都不在 D16 库**的名单块（外来团队、乙方人员、新入职）。
    #   实测：库外真名所在块的「同块 D16 邻居」命中率为 **0%**——
    #   这条路对库外真名完全无效，缺口无法靠 S2 补上。
    #
    # 【替代证据】块内**全部**项都符合严格姓名形态（受闸门姓氏 + 名字池字 +
    #   长度 2~3）——这是纯结构信号，不依赖任何外部名单。
    #
    # 【为什么误伤极低】
    #   119 份真实文档全量扫描：满足「块内全为严格姓名形态」的项共 41 个，
    #   其中 34 个本来就已被 D16/S1 覆盖，**新增覆盖仅 7 个**
    #   （张榕/林润/卢蔷伊/张惠蓉/连玲典/陈雪香/刘宝香）——逐条核对全是人名，
    #   **零业务词命中**。
    #   通用语料 102 条：0 命中。
    #
    # 【为什么必须严】
    #   S2 的块正则本身就能匹配业务词并列（「成本控制、需求变更」——
    #   「成」是姓氏、「本控制」在名字池内）。所以 S4 必须比 S2 更严：
    #   姓氏须过闸门（freq≥5）、长度限 2~3、且**块内每一项都须满足**，
    #   任何一项是业务词则整块放弃。
    #
    # 【与 S2 的分工】
    #   S2：块内含 D16 确认真名 → 整块脱敏（证据来自名单库）
    #   S4：块内全部为严格姓名形态 → 整块脱敏（证据来自结构一致性）
    #   两者互为补充，均为「列表一致性」这一结构性证据的两种取证方式。
    _s4_gated_surnames = set(_get_gated_surnames(cfg)) if _name_rule_mode == "graded" else set(SURNAME_SET)
    # 单项形态：受闸门姓氏 + 1~2 个名字池字（无命名组，便于内嵌到块正则）
    _s4_item_pat = (
        rf'(?<![a-zA-Z0-9])(?:{"|".join(re.escape(s) for s in sorted(_s4_gated_surnames))})'
        rf'{name_char_class}{{1,2}}(?![a-zA-Z0-9])'
    )
    _s4_block_re = re.compile(
        rf'(?<![a-zA-Z0-9])'
        rf'(?P<block>(?:{_s4_item_pat})'
        rf'(?:{_list_sep}(?:{_s4_item_pat})(?![a-zA-Z0-9]))+)'
    )

    def _s4_rescue_repl(m: re.Match) -> str:
        """名单块纯姓名形态救援：块内每项都符合严格姓名形态 → 整块脱敏。"""
        block = m.group("block")
        if _is_protected_by_common_word(m.string, m.start(), m.end()):
            return block
        parts = re.split(f"({_list_sep})", block)
        names = [p for p in parts if p and not re.fullmatch(_list_sep, p)]
        # 每一项都必须是严格的姓名形态，否则整块放弃
        for it in names:
            s = it.strip()
            if not (2 <= len(s) <= 3):
                return block
            if s[0] not in _s4_gated_surnames:
                return block
            if any(c not in name_pool_chars for c in s[1:]):
                return block
            # R-㊷（业务词守卫）：项命中业务词 → 整块放弃。
            # S4 的证据是「块内每项都是严格姓名形态」，一旦某项其实是
            # 业务词（白名单/单位/活期/定期…），说明整个块不是人员名单。
            if _is_protected_by_biz_word(it, 0, len(it)):
                return block
        out = []
        for it in parts:
            if re.fullmatch(_list_sep, it):
                out.append(it)
                continue
            s = it.strip()
            out.append(it.replace(s, _name_rep_block) if s else it)
        return "".join(out)

    patterns.append((_s4_block_re, _s4_rescue_repl))

    # D16 精确层（延后到 S2 之后 append，理由见上方 S2 注释）
    # 长名优先（降序），确保复姓等长名不被短名截断
    # 边界：排除 ASCII/数字前后跟随；允许中文跟随（如"邹阳和陈黎"）
    if _full_name_list:
        _sorted_names = sorted(_full_name_list, key=len, reverse=True)
        # 【边界按名字长度分层（2026-10-26，实测发现）】
        #
        # 原边界 `(?<![a-zA-Z0-9])(?:...)(?![a-zA-Z])` 把「ASCII/数字 + 姓名」
        # 形态整类拦掉。真实文档里这类形态极常见（表格里姓名紧贴系统代号/人数）：
        #   IDATA-BDS季海涛 / FB-APPSVR卫海啸 / 研发组11张海翔 / OF-BANK赵乐品
        # 实测导致 119 份文档中多条**库内确认真名**漏脱。
        #
        # 但**不能无差别放宽**：把左边界改成「不排除 ASCII/数字」会让
        # 「组余翔」「宋广举」这类「紧跟汉字的库内名」漏脱（实测多漏 8 个）。
        #
        # 【分层判据】按名字长度分别定边界——因为**误伤风险随长度递减**：
        #   2 字名（1990 条）：短，最容易是常用词的一部分（"白天"/"叶子"/"单元"）
        #        → 保持严格左边界 `(?<![a-zA-Z0-9])`，只放宽右边界（允许数字跟随）
        #   3 字及以上（5407 条）：长，几乎不可能是常用词的一部分
        #        → 左边界放宽为 `(?<![\u4e00-\u9fa5])`，允许 ASCII/数字/标点在前
        #
        # 实测（119 份文档 / 102 条通用语料）：
        #   分层方案  金标 783→785（+2）、新增误伤 0、新增漏脱 0
        _short_names = [n for n in _full_name_list if len(n) == 2]
        _long_names = [n for n in _full_name_list if len(n) >= 3]
        _d16_layers = []
        if _short_names:
            # 2 字名：左边界严格（排除 ASCII/数字在前），右边界允许数字/中文跟随
            _alt = "|".join(re.escape(n) for n in sorted(_short_names, key=len, reverse=True))
            _d16_layers.append(re.compile(rf'(?<![a-zA-Z0-9])(?:{_alt})(?![a-zA-Z])'))
        if _long_names:
            # 3 字及以上：左边界允许 ASCII/数字/标点，只排除紧跟汉字
            _alt = "|".join(re.escape(n) for n in sorted(_long_names, key=len, reverse=True))
            _d16_layers.append(re.compile(rf'(?<![\u4e00-\u9fa5])(?:{_alt})(?![a-zA-Z])'))
        for _pat in _d16_layers:
            patterns.append((_pat, rep.get("NAME", "XXX")))

    # -----------------------------------------------------------------------
    # Issue #49 P1：场景分层姓名规则（S1 裸姓名 / S2 名单块 / S3 强上下文）
    # -----------------------------------------------------------------------
    #
    # 【为什么必须分层，而不是「一刀切收紧闸门」】
    #
    # 实测数据（824 条真名金标 / 178 条库外真名 / 102 条通用语料 / 2955 条难例）：
    #
    #   裸姓名场景的单维度字频判据有**结构性天花板**：
    #     判据                      金标覆盖   库外覆盖   通用语料误伤  难例误伤
    #     sur>=5 tail>=12              70%       69%          12        675
    #     sur>=0 tail>=12              74%       70%          20        888
    #     sur>=0 tail>=3               94%       92%          30       1817
    #   → 放宽闸门能救回真名，但误伤同步爆炸；收紧闸门能压误伤，但真名漏脱。
    #   → 字频单维度**不存在**同时满足「零漏脱 + 低误伤」的参数点。
    #
    #   但**上下文信号与字频弱相关**——上下文救回的库外真名，字频上完全不可分辨：
    #     S1(严闸门 sur>=5/tail>=12) + S3(上下文全池) → 金标 92%、库外 98%、误伤仅 12
    #   → 结论：**场景分层**。裸姓名用严闸门（此处真名/业务词冲突最激烈），
    #     强上下文/名单块用宽匹配并由上下文本身充当证据。
    #
    # 【三个场景层】
    #   S1 裸姓名（无任何上下文）：姓氏位闸门 + 名字位「尾字热 + 前字温」。
    #      证据来自字频，故必须最严——这是误伤的主要来源地。
    #   S2 名单块（顿号/逗号并列）：**块一致性救援**——块内若含 D16 已确认真名，
    #      同块其他项按真名处理（列表一致性是强证据，且业务词并列不会
    #      与真名同块共现）。
    #   S3 强上下文（角色锚点 / 冒号 / 介词）：全池匹配。
    #      证据来自上下文，字频闸门在此属于「连坐」，会误伤真实姓名。
    #
    # 【量词语义（关键 bug 修复）】
    #   名字位必须按**长度**显式展开，不能写成 `TAIL{n,m}`：
    #     旧形态 `[温]{0,2}[热]` 再套 `{1,3}` → 实际是 `[温]{0,2}[热]{1,3}`，
    #     量词落在**尾字类**上而非名字位上：名字位可长达 5 字、尾字判定失效。
    #   新形态（见 _build_name_pos_class）：
    #     名字位 1~3 字 → `[温]{0,2}[热]`
    #     名字位 2~3 字 → `[温]{1,2}[热]`
    #     名字位 1~2 字 → `[温]{0,1}[热]`
    _name_rule_mode = _get_name_rule_mode(cfg)
    _hot_chars = _get_gated_name_chars(cfg)     # 名字位「热字」（尾字门槛）
    _warm_chars = _get_warm_name_chars(cfg)     # 名字位「温字」（非尾字门槛）

    # 规则A 专用：受字频闸门收紧的字符类（strict 模式下退化为全池）
    _gated_name_class = name_char_class
    if _name_rule_mode == "graded":
        _gated_name_class = _build_name_char_class(_hot_chars)

    # 复姓优先：2字复姓 + 1~3个名字汉字（防止复姓第二字被单姓规则误匹配）
    # 复姓本身是强信号（双字姓氏在现代汉语里几乎不与常用词碰撞），
    # 故复姓通道使用**全池**名字位，不叠字频闸门。
    compound_surnames = cfg.get("compound_surnames") or list(_NAME_GATE_COMPOUND_SURNAMES)
    if compound_surnames:
        compound_alt = '|'.join(re.escape(s) for s in sorted(compound_surnames, key=len, reverse=True))
        patterns.append((
            re.compile(
                rf'(?<![a-zA-Z0-9])'
                rf'(?:{compound_alt}){name_char_class}{{1,3}}'
            ),
            rep.get("NAME", "XXX")
        ))

    if _name_rule_mode == "graded":
        # S1 裸姓名：姓氏位闸门 + 名字位字频闸门（唯一无上下文约束的场景）
        #
        # 姓氏位闸门：只保留在 D16 全名库中确实作姓出现过的姓。
        # 这是残余误伤的主入口——「水平」「单元」「沙子」「明天」的结构都是
        # 「冷僻姓 + 高频名字字」，姓氏位才是判别点（详见 _get_gated_surnames）。
        _gated_surnames = _get_gated_surnames(cfg)
        _high_risk = set(cfg.get("high_risk_surnames") or "")
        _low_surnames = sorted(s for s in _gated_surnames if s not in _high_risk)
        _high_surnames = sorted(s for s in _gated_surnames if s in _high_risk)
        # R2 / R3：按姓氏分档，名字位字数区间不同
        # 高危姓（high_risk_surnames）更易与日常词首字碰撞 → 要求更长的名字位；
        # 低危姓保留「姓 + 1 字」通道，避免"张明""李雷"这类二字名漏脱。
        _lo_min, _lo_max = _NAME_GATE_LOW_RANGE
        _hi_min, _hi_max = _NAME_GATE_HIGH_RANGE
        if _low_surnames:
            _low_alt = '|'.join(re.escape(s) for s in _low_surnames)
            _low_pos = _build_name_pos_class(_hot_chars, _warm_chars, _lo_min, _lo_max)
            patterns.append((
                re.compile(
                    rf'(?<![a-zA-Z0-9])'
                    rf'(?:{_low_alt}){_low_pos}'
                ),
                rep.get("NAME", "XXX")
            ))
        if _high_surnames:
            _high_alt = '|'.join(re.escape(s) for s in _high_surnames)
            _high_pos = _build_name_pos_class(_hot_chars, _warm_chars, _hi_min, _hi_max)
            patterns.append((
                re.compile(
                    rf'(?<![a-zA-Z0-9])'
                    rf'(?:{_high_alt}){_high_pos}'
                ),
                rep.get("NAME", "XXX")
            ))

        # R3b：高危姓 + **单字名** + 「职位/职责后缀」右边界通道
        #
        # 【缺口】R3 要求高危姓的名字位 ≥2 字，导致「樊霖负责」「樊霖副组长」
        #   这类「姓名 + 职位/动词后缀」的形态漏脱——名字后紧跟中文时，
        #   贪婪量词回溯降级，S1 无法命中。
        #
        # 【为什么不能整体放宽高危姓的单字名】
        #   实测「高危姓 + 单字热字名」在通用语料中命中 11 处
        #   （宁波/方向/孔明/安庆/孟子/孔子/庄子/石家庄…）——放宽代价远大于收益。
        #   且「樊霖」在 D16 库中不存在（姓+名对共现为 0），
        #   无法用名单证据把它与「孔明」区分开。
        #
        # 【本通道的证据】**右边界**：名字后面紧跟职位/职责类词。
        #   「樊霖负责」「樊霖副组长」中，「负责/副组长」证明前面的两字是
        #   「人名 + 职务」的组合，而非一个双字常用词。
        #   实测通用语料 102 条中该形态 **0 命中**（宁波银行等已被
        #   银行/城市规则先行拦截），零误伤。
        _role_suffixes = [
            '负责', '牵头', '副组长', '组长', '主任', '经理', '主管', '专员',
            '工程师', '负责人', '联系人', '成员', '参会', '出席', '签名', '签字',
        ]
        _role_suffix_alt = '|'.join(re.escape(s) for s in _role_suffixes)
        _single_pos = _build_name_pos_class(_hot_chars, _warm_chars, 1, 1)
        patterns.append((
            re.compile(
                rf'(?<![a-zA-Z0-9])'
                rf'(?:{_high_alt}){_single_pos}'
                rf'(?=(?:{_role_suffix_alt}))'
            ),
            rep.get("NAME", "XXX")
        ))
    else:
        # strict：#49 整改前的单档规则A（全池）
        patterns.append((
            re.compile(
                rf'(?<![a-zA-Z0-9])'
                rf'(?:{surname_alt}){name_char_class}{{1,3}}'
            ),
            rep.get("NAME", "XXX")
        ))

    # -----------------------------------------------------------------------
    # S3 强上下文姓名规则（Issue #49 P1）
    # -----------------------------------------------------------------------
    #
    # 场景分层后，**有上下文约束**的规则统一走**全池**匹配：
    #   证据来自上下文本身（角色槽位 / 冒号 / 介词），字频闸门在此属于「连坐」——
    #   会误伤真实姓名（如「王根利」的「根」在 D16 名字位仅出现 3 次）。
    #   实测：S1 严闸门 + S3 全池 → 金标 92%、库外 98%、通用语料误伤仅 12。

    # 规则C-1：半角冒号后姓名（如"联系人:方培培"）
    # 全角冒号已由 Rule C 处理；半角冒号(:)ASCII 不在 [\u4e00-\u9fa5] 范围，需单独处理
    # R-F（Issue #5）：右边界同步移除
    patterns.append((
        re.compile(
            rf'(?<=:)(?:{surname_alt}){name_char_class}{{1,3}}'
        ),
        rep.get("NAME", "XXX")
    ))

    # 规则C：姓氏 + 1个名字汉字（左侧必须是全角冒号，解决"组长：樊霖"型漏检）
    # Rule B 要求 surname+2名字汉字，但"樊霖"只有 surname+1名字汉字，
    # 在"组长：樊霖副组长"中 Rule B 无法匹配（霖后跟CJK"副"），
    # Rule A 也无法匹配（霖后跟CJK"副"违反右边界）。
    # 本规则专门处理：全角冒号后紧跟「姓氏+1名字汉字」的场景，右边界允许CJK跟随。
    # R-F（Issue #5）：右边界同步移除
    patterns.append((
        re.compile(
            rf'(?<=：)(?:{surname_alt}){name_char_class}'
        ),
        rep.get("NAME", "XXX")
    ))

    # 规则D：姓氏 + 名字汉字（左侧是常见介词/动词，解决"由XXX负责"型漏检）
    # 仅添加最可靠的上下文：左侧为"由"时，人名概率最高。
    # R-F（Issue #5）：右边界同步移除（原"阻止ASCII/数字跟随"同致回溯降级）
    patterns.append((
        re.compile(
            rf'(?<=由)(?:{surname_alt}){name_char_class}{{1,3}}'
        ),
        rep.get("NAME", "XXX")
    ))

    # 规则E：姓氏 + 名字汉字（左侧是常见动词如"由、为、对"等）
    # 覆盖"为XXX安排"、"对XXX负责"等场景。
    # R-F（Issue #5）：右边界同步移除
    _name_follow_verbs = ['由', '为', '对', '让', '请', '告', '诉', '见', '任', '选', '用', '调', '指', '派', '承', '责', '主', '抓', '干', '经', '协']
    _verb_alt = '|'.join(re.escape(v) for v in _name_follow_verbs)
    patterns.append((
        re.compile(
            rf'(?<=(?:{_verb_alt}))(?:{surname_alt}){name_char_class}{{1,3}}'
        ),
        rep.get("NAME", "XXX")
    ))


    # -----------------------------------------------------------------------
    # S3' 角色锚点规则（Issue #49 P1 新增）
    # -----------------------------------------------------------------------
    #
    # 「组长：樊霖」「负责人 王根利」这类场景的证据是**角色槽位**而非字频。
    # 锚点后一律全池匹配——这一层是库外真名的主要救回通道
    # （实测：锚点信号覆盖 45 个金标真名，含库外真名）。
    #
    # 锚点词表复用 #38 D8/D11 角色槽位；锚点后允许 0~3 个名字位。
    # 右边界不设（与规则A 一致，避免贪婪回溯降级为部分匹配）。
    _anchor_alt = '|'.join(re.escape(a) for a in _NAME_GATE_ANCHORS)
    _anchor_re = re.compile(
        rf'(?<![a-zA-Z0-9])'
        rf'(?P<anchor>{_anchor_alt})'
        rf'(?P<sep>[:：\s]*)'
        rf'(?P<name>(?:{surname_alt}){name_char_class}{{1,3}})'
    )
    _name_rep = rep.get("NAME", "XXX")

    def _anchor_repl(m: re.Match) -> str:
        """锚点 + 姓名 → 保留锚点与分隔符，仅替换姓名部分。"""
        return f"{m.group('anchor')}{m.group('sep')}{_name_rep}"

    patterns.append((_anchor_re, _anchor_repl))


    # 拆分：姓名规则单独归组，供 apply_redactions 在地址通道之后执行
    _PATTERNS_PRE = patterns[:_pre_pattern_count]
    # D11 注入姓名组最前面（姓氏规则之前）——行首姓名列表优先匹配
    # 场景："梁红兵、各系统行方项目经理" → 行首姓名先被 D11 处理，
    # 姓氏规则对 "XXXX、各系统..." 中的 "方项" 无从下手。
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

def _label_for_replacement(replacement: str) -> str:
    """按占位符内容归类计数标签（与 REDACTION_LABELS 中文标签一致）。"""
    if "YYYY" in replacement or "MM" in replacement or "DD" in replacement:
        return "日期"
    if "@" in replacement:
        return "邮箱"
    if replacement.endswith("银行"):
        return "银行名"
    if replacement == "X.X.X.X":
        return "IP地址"
    if replacement == "XXXX":
        return "组织名"
    if replacement == "XXX":
        return "姓名"
    if replacement.startswith("0XX"):
        return "固话"
    # 长串全X（手机号 11位、身份证 18位、银行卡 16位等）→ 不归"地址"
    if (replacement.startswith("XXXXX") and replacement.isdigit()) or replacement in (
            "XXXXXXXXXXX",    # 手机
            "XXXXXXXXXXXXXXXXXX",  # 身份证
            "XXXXXXXXXXXXXXXX",    # 银行卡
    ):
        return "手机"
    if replacement.startswith("XX"):
        return "地址"
    return "其他"


# ---------------------------------------------------------------------------
# R-⑥（v1.3.3，响应 Issue #10-A）：段落级差异回写（通用工具，DOCX/PPTX 共用）
# ---------------------------------------------------------------------------

def redistribute_paragraph(texts: list, redacted: str) -> list:
    """
    段落级差异回写。

    Word/PPT 常把一句话拆进多个 run（拼写检查/字体切换），per-run 处理会漏掉
    跨 run 实体（"2015年4月"+"3日"、"2017"+"年"+"3"+"月"、跨 run 邮箱等）。
    本函数在整段文本 apply_redactions 后，用 SequenceMatcher 差异区间把变更
    精确回写到各 run 节点：未变更的 run 文本原样保留（格式不破坏），
    变更文本写入首个受影响节点。

    texts: 段内各 run 的原始文本（按文档顺序）
    redacted: 整段脱敏后的文本
    返回: 与 texts 等长的新文本列表
    """
    import difflib
    combined = "".join(texts)
    if combined == redacted:
        return list(texts)

    n = len(texts)
    starts = [0] * n
    pos = 0
    for k, t in enumerate(texts):
        starts[k] = pos
        pos += len(t)
    total = pos

    def _owner(p: int) -> int:
        """绝对位置 p（基于原始 combined）所属的节点下标。"""
        if n == 0:
            return 0
        if p >= total:
            p = total - 1
        if p < 0:
            p = 0
        for k in range(n):
            s = starts[k]
            e = s + len(texts[k])
            if s <= p < e:
                return k
        # p 落在空节点边界：返回该空节点或最后一个节点
        for k in range(n):
            if starts[k] == p:
                return k
        return n - 1

    out = [[] for _ in range(n)]
    sm = difflib.SequenceMatcher(None, combined, redacted, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # 原文逐节点归还（按节点区间整块分配，保持原 run 文本不动）
            p = i1
            while p < i2:
                k = _owner(p)
                node_end = starts[k] + len(texts[k])
                if node_end <= p:  # 空节点，前进
                    p += 1
                    continue
                e = min(i2, node_end)
                out[k].append(combined[p:e])
                p = e
        elif tag == "replace":
            # 替换文本写入首个受影响节点
            out[_owner(i1)].append(redacted[j1:j2])
        elif tag == "insert":
            # 新增文本：i1==0 写入首节点，否则追加到前一个字符所在节点
            k = _owner(i1 - 1) if i1 > 0 else 0
            out[k].append(redacted[j1:j2])
        # delete：原文丢弃，不产出

    return ["".join(chunks) for chunks in out]


# ---------------------------------------------------------------------------
# R-⑧（v1.3.3，响应 Issue #10-A）：角色上下文英文人名规则
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# N-B 层（Issue #16 REQ-NAME-001）：扩展角色关键词（核心+通用+分组三层）
# ---------------------------------------------------------------------------

# 核心角色词（高精度，常跟冒号分隔名单，如"项目经理：张三"）
_ROLE_KEYWORDS_CORE = (
    "组长", "副组长",
    "项目经理", "技术负责人", "业务负责人", "项目总监", "架构师",
    "技术总监", "产品经理", "需求负责人", "技术经理",
)
# 通用角色词（中等精度）
_ROLE_KEYWORDS_GENERAL = (
    "经理", "负责人", "联系人", "审批人", "复核人",
    "参与人", "参加人", "主办人", "主持人", "讲师", "签字人", "接口人",
    "编制", "审核", "批准", "承办", "承办人", "协办", "协办人",
    "拟稿", "校对", "分发", "参会人", "参会人员",
)
# 分组角色词（后接冒号/顿号，如"开发组：张三"）
_ROLE_KEYWORDS_GROUP = (
    "核心组", "开发组", "测试组", "业务组", "运维组",
    "项目组", "专家组", "评审组", "实施组", "设计组",
    "成员", "组员", "队员",
)

# 合并（兼容现有 _ROLE_NAME_RE 的冒号/空格后缀模式）
_ROLE_KEYWORDS = (
    _ROLE_KEYWORDS_CORE + _ROLE_KEYWORDS_GENERAL + _ROLE_KEYWORDS_GROUP
    + ("组：", "队：")  # 兼容旧格式
)

# R-㉙（v1.3.9，响应 Issue #15-C10，赵辉）：角色槽位姓名三件套
# 1. 精确排除表：含这些词整段跳过，不误伤"成员：需求、开发、测试人员"
_ROLE_EXCLUDED_PHRASES = {"需求", "开发", "测试", "技术", "业务", "项目", "产品",
                           "运营", "市场", "设计", "运维", "安全", "数据", "算法",
                           "模块", "系统", "版本", "编号", "任务", "阶段", "日期"}

# D8（赵辉，第3批，2026-10-08）：角色槽位连词守卫
# "王力和张明"中"张明"前导"和"，姓名规则无法触发；连词两侧均脱敏
# 约束：连词两侧必须是"姓氏字+名字用字"，避免捕到任意CJK块
# 延迟构建（在模块加载时通过 _load_config 读取，避免循环依赖）
_role_conj_re_cache: Optional[re.Pattern] = None

def _get_role_conjunction_re() -> re.Pattern:
    global _role_conj_re_cache
    if _role_conj_re_cache is not None:
        return _role_conj_re_cache
    cfg = _load_config()
    sp = cfg.get("surname_pool", "")
    if not sp:
        sp = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣邓单洪包诸左石崔吉钮龚林门龙段郑孔牛童浦施零厉付肖闫覃刘"
    # M9（Issue #49）：载入期清洗，剔除标点/符号类污染
    np_chars = _get_name_pool_chars(cfg)
    surname_alt = "|".join(re.escape(c) for c in sp)
    name_class = _build_name_char_class(np_chars)
    _role_conj_re_cache = re.compile(
        rf'({surname_alt}{name_class}{{1,3}})(和|与|及|或者|以及)'
        rf'({surname_alt}{name_class}{{1,3}})'
    )
    return _role_conj_re_cache

# H1/D11/D16（赵辉，第3批）：顿号名单保形 + 序号守卫
# 顿号列表逐块替换天然保形；追加"第X名"等序号结构防误捕
_ROLE_LIST_EXCLUDED = {
    "等", "如下", "包括", "共计", "合计", "共", "名",
    "第一名", "第二名", "第三名", "第四名", "第五名",
    "第六名", "第七名", "第八名", "第九名", "第十名",
    "倒数第一", "倒数第二", "倒数第三",
}

# F1~F6/G1（赵辉，第3批）：桂林/柳州场景口径
_ROLE_EXCLUDED_PHRASES.update({
    "桂林银行", "柳州银行", "南宁分行", "桂林分行", "柳州分行",
    "广西分行", "北部湾银行", "桂银", "柳银",
    "桂分", "桂行", "柳分", "柳行",
    "漓江", "桂山", "桂林", "柳州",
    "桂林市", "柳州市", "南宁市", "梧州市", "北海市",
    "防城港市", "钦州市", "贵港市", "玉林市", "百色市",
    "贺州市", "河池市", "来宾市", "崇左市",
})

# 2. 角色前缀扩展 + 姓名捕获（token 完整性断言）
# 形态："核心业务组：蔡元龙"、"测试组：张三"、无冒号"负责人李四"
_ROLE_NAME_RE = re.compile(
    r"(?:(?:"
    + "|".join(re.escape(k) for k in _ROLE_KEYWORDS)
    + r")[:：\s]*)([\u4e00-\u9fa5]{2,4})(?![\\u4e00-\u9fa5\d])"
)
# 3. 调整记录句式（无角色前缀）："将刘利坤、杨磊加入安全测试组"
# 动词放在捕获组内，保证 m.group(1) 总是全匹配
_ADJUST_RECORDS_RE = re.compile(
    r"将([\u4e00-\u9fa5]{2,4}(?:[、，][\u4e00-\u9fa5]{2,4})*(?:加入|调入|调整至|调整到))"
)
_ENGLISH_NAME_RE = re.compile(r"(?<![A-Za-z])[A-Z][a-z]{2,}(?![A-Za-z])")


def _apply_english_name_pass(text: str, counts: Dict[str, int]) -> str:
    """段落含角色词时，遮蔽段内英文人名（Lisa/David 等）。
    全大写术语（UAT/POS）与句首普通词不受影响——仅匹配首字母大写
    且非全大写的独立词，且限定角色上下文。"""
    if not text or not isinstance(text, str):
        return text
    out_lines = []
    for line in text.split("\n"):
        if any(k in line for k in _ROLE_KEYWORDS):
            def _repl(m: re.Match) -> str:
                counts["姓名"] = counts.get("姓名", 0) + 1
                return "XXX"
            line = _ENGLISH_NAME_RE.sub(_repl, line)
        out_lines.append(line)
    return "\n".join(out_lines)


def _apply_role_context_pass(text: str, counts: Dict[str, int]) -> str:
    """
    R-㉙（v1.3.9，响应 Issue #15-C10，赵辉）：角色槽位姓名三件套。

    1. 精确排除表：段落含排除短语（需求/开发/测试等）→ 整段跳过，不误伤
       "成员：需求、开发、测试人员"（无冒号，仅在角色词前缀形态下触发）
    2. 角色前缀+中文姓名捕获（token 完整性断言防"天阳宏业7人"误伤）：
       "核心业务组：蔡元龙" → "核心业务组：XXX"
    3. 调整记录句式（无角色前缀也可脱）：
       "将刘利坤、杨磊加入安全测试组" → "将XXX、XXX加入安全测试组"
    """
    if not text or not isinstance(text, str):
        return text
    out_lines = []
    for line in text.split("\n"):
        # 跳过含排除短语的行（如"需求、开发、测试人员"等非姓名语义）
        # 排除短语用词边界保护（防"核心业务组"被"项目"误命中）
        if _ROLE_EXCLUDED_PHRASES and any(
            re.search(rf"(?<!\w){re.escape(p)}(?!\w)", line) for p in _ROLE_EXCLUDED_PHRASES
        ):
            out_lines.append(line)
            continue
        # 三件套之一：角色前缀+姓名（token 完整性断言已在正则中）
        def _role_repl(m: re.Match) -> str:
            # H2 / D17 / E3（赵辉，第6批）：排除已知的非姓名语义词
            # "批准后即启动"中"批准"是关键词，但"后即"是动宾短语不应脱敏
            # 检查完整候选 + 前2字 + 前3字（覆盖"后即启动"的前缀词片段）
            name_candidate = m.group(1)
            if (name_candidate in EXCLUDED_COMMON_WORDS
                    or name_candidate[:2] in EXCLUDED_COMMON_WORDS
                    or name_candidate[:3] in EXCLUDED_COMMON_WORDS):
                return m.group(0)  # 保护，跳过替换
            counts["姓名"] = counts.get("姓名", 0) + 1
            return m.group(0)[:0 - len(m.group(1))] + "XXX"
        new_line = _ROLE_NAME_RE.sub(_role_repl, line)
        # 三件套之二：调整记录句式（无角色前缀也可脱）
        # 形态："将刘利坤、杨磊加入安全测试组" → "将XXX、XXX加入安全测试组"
        # 策略：匹配"将...动词"，在替换函数中只替换姓名，保留动词
        def _adjust_repl(m: re.Match) -> str:
            counts["姓名"] = counts.get("姓名", 0) + 1
            matched_text = m.group(0)  # e.g. "将刘利坤、杨磊加入安全测试组"
            for verb in ("加入", "调入", "调整至", "调整到"):
                vi = matched_text.find(verb)
                if vi >= 0:
                    # 提取"将"之后、动词之前的姓名段（去掉开头的"将"）
                    names_sub = matched_text[2:vi]  # "刘利坤、杨磊"
                    n_names = names_sub.count("、") + 1
                    names_replaced = "、".join(["XXX"] * n_names)
                    # 重建：保留"将"+替换后姓名段+动词+后缀
                    return "将" + names_replaced + verb + matched_text[vi + len(verb):]
            return matched_text
        new_line = _ADJUST_RECORDS_RE.sub(_adjust_repl, new_line)
        new_line = _ADJUST_RECORDS_RE.sub(_adjust_repl, new_line)
        # D8（赵辉）：连词守卫——"王力和张明"中"和"两侧姓名均脱敏
        def _conj_repl(m: re.Match) -> str:
            counts["姓名"] = counts.get("姓名", 0) + 2
            return "XXX" + m.group(2) + "XXX"
        new_line = _get_role_conjunction_re().sub(_conj_repl, new_line)
        out_lines.append(new_line)
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# N-B 层（Issue #16 REQ-NAME-001）：段落角色锚点扩散
#
# 触发条件：段落含角色关键词 + 冒号（`：/：/： `）
# 扩散策略：冒号后捕获所有 2-4 字姓名块（顿号分隔），批量替换为 XXX
#
# 示例：
#   "核心组：蔡新发、张瑜、李明" → "核心组：XXX、XXX、XXX"
#   "项目经理：王健负责"        → "项目经理：XXX负责"（无顿号，单名保留冒号后）
# ---------------------------------------------------------------------------

# 角色锚点正则：关键词 + 冒号（捕获冒号后的内容）
_ANCHOR_COLON_RE = re.compile(
    r"((?:"
    + "|".join(re.escape(k) for k in (_ROLE_KEYWORDS_CORE + _ROLE_KEYWORDS_GROUP + _ROLE_KEYWORDS_GENERAL))
    + r")[:：]\s*)(.+)"
)

# D16（赵辉，第3批，2026-10-08）：顿号名单保形规则
# 场景："工具支持：窦磊、王富生、王根利"（冒号+名单）或纯顿号名单"窦磊、王富生、王根利"
# 策略：
#   ① 含冒号名单：已由 _ANCHOR_COLON_RE + _apply_anchor_expand_pass 处理
#   ② 无冒号顿号名单：用 _LIST_NAME_RE 捕获"纯2-4字CJK块顿号"序列，
#      替换时逐块等长打码，保留顿号（保形）
# ③ 残字虚词守卫：_RESIDUE_STOP 防止 XXX 的B角 → XXXB角
_RESIDUE_STOP = set("的了和与及或等是在中上下将把被由从向对为以并而同则着过们")
_LIST_NAME_RE = re.compile(
    r"^[\u4e00-\u9fa5]{2,4}(?:[、，][\u4e00-\u9fa5]{2,4}){1,}$"
)
def _list_name_repl(m: re.Match) -> str:
    """顿号名单保形：逐块脱敏，保留顿号/逗号"""
    text = m.group(0)
    parts = re.split(r'([、，])', text)
    result = []
    for p in parts:
        if re.match(r'^[\u4e00-\u9fa5]{2,4}$', p):
            # 检查残字：若替换后前一块末字+当前块=残字组合则跳过
            if result and len(p) >= 2 and p[0] in _RESIDUE_STOP:
                result.append(p)
            # R-㊷：列表项本身命中业务词（活期/定期/票据池/商票保贴…）→ 保留原项
            elif _is_protected_by_biz_word(p, 0, len(p)):
                result.append(p)
            else:
                result.append("XX" * (len(p) // 2))
        else:
            result.append(p)
    return "".join(result)

# D11（赵辉，第3批，2026-10-08）：名单续行守卫
# 场景："梁红兵、各系统行方项目经理、实施厂商项目经理"
#   → 行首姓名 + 顿号 + 含人事语义词（项目经理/厂商/实施/组员/成员/负责人）
# 约束：后半段须含人事语义词（防止"核心、外围"等普通并列被误伤）
_PERSONNEL_HINT = {
    "经理", "厂商", "实施", "组员", "成员", "负责人",
    "主任", "总监", "部长", "处长", "科长", "主管",
    "工程师", "设计师", "架构师", "开发", "测试", "运维",
    "组长", "副组长", "顾问",
}
_LIST_CONTINUATION_RE = re.compile(
    # 非捕获组让贪婪 .* 吞掉整行；捕获组仅包裹行首模式
    # 分支1：纯CJK姓名（2-4字）+顿号/逗号（含全角，U+FF0C）
    # 分支2：XXX（3字母）+顿号/逗号 → 二字姓名被姓氏规则处理后
    # 分支3：XXX+单CJK+顿号/逗号 → 三字姓名被姓氏规则处理后
    # 贪婪 .* 确保吞掉整行内容（无换行符时不用 $ 锚点）
    r'^(?:[\u4e00-\u9fa5]{2,4}[、，,]|[X]{3}[、，,]|[X]{3}[\u4e00-\u9fa5][、，,]).*',
    re.MULTILINE
)


def _list_continuation_repl(m: re.Match) -> str:
    """名单续行守卫：仅替换行首姓名，保留顿号和后半段。

    正则匹配行首姓名（纯CJK或已处理XXX+单CJK）到行末，
    replacement 函数找第一个顿号来分离行首姓名和后半段。
    姓氏规则先于 D11 执行，输入可能是 "梁红兵、..." 或 "XXX兵、..."
    ——D11 只处理顿号分隔的多元素名单，且后半段必须含人事关键词。
    """
    full = m.group(0)
    # 残字守卫
    if len(full) >= 2 and full[0] in _RESIDUE_STOP:
        return full
    # 第一个顿号/逗号 = 行首姓名与后半段的分界点
    # 头模式最多匹配 4 CJK + 1 逗号 = 5 字符；从位置 5 起找第一个逗号
    # 这样可以避免后半段含多个顿号时取到错误的分隔符
    comma_pos = len(full)  # 默认取到末尾
    for sep in ('、', ',', '，'):
        idx = full.find(sep)
        if 0 <= idx < comma_pos:
            comma_pos = idx
    if comma_pos >= 5:
        comma_pos = len(full)  # 第一个分隔符在头模式之后，说明头模式吞掉了分隔符
    if comma_pos < 0 or comma_pos >= len(full):
        return full
    # 人事关键词守卫：后半段必须含人事关键词，才启用 D11
    tail = full[comma_pos:]
    if not any(kw in tail for kw in _PERSONNEL_HINT):
        return full
    # head = 行首姓名（纯CJK或XXX+单CJK），len(head) 为字符数
    head = full[:comma_pos]
    head_len = len(head)  # 字符数
    if head_len < 2:
        return full
    # 行首姓名 mask：按字符数折半（向上取整），最小 2
    mask_chars = max((head_len + 1) // 2, 2)
    masked_head = "XX" * mask_chars
    return masked_head + tail

# 姓名块正则：2-4 连续CJK字符（排除词/数字为主）
_NAME_BLOCK_RE = re.compile(
    "[\u4e00-\u9fa5]{2,4}"
)
# 排除词（不含语义的角色词/常见词）
_ANCHOR_EXCLUDED_NAMES: set = {
    "今日", "昨日", "明日", "一组", "二组", "三组", "四组",
    "本组", "各组", "组长", "组员", "核心", "开发",
    "测试", "业务", "运维", "项目", "专家", "评审",
    "实施", "设计", "组长", "加入", "调入",
    "负责", "协办", "承办", "编制", "审核", "批准",
    "拟稿", "校对", "分发", "参会", "需求",
}
# D11/D16/H1（赵辉，第3批）：合并顿号名单保形守卫 + 序号守卫
_ANCHOR_EXCLUDED_NAMES.update(_ROLE_LIST_EXCLUDED)


def _apply_plain_list_pass(text: str, counts: Dict[str, int]) -> str:
    """
    D16（赵辉，第3批）：顿号名单保形——无冒号的纯顿号姓名列表。

    场景：段落中含"窦磊、王富生、王根利"（无角色前缀），
    逐块脱敏姓名、保留顿号。
    残字守卫：若前一块末字 + 当前块=残字组合，跳过。
    人事关键词守卫：若行含人事关键词，跳过（由 D11 续行守卫处理）。
    """
    if not text or not isinstance(text, str):
        return text
    out_lines = []
    for line in text.split("\n"):
        # 人事关键词守卫：含人事关键词的顿号名单由 D11 处理
        if any(kw in line for kw in _PERSONNEL_HINT):
            out_lines.append(line)
            continue
        new_line = re.sub(_LIST_NAME_RE, _list_name_repl, line)
        out_lines.append(new_line)
    return "\n".join(out_lines)


def _apply_list_continuation_pass(text: str, counts: Dict[str, int]) -> str:
    """
    D11（赵辉，第3批）：名单续行守卫。

    场景："梁红兵、各系统行方项目经理、实施厂商项目经理"
    行首姓名 + 顿号 + 含人事语义词（项目经理/厂商/实施/组员等）→ 仅脱行首姓名。
    约束：后半段含人事语义词门槛，防止普通并列（"核心、外围"）误伤。
    """
    if not text or not isinstance(text, str):
        return text
    out_lines = []
    for line in text.split("\n"):
        new_line = _LIST_CONTINUATION_RE.sub(_list_continuation_repl, line)
        out_lines.append(new_line)
    return "\n".join(out_lines)


def _apply_anchor_expand_pass(text: str, counts: Dict[str, int]) -> str:
    """
    N-B 层（Issue #16 REQ-NAME-001）：段落角色锚点扩散。

    对含角色关键词 + 冒号的段落，扩散遮蔽冒号后所有姓名块。
    在 _apply_role_context_pass 之后执行（叠加而非替代）。

    示例：
      "核心组：蔡新发、张瑜、李明" → "核心组：XXX、XXX、XXX"
      "组长张斌负责"              → 由 _apply_role_context_pass 处理（已有规则）
    """
    if not text or not isinstance(text, str):
        return text

    out_lines = []
    for line in text.split("\n"):
        new_line = line

        def _anchor_repl(m: re.Match) -> str:
            prefix = m.group(1)  # e.g. "核心组："
            rest = m.group(2)    # e.g. "蔡新发、张瑜、李明"

            # 在冒号后内容中捕获所有 2-4 字姓名块
            result_parts = []
            last_end = 0

            for block_m in _NAME_BLOCK_RE.finditer(rest):
                word = block_m.group(0)
                # 排除词保护
                if word in _ANCHOR_EXCLUDED_NAMES:
                    continue
                # 排除数字为主的词块（日期/编码碎片）
                digit_ratio = sum(c.isdigit() for c in word) / len(word)
                if digit_ratio >= 0.4:
                    continue
                # 替换
                counts["姓名"] = counts.get("姓名", 0) + 1
                result_parts.append(rest[last_end:block_m.start()])
                result_parts.append("XXX")
                last_end = block_m.end()

            # 保留未匹配的后缀
            if last_end < len(rest):
                result_parts.append(rest[last_end:])

            return prefix + "".join(result_parts)

        new_line = _ANCHOR_COLON_RE.sub(_anchor_repl, new_line)
        out_lines.append(new_line)

    return "\n".join(out_lines)


def _counting_sub(pattern: re.Pattern, text: str, replacement,
                  counts: Dict[str, int]) -> str:
    """带计数的规则替换：每处命中 +1，按实际产出占位符归类。
    支持 callable 替换（如固话 0XX- 规则），按实际产出分类。"""

    def _repl(m: re.Match) -> str:
        # Agent 评审决策跳过：若本轮匹配文本在 skip 集合中，不遮盖
        if _get_skip_texts() and m.group(0) in _get_skip_texts():
            return m.group(0)
        # R-①（v1.3.2，响应 Issue #8）：字符串替换须经 m.expand 展开反向引用
        # （如日期范围规则的 \2=分隔符）。callable 传给 pattern.sub 时返回值
        # 不再展开 \1/\2，字面 "\2" 会直接落盘；expand 对不含反斜杠的纯文本
        # 替换串为恒等操作，零副作用。
        out = replacement(m) if callable(replacement) else m.expand(replacement)
        label = _label_for_replacement(out)
        counts[label] = counts.get(label, 0) + 1
        return out

    return pattern.sub(_repl, text)


def _name_guard_sub_counted(pattern: re.Pattern, text: str, replacement: str,
                            counts: Dict[str, int]) -> str:
    """带计数的姓名规则替换：仅统计实际替换（排除常用词保护/Agent skip 保留的命中）。"""

    def _repl(m: re.Match) -> str:
        if _is_protected_by_common_word(m.string, m.start(), m.end()):
            return m.group(0)
        # R-㊷：业务词保护守卫（G1 跨界 / G2 整体 / G3 指示词）
        if _is_protected_by_biz_word(m.string, m.start(), m.end()):
            return m.group(0)
        # Agent 评审决策跳过：若本轮匹配文本在 skip 集合中，不遮盖
        if _get_skip_texts() and m.group(0) in _get_skip_texts():
            return m.group(0)
        counts["姓名"] = counts.get("姓名", 0) + 1
        # 支持函数型 replacement（如 D11 的 _list_continuation_repl）
        return replacement(m) if callable(replacement) else replacement

    return pattern.sub(_repl, text)


def apply_redactions_counted(text: str) -> Tuple[str, Dict[str, int]]:
    """
    对文本执行全量脱敏替换（链式顺序），并返回各类别的替换处数。
    返回 (脱敏后的文本, 计数字典)。

    计数口径：每处替换动作 +1（如"XX省XX市XX区"计 3 处地址）。
    后处理纠错（恢复误脱词）不参与计数。
    """
    counts: Dict[str, int] = {}
    if not text or not isinstance(text, str):
        return text, counts

    pre_patterns, name_patterns = _get_pattern_groups()

    result = text
    # 第一阶段：姓名之前的规则（邮箱/日期/银行/支行/地名等）
    for pattern, replacement in pre_patterns:
        result = _counting_sub(pattern, result, replacement, counts)

    # R-⑮（v1.3.7，Issue #13-③）：无区号 7–8 位本地号码（上下文限定）
    # 须在地址通道之前执行——否则"电话：87517381"等可能被其它规则先行切分
    result = _apply_local_phone_pass(result, counts)

    # 第二阶段：回溯式地址脱敏通道（省/市/县/区/街道/路/楼盘/大厦/支行/门牌）
    # 必须在姓名规则之前执行，防止"金水/花园"等地址成分被姓名规则误吞
    result = _apply_address_pass(result, stats=counts)

    # 第三阶段：姓名规则（R1：带常用词覆盖保护，"说明书→说XXX"类误伤在应用层过滤）
    _name_repl = get_replacement("NAME", "XXX")
    for pattern, replacement in name_patterns:
        result = _name_guard_sub_counted(pattern, result, replacement, counts)

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
    # M9（Issue #49）：载入期清洗，剔除标点/符号类污染
    name_pool_chars = _get_name_pool_chars(cfg)
    _surname_set_recovery = SURNAME_SET  # 引用全局姓氏集
    _name_chars = set(name_pool_chars)  # 名字用字集合
    # R-⑱（v1.3.8，响应 Issue #14）：机构后缀守卫——恢复段本为修复地址通道截断
    # 的姓名（黄XX镇→XXX），但"银行/支行名→XX银行"等机构占位符同样产出 XX。
    # 当"姓氏字+XX+机构字"（如 于XX银/和XX心/李XX学）时误并致"关XXX行"类过遮盖。
    # 故 XX 后片段以机构/单位后缀开头时跳过合并；行政后缀（镇/村/路/县等）
    # 不纳入守卫，保住"黄XX镇→XXX"既有修复场景。
    _RECOVERY_ORG_SUFFIXES = (
        "银行", "支行", "分行", "营业部", "分理处",
        "公司", "中心", "学校", "学院", "大学", "医院", "集团", "研究院",
    )
    # 路型后缀守卫（2026-10-26，修复 #45 扩池引入的既有回归）
    #
    # 背景：Issue #45 精确全名匹配把姓名池从 152 扩到 1283 字，「路/道/号」
    # 等**地址用字**随之进入名字池。于是本恢复段把地址通道**自身输出的**
    # 「XX路」「XX号」占位符误判为"被截断的姓名片段"，合并成 XXX：
    #     位于XX路XX号  →  XXXXX号      （期望：位于XX路XX号）
    # 即地址占位符被吃掉、路名与门牌号的结构丢失（R15 门禁用例
    # test_address_tail 实测失败，二分定位引入点为 86f0bd5 / Issue #45）。
    #
    # 判别依据：恢复段的语义是"地址通道把一个**姓名**截断了 → 还原成完整姓名"。
    # 若 XX 后紧跟的字是**路型/门牌型后缀**（街路巷弄道号），说明这个 XX 是
    # 地址通道自己吐出的占位符（地址通道的终态就是「XX路XX号」），而不是被
    # 截断的姓名片段——此时绝不合并。
    #
    # 与机构后缀守卫（R-⑱）的关系：R-⑱ 拦的是"XX+机构名"（XX银行/XX支行），
    # 本守卫拦的是"XX+路型/门牌后缀"（XX路/XX号）。**行政后缀（镇/村/县/区/
    # 市/省）刻意不纳入**——「XX镇」是中间态而非终态，须由本段合并回姓名，
    # 以保住"黄日镇→XXX"既有修复场景。
    _RECOVERY_ROAD_SUFFIX_CHARS = frozenset("街路巷弄道号")
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
                        # 路型/门牌后缀守卫：XX 是地址通道自身的占位符，非姓名片段
                        if _ch_after in _RECOVERY_ROAD_SUFFIX_CHARS:
                            _pos = _idx + 2
                            continue
                        # R-⑱：XX 后以机构后缀开头 → 机构占位符，非截断姓名，跳过
                        if any(result.startswith(suf, _idx + 2)
                               for suf in _RECOVERY_ORG_SUFFIXES):
                            _pos = _idx + 2
                            continue
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
            # 注：该处日期仍处于脱敏状态（仅占位符形式变化），计数不回退
        _pos = _idx + 1

    # R-⑧（v1.3.3，响应 Issue #10-A）：角色上下文英文人名规则
    # 仅当段落（按行）含角色词时，遮蔽段内首字母大写英文词（Lisa/David…）。
    # 不做无差别英文遮蔽——UAT/POS/PO/OA 等业务术语不受影响。
    result = _apply_english_name_pass(result, counts)

    # R-㉙（v1.3.9，响应 Issue #15-C10，赵辉）：角色槽位姓名三件套
    # 仅当段落含角色关键词（且不含排除短语）时，遮蔽段内中文姓名
    result = _apply_role_context_pass(result, counts)

    # N-B 层（Issue #16 REQ-NAME-001）：角色锚点扩散——冒号后姓名批量脱敏
    # 在三件套之后叠加执行，专注处理"角色：名单"格式
    result = _apply_anchor_expand_pass(result, counts)

    # D16（赵辉）：顿号名单保形——无冒号的顿号名单逐块打码
    result = _apply_plain_list_pass(result, counts)

    # D11（赵辉，第3批）：名单续行守卫——行首姓名脱敏，保留顿号和后半段
    result = _apply_list_continuation_pass(result, counts)

    # R4 层（Issue #17）：分词边界 + 上下文评分
    # 仅当 name_mode 为 "r4" 或 "both" 时启用；"enum" 模式保持 v1.3.10 行为不变
    try:
        from r4_name_scoring import apply_r4_name_pass, get_name_mode
        if get_name_mode() in ("r4", "both"):
            result = apply_r4_name_pass(result, counts, mode=get_name_mode())
    except ImportError:
        pass  # R4 模块不可用时降级

    # D15（赵辉，第5批，2026-10-09）：表格列姓名预扫 → 全局注入
    # 在所有规则链之后，对预扫收集到的额外姓名做全文替换
    if _EXTRA_NAMES:
        _name_repl = get_replacement("NAME", "XXX")
        for name in _EXTRA_NAMES:
            if name and len(name) >= 2:
                # 使用词边界保护，避免子串误伤
                pat = re.compile(rf'(?<!\w){re.escape(name)}(?!\w)')
                # 注意：_counting_sub 只返回字符串（不是 subn 的 (str, n) 元组）。
                # 历史缺陷（d40fc57 起）：此处误按二元组解包，抛
                # ValueError: too many values to unpack (expected 2)。
                # 该异常发生在 apply_redactions_counted 的**末尾** D15 段，
                # 会连带丢弃本函数此前所有脱敏结果；而调用方
                # （redact_ppt/_excel 的 _process_xml_file）以 try/except
                # 兜住异常并直接跳过文件写回 → 整页/整表零脱敏（真实泄漏）。
                # 修复：只取返回值，计数由 _counting_sub 内部完成。
                result = _counting_sub(pat, result, _name_repl, counts)

    return result, counts


def apply_redactions(text: str) -> str:
    """
    对文本执行全量脱敏替换（链式顺序）。
    返回脱敏后的文本。

    v1.2.3：本函数为 apply_redactions_counted 的兼容包装，
    需要分类计数的调用方（excel/ppt 处理器）请使用 apply_redactions_counted。
    """
    result, _counts = apply_redactions_counted(text)
    return result


# ---------------------------------------------------------------------------
# R-⑬（v1.3.7，响应 Issue #13-①）：输出文件名脱敏（元数据泄漏治理）
#
# 背景：批量入口 pipeline.py 已对输出 stem 走规则脱敏 + 日期戳处理，但单文件
# CLI（redact_word/excel/ppt/image）未同步——默认输出名保留"郑州银行"等敏感词，
# 属元数据泄漏（正文已脱敏、文件系统里仍留客户名）。现抽为公共函数，所有入口
# 共用；用户显式指定 output_path 时不改写（尊重调用方意图）。
# ---------------------------------------------------------------------------

_FILENAME_DATESTAMP_RE = re.compile(
    r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"
)


def redact_filename_stem(stem: str) -> str:
    """对输出文件名主干执行脱敏。

    - apply_redactions 链式规则：银行名/地名/日期等 → 占位符
    - 8 位日期戳（20171120）→ YYYYMMDD（与 pipeline 口径一致）
    失败时保留原名，绝不影响正文脱敏。
    """
    if not stem or not isinstance(stem, str):
        return stem
    try:
        redacted = apply_redactions(stem)
        redacted = _FILENAME_DATESTAMP_RE.sub("YYYYMMDD", redacted)
        return redacted or stem
    except Exception:
        return stem


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
# R-⑤（v1.3.2，Issue #8 整改连带发现）：docProps/core.xml ISO 时间戳保护
#
# 背景：R2（v1.3.1）将日期规则右边界由 $ 放宽为 (?![0-9]) 后，日期规则开始
# 命中 core.xml 中的 ISO 8601 元数据时间戳（如 lastPrinted="2026-09-05T01:44:53Z"），
# 产出 "YYYY/MM/DDT01:44:53Z" 之类非法时间戳——openpyxl 直接判定工作簿损坏，
# python-docx 虽宽容但属性已损坏。时间戳属于低敏感元数据，须整体原样保留。
# ---------------------------------------------------------------------------

_ISO_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def protect_iso_datetimes(text: str) -> Tuple[str, List[str]]:
    """将 ISO 8601 时间戳替换为哨兵占位，返回 (保护后文本, 原值列表)。
    脱敏后须调用 restore_iso_datetimes 按原序还原。"""
    tokens: List[str] = []

    def _stash(m: re.Match) -> str:
        tokens.append(m.group(0))
        return f"\x00ISO{len(tokens) - 1}\x00"

    return _ISO_DATETIME_RE.sub(_stash, text), tokens


def restore_iso_datetimes(text: str, tokens: List[str]) -> str:
    for i, tok in enumerate(tokens):
        text = text.replace(f"\x00ISO{i}\x00", tok)
    return text


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


# ---------------------------------------------------------------------------
# R-⑪（v1.3.5，响应 Issue #11）：LibreOffice 转换器查找（跨平台公共实现）
# ---------------------------------------------------------------------------

# 常见安装路径兜底（用户自定义安装位置且未加入 PATH 时）
_LIBREOFFICE_CANDIDATES = [
    # Windows 默认/便携安装
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    # macOS（brew cask / 官方 dmg，均不自动进 PATH）
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    os.path.expanduser("~/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    "/usr/local/bin/soffice",
    "/opt/homebrew/bin/soffice",
    # Linux
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/lib/libreoffice/program/soffice",
    "/snap/bin/libreoffice",
]


def find_libreoffice() -> Optional[str]:
    """
    查找 LibreOffice 可执行文件路径（跨平台，纯标准库，无外部命令依赖）。

    查找顺序：
      1. SOFFICE_PATH 环境变量（用户显式指定，最高优先级）
      2. shutil.which 探测 PATH（soffice / soffice.exe / libreoffice / libreoffice.exe）
      3. 常见安装路径兜底（Windows / macOS / Linux）

    找不到返回 None（调用方按 R-④ 契约走安全降级：报错终止，不生成输出）。
    相比旧实现（subprocess 调用外部 which）：
      - Windows 原生 / Alpine 等 minimal 镜像无 which 命令时不再抛未捕获 FileNotFoundError
      - Git Bash 的 POSIX 风格路径问题消除（shutil.which 返回当前平台可执行路径）
    """
    # 1. 环境变量显式覆盖
    env_path = os.environ.get("SOFFICE_PATH", "").strip()
    if env_path and Path(env_path).exists():
        return env_path

    # 2. PATH 探测（标准库，跨平台；找不到返回 None 而非抛异常）
    for cmd in ("soffice", "soffice.exe", "libreoffice", "libreoffice.exe"):
        p = shutil.which(cmd)
        if p:
            return p

    # 3. 常见安装路径兜底
    for cand in _LIBREOFFICE_CANDIDATES:
        try:
            if cand and Path(cand).exists():
                return cand
        except OSError:
            # Windows 上非法路径字符等极端情况，跳过
            continue
    return None
