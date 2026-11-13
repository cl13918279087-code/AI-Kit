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
import threading
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

# D16 全名库中的「非人名」条目（字段标签 / 表头），必须排除出精确匹配层。
# 背景：实测 full_name_list 含「姓名」——它是文档字段名不是人名，
# 精确层会把它替换成 XXX（「请填写姓名」→「请填写XXX」），属误伤。
# 【定义位置】必须先于 get_full_name_set()——该函数体在**调用时**才引用本常量，
# 若定义在其后，模块导入后首次调用即抛 NameError。
_D16_NON_NAME_ENTRIES = frozenset({"姓名"})


def _get_extra_name_list(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """返回 D-5 补充名单条目（内联 + 外置文件合并）。

    【为什么也要外置】
    `config.json` 的 `extra_name_list` 是**人工确认真实姓名**清单
    （Issue #60 D-5：名字位生僻字、字频闸门救不回的漏脱修复），
    同样是客户 PII——分发包不能携带。
    与 `full_name_list` 同构，支持：
      - `extra_name_list_file`：每行一个姓名的外部文本文件；
      - `AIKIT_EXTRA_NAME_LIST`：环境变量优先级最高。

    合并语义：内联 ∪ 外置（去重保序）；外置缺失时静默回退内联，
    保证既有行为完全不变。
    """
    if cfg is None:
        cfg = _load_config()

    names: List[str] = []
    seen = set()

    for n in (cfg.get("extra_name_list") or []):
        if isinstance(n, str) and n and n not in seen:
            seen.add(n)
            names.append(n)

    rel = str(cfg.get("extra_name_list_file") or "").strip()
    env_file = os.environ.get("AIKIT_EXTRA_NAME_LIST", "").strip()
    if env_file:
        rel = env_file
    if rel:
        for p in _resolve_data_paths(rel):
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for line in text.splitlines():
                n = line.strip()
                if n and not n.startswith("#") and n not in seen:
                    seen.add(n)
                    names.append(n)
            break

    return names


def _get_full_name_list(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """返回 D16 全名库条目列表（内联 + 外置文件合并）。

    【为什么需要外置文件】
    `config.json` 的 `full_name_list` 是从真实业务语料中抽取的**自然人姓名**
    （约 7400 条）。它既是姓名规则字频自举的种子数据，也是精确匹配层——
    但把它固化在分发包里，会带来两个问题：
      1. **分发侧 PII 风险**：包一旦流出（内网扩散、二次分发、开发机同步），
         真实人名清单随之扩散，且难以追溯；
      2. **内网无法注入本行名单**：投产时真正该用的是「本行员工花名册」，
         而非语料派生名单。

    因此支持 `full_name_list_file`：指向一个**每行一个姓名**的外部文本文件，
    内网部署方用本行花名册覆盖/补充即可，无需改动代码。

    合并语义：内联列表 ∪ 外置文件（外置优先，去重保序）。
    外置文件缺失时静默回退到内联列表，保证既有行为完全不变。
    """
    if cfg is None:
        cfg = _load_config()

    names: List[str] = []
    seen = set()

    for n in (cfg.get("full_name_list") or []):
        if isinstance(n, str) and n and n not in seen:
            seen.add(n)
            names.append(n)

    # D-5 补充名单（Issue #60 §一 实测漏脱的低频生僻字姓名）：
    # 这些姓名的名字位字在语料字频中为 0，无法由字频闸门救回，
    # 但它们是**确知的真实人名**——按精确全名处理，槽位内外一律脱敏。
    # 【外置支持】与 full_name_list 同构：extra_name_list_file /
    #   AIKIT_EXTRA_NAME_LIST 指向外部确认真名清单，内网可注入本行名单。
    for n in _get_extra_name_list(cfg):
        if n not in seen:
            seen.add(n)
            names.append(n)

    rel = str(cfg.get("full_name_list_file") or "").strip()
    # 环境变量优先：内网部署方无需改动分发包内的配置文件即可注入本行花名册
    env_file = os.environ.get("AIKIT_FULL_NAME_LIST", "").strip()
    if env_file:
        rel = env_file
    if rel:
        for p in _resolve_data_paths(rel):
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for line in text.splitlines():
                n = line.strip()
                if n and not n.startswith("#") and n not in seen:
                    seen.add(n)
                    names.append(n)
            break

    return names


def _resolve_data_paths(rel: str) -> List[Path]:
    """数据文件路径解析：环境变量根 / 包根 / scripts/ 自身 / 原样。"""
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / rel,
        here / rel,
        Path(rel),
    ]
    env_root = os.environ.get("AIKIT_DATA_ROOT", "").strip()
    if env_root:
        candidates.insert(0, Path(env_root) / rel)
    return candidates


def get_full_name_set() -> set:
    """返回 D16 精确全名库（已剔除字段标签型污染条目）。

    Issue #50 N-C 区域检测层把「区域内至少一项命中 D16」作为
    上下文性准则的种子信号（§4.3 准则 2 / §4.5）。
    """
    global _FULL_NAME_SET_CACHE
    if _FULL_NAME_SET_CACHE is None:
        names = _get_full_name_list()
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


def _build_name_char_class_body(chars: str) -> str:
    """构造正则字符类的**内容部分**（不含首尾方括号）。

    与 `_build_name_char_class` 同规则，但供需要把字符类**嵌入到另一个
    字符类内部**的场景使用（例如 `(?![...])` 负向先行断言）。
    若直接嵌入带方括号的形式会形成 `(?![[...]])` 嵌套字符类——
    Python 会抛 `FutureWarning: Possible nested set`，且语义与预期不符。
    """
    chars = chars or _FALLBACK_NAME_POOL
    return "".join("\\" + c if c in "\\]^-[" else c for c in chars)


def _build_name_char_class(chars: str) -> str:
    """由名字用字序列构造安全的正则字符类。

    - 空序列回退到内置兜底池（`[]` 会让 re 报 unterminated character set）；
    - 转义 `]`、`^`、`-`、反斜杠等正则元字符，避免污染字符破坏字符类边界。
    """
    return "[" + _build_name_char_class_body(chars) + "]"


_NAME_POOL_SET_CACHE: Optional[set] = None


def _get_name_pool_set() -> set:
    """名字用字**集合**（带缓存）。

    L1 证据层要逐子串判定「其余字 ∈ 名字用字池」，逐次构造集合的开销
    不可接受，故在此缓存（原始池字符串变化时随 `_load_config` 重置）。
    """
    global _NAME_POOL_SET_CACHE
    if _NAME_POOL_SET_CACHE is None:
        _NAME_POOL_SET_CACHE = set(_get_name_pool_chars())
    return _NAME_POOL_SET_CACHE


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

# D16 全名库名字位字频缓存（按全名库内容 key）
_NAME_CHAR_FREQ_CACHE: Dict[str, Any] = {}


def _get_name_char_freq(cfg: Optional[Dict] = None) -> Dict[str, int]:
    """从 D16 全名库自举名字位字频。

    统计 full_name_list 中每个名字位（name[1:]，跳过姓位）的字频。
    数据源完全自洽——不依赖 jieba、不依赖外部词表。

    【字频表外置支持】（内网分发 PII 治理）
    `full_name_list` 是真实自然人姓名清单，内网分发包不能携带。
    但名字位字频（单字出现次数）是从中派生的**聚合统计量**，
    不含姓名组合，无法反推具体人名（名字位字池 1278 字，
    2 字组合空间约 163 万，单字频次无法定位）。

    因此新增配置键 `name_char_freq_map`：
      - 内网分发包中 `full_name_list` 置空（去 PII），
        字频改由该键提供 —— 闸门行为与原始完全一致，召回不掉。
      - 存在该键时优先使用；不存在则回退到从 full_name_list 自举
        （上游仓库行为不变，向后兼容）。

    返回 {字: 字频}；全名库缺失且无字频表时返回空 dict（调用方须降级到全池）。
    """
    if cfg is None:
        cfg = _load_config()

    # 优先读外置字频表（内网分发包：人名库已剥离，字频由字频表提供）
    ext = cfg.get("name_char_freq_map")
    if isinstance(ext, dict) and ext:
        freq: Dict[str, int] = {}
        for ch, f in ext.items():
            if isinstance(ch, str) and len(ch) == 1:
                try:
                    freq[ch] = int(f)
                except (TypeError, ValueError):
                    continue
        if freq:
            return freq

    names = _get_full_name_list(cfg)
    key = f"{len(names)}:{hash(tuple(names[:64]))}"
    cached = _NAME_CHAR_FREQ_CACHE.get(key)
    if cached is not None:
        return cached
    freq = {}
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
    names = _get_full_name_list(cfg)
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


def _is_rule_a_enabled(cfg: Optional[Dict] = None) -> bool:
    """规则A 主链路开关（Issue #50 §4.8 退出门槛落地）。

    规则A = 「无上下文约束的**单姓** + 名字池笛卡尔积粗筛」通道族：
      - R2 低危姓 + 名字位字频闸门
      - R3 高危姓 + 名字位字频闸门
      - R3b 高危姓 + 单字名 + 职务后缀

    这些通道是**全局形态粗筛**，不看任何上下文：
      - 优点：唯一能捞「D16 名单外人名」的机制（召回主力）；
      - 缺点：笛卡尔积全局匹配 → 误伤无上界（实测扩池后 51.0%）。

    Issue #50 §4.8 规定的退出门槛：
        N-C 区域 + D16 + L2 同构 的召回 ≥ 12/13，
        且 102 条通用语料误伤 ≤ 5%。

    门槛达成后，规则A 从**主链路**退出（`rule_a_enabled: false`），
    其召回职责由 N-C 区域层（T1 表格姓名列 / T2 正文名单段）承担：
      - 区域内逐项判定用**形态证据**（`is_name_shaped`），不看字频闸门；
      - 区域外一律不动（人做脱敏时也是先扫版面找名单区，区域外默认不动）。

    开关语义：
      - `true`（默认）：规则A 保留在主链路（现状行为，供回滚与对照）
      - `false`：规则A 退出主链路，仅由 N-C 区域层 + D16 + 上下文通道承担召回

    **保留回滚能力**：本开关只影响「规则A 是否进入主链路」，
    不改动 D16 / S2 / S3 / N-C 任何通道的语义，
    因此 `false → true` 可完整回滚到 #49 状态。

    【与复姓精确层的关系】
    Issue #50 §4.8 的规则A 定义写作「姓 + 名字池」，其中**复姓**分支
    （欧阳/司马/上官… + 全池名字位）在本轮实施中被**拆出**为独立的
    「S1b 复姓精确层」（见 `_is_compound_surname_enabled`），不再受本开关控制：
      - 依据：双字姓是**闭集强信号**（47 个复姓），在现代汉语里几乎
        不与常用词碰撞，实测在 102 条通用语料与 1200 条真实业务语料上
        **零误伤**；而单姓通道的误伤（39/102 形态成立）全部来自
        「单姓 + 名字池」的巨大笛卡尔积面；
      - 收益：规则A 退出后仍能捞回名单外的复姓人名
        （实测样本 `欧阳娜娜`、`司马光`），保证「漏脱是合规红线」不被牺牲；
      - 该层**不依赖单姓池**，因此不属于规则A 的笛卡尔积误伤面。
    """
    if cfg is None:
        cfg = _load_config()
    return bool(cfg.get("rule_a_enabled", True))


def _is_compound_surname_enabled(cfg: Optional[Dict] = None) -> bool:
    """S1b 复姓精确层开关（Issue #50 §4.8 规则A 退出时拆出）。

    保留此层的理由见 `_is_rule_a_enabled` 文档字符串末段：
    复姓是闭集强信号，实测在 102 条通用语料 + 1200 条真实业务语料上零误伤，
    且是规则A 退出后唯一仍能捞「D16 名单外复姓人名」的机制。

    开关语义：
      - `true`（默认）：复姓精确层进入主链路；
      - `false`：完全关闭（仅用于对照实验）。
    """
    if cfg is None:
        cfg = _load_config()
    return bool(cfg.get("compound_surname_enabled", True))

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
# 通道互斥（Issue #53 §3.3/§3.4）
#
# §3.3「机构 + 地名链整体脱敏」：当一个地名（银行品牌词表里的城市名）处在
#   「地名 + 机构后缀」的机构链中时（如「深圳高新北支行网点」），该地名
#   **不得被银行品牌规则抢先改写**——否则机构链被从中间切开
#   （「深圳XX高新北支行网点」）。正确做法是让位给地址通道整体收拢
#   → 「XX支行网点」（保留机构类型后缀）。
#
# §3.4「通道互斥断言」：银行名判定链命中后（脱敏为「XX银行」），
#   其后紧跟的机构链（分行/支行/营业部/网点/分理处）区间**锁定**——
#   姓名通道与地址通道不得再对该区间内部做任何替换，
#   「工商银行北京分行」→ 「XX银行北京分行」（保留分行层级，不被拆开）。
# ---------------------------------------------------------------------------

# 机构链后缀（与地址通道的「可跨后缀」集合对齐，另加「网点」）
_INSTITUTION_CHAIN_SUFFIXES = ("分行", "支行", "营业部", "网点", "分理处")
_INSTITUTION_CHAIN_RE = re.compile("|".join(_INSTITUTION_CHAIN_SUFFIXES))
# 机构链前最多允许多少字的地名/修饰成分（如「高新北」「人民路」）
_INSTITUTION_CHAIN_MAX_PREFIX = 12
# 已脱敏银行名 + 机构链 → 锁定区间（地址/姓名通道不得再切内部）
_BANK_CHAIN_LOCK_RE = re.compile(
    r"X{1,4}银行[一-龥]{0,6}?(?:分行|支行|营业部|网点|分理处)"
)
# 已脱敏银行名占位符（紧邻前文时，机构链归银行判定链所有）
_MASKED_BANK_TAIL_RE = re.compile(r"X{1,4}银行$")


def _masked_bank_before(text: str, pos: int) -> bool:
    """pos 之前是否紧邻已脱敏的银行名占位符（「XX银行」）。

    §3.4 通道互斥：银行判定链命中后，其后的机构链归银行链所有，
    其它通道不得抢先切分。
    """
    return bool(_MASKED_BANK_TAIL_RE.search(text[:pos]))


def _in_institution_chain(text: str, pos: int) -> bool:
    """判断 text[pos:] 是否紧跟「机构链后缀」（中间只允许汉字）。

    用于 §3.3：银行品牌规则在「地名 + 机构后缀」的机构链中必须让位，
    避免机构链被从中间切开（「深圳高新北支行网点」不被改成
    「深圳XX高新北支行网点」）。
    """
    window = text[pos:pos + _INSTITUTION_CHAIN_MAX_PREFIX]
    m = _INSTITUTION_CHAIN_RE.search(window)
    if not m:
        return False
    # 机构链后缀与地名之间只允许汉字（不允许标点/空格断开实体）
    return all("一" <= c <= "龥" for c in window[:m.start()])


def _bank_chain_locked_spans(text: str) -> List[Tuple[int, int]]:
    """返回被银行判定链锁定的区间列表（§3.4 通道互斥）。

    锁定区间 = 「XX银行」+ 其后紧跟的机构链（如「XX银行北京分行」）。
    区间内的地名/机构层级不得再被地址通道或姓名通道改写。
    """
    return [m.span() for m in _BANK_CHAIN_LOCK_RE.finditer(text)]


def _is_in_locked_span(spans: List[Tuple[int, int]], start: int, end: int) -> bool:
    """命中区间是否落在某个锁定区间内。"""
    for ws, we in spans:
        if ws <= start and end <= we:
            return True
    return False


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
# G6 切词守卫用词表（Issue #51，真实业务语料实测）
#
# 【用途】姓名规则把常用词切成两半时，命中片段是「后半截 + 下一词首字」
#   （「检索目标」← 检索|目标、「任务一」← 任务|一）。这类命中以**边界相邻**
#   而非重叠的方式暴露，G1（重叠）与 G4（整词重叠）都抓不到，
#   故用「命中片段的前缀/后缀是常用词」这一判据兜住。
#
# 【正确性前提：与真名库互斥】
#   排除常用词表里混着 7 个**真名后缀**：海洋/创业/恒信/建设/成立/腾飞/黎明
#   （对应真名 吴海洋/文创业/郭恒信/王建设/吴成立/陈腾飞/丁黎明）。
#   若对它们做前缀/后缀豁免，会造成真名漏脱——这正是「真名零泄漏」的红线。
#   故本表 = 排除常用词 − 与 D16 真名库有包含关系的词，在载入期一次算清。
# ---------------------------------------------------------------------------

_SAFE_CUT_WORDS: set = set()


def _build_safe_cut_words(cfg: Optional[Dict] = None) -> set:
    """构造「可安全用于切词守卫」的常用词集合。

    判据：该词是排除常用词，且与 D16 全名库**无任何包含关系**
    （既不是某真名的子串，也不包含任何真名）。
    """
    if cfg is None:
        cfg = _load_config()
    names = {n for n in _get_full_name_list(cfg) if isinstance(n, str)}
    names.discard("姓名")
    safe = set()
    for w in EXCLUDED_COMMON_WORDS:
        if len(w) < 2:
            continue
        conflict = False
        for n in names:
            if w in n or n in w:
                conflict = True
                break
        if not conflict:
            safe.add(w)
    return safe


def _get_safe_cut_words() -> set:
    """取切词守卫词表（带缓存）。"""
    global _SAFE_CUT_WORDS
    if not _SAFE_CUT_WORDS:
        _SAFE_CUT_WORDS = _build_safe_cut_words()
    return _SAFE_CUT_WORDS


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


# ---------------------------------------------------------------------------
# G4 整词保护表（Issue #51，响应真实业务语料实测）
#
# 【问题】前三级守卫（G1 跨界 / G2 整体 / G3 指示词）都依赖 `biz_guard_words`，
#   而那张表只有 135 条**银行业**词汇。真实业务语料（公司 Agent 规划文档，
#   178 份）里密集出现另一类高频复合词：
#       「编排链」「织架构」「范定义」「焦三大」「务名」「程中」「于向」
#   它们被姓名规则以「姓氏 + 名字用字」形态切出，逐个收录进 `biz_guard_words`
#   属打地鼠——真实语料的新词会持续冒出来。
#
# 【解法】新增 `common_word_guards`（config.json）作为**整词豁免表**：
#   1. 条目是**完整词**（如「编排链」「任务名称」「关键路径」），不是碎片；
#   2. 命中片段与表内词**重叠**即豁免（与 G1 同构，但词更长、语义更完整）；
#   3. 表内词与 D16 全名库**双向互斥**——入库前剔除与任何真名有包含关系的词，
#      因此本守卫**不可能**造成真名漏脱（与 `biz_guard_words` 同一纪律）。
#   4. 表由真实语料**自动挖掘**产出（scripts/mine_guard_words.py），
#      改动规则后重跑即可续挖，不靠人工枚举。
#
# 【为什么与 G1/G2 不重复】G1/G2 面向「切碎的碎片」（金子/于可/池业），
#   G4 面向「本身是完整术语、只是恰好由姓+名用字拼成」的词（编排链/关键路径）。
# ---------------------------------------------------------------------------

_COMMON_WORD_GUARDS: Optional[set] = None
_COMMON_WORD_GUARDS_MAXLEN: int = 0


def _get_common_word_guards() -> set:
    """取整词保护表（config.json → common_word_guards），带缓存。"""
    global _COMMON_WORD_GUARDS, _COMMON_WORD_GUARDS_MAXLEN
    if _COMMON_WORD_GUARDS is None:
        cfg = _load_config()
        words = {w for w in (cfg.get("common_word_guards") or [])
                 if isinstance(w, str) and w}
        _COMMON_WORD_GUARDS = words
        _COMMON_WORD_GUARDS_MAXLEN = max((len(w) for w in words), default=0)
    return _COMMON_WORD_GUARDS


_COMMON_WORD_GUARD_SUBSTRINGS: Optional[set] = None


def _get_common_word_guard_substrings() -> set:
    """取整词保护表中长度 2/3 的**全部子串**集合（带缓存）。

    【用途：G4 守卫的必要条件剪枝】
    G4 守卫的豁免条件是「命中片段被某个保护词的出现区间**完全覆盖**」，
    由此必然蕴含：**命中片段是某个保护词的子串**。
    故若命中片段不在本集合中，G4 一定不豁免 —— 可跳过其昂贵的
    「对全部保护词逐个 text.find」扫描。

    【为什么需要它】
    通用人名词典层（Issue #55）会产生远多于其它层的候选（实测 800 个/6.6K 字符），
    而 G4 守卫对每个候选都遍历 330 个保护词做窗口 find，实测占该层
    873ms / 800 候选（占总耗时 41%）。本剪枝把该开销降为 O(1) 查表，
    且**语义完全等价**（剪枝条件是 G4 豁免的必要条件，非充分条件）。

    【规模】
    330 个保护词 × 每词长度 ≤ 8 → 约 2000 条子串，构建一次后可复用。
    """
    global _COMMON_WORD_GUARD_SUBSTRINGS
    if _COMMON_WORD_GUARD_SUBSTRINGS is None:
        subs: set = set()
        for w in _get_common_word_guards():
            n = len(w)
            for L in (2, 3):
                if n < L:
                    continue
                for i in range(n - L + 1):
                    subs.add(w[i:i + L])
        _COMMON_WORD_GUARD_SUBSTRINGS = subs
    return _COMMON_WORD_GUARD_SUBSTRINGS


def _is_protected_by_common_word_guard(text: str, start: int, end: int,
                                       strict_longer: bool = False) -> bool:
    """G4 整词守卫：命中片段**完整落在**整词保护表的某个词内部 → 保留原文。

    【strict_longer=True（G4-S，Issue #53 §3.2「前缀成词整体保留」）】
    只在「表内词**严格长于**命中片段」时才算豁免——即命中片段只是某个
    完整组织实体的**一部分**（前缀成词），此时整词保留原文。
    与之相对，命中片段与表内词**完全相同**时不算豁免
    （否则「科技部」这类独立组织词将永不被脱敏，与既有脱敏设计冲突）。


    【语义：包含而非重叠】命中区间 [start, end) 必须被某个保护词的出现
    区间 [ws, we) **完全覆盖**（ws <= start 且 end <= we）才豁免。

    为什么不能用「重叠即豁免」：
      重叠语义下，保护词「张三」（占位人名）会与命中片段「张三丰」相交，
      从而把**真名**整体豁免 → 真名漏脱（回归 test_d15_extra_names_guard）。
      包含语义下，「张三丰」不被「张三」覆盖（张三 是 三丰 的前缀），
      因此真名照常脱敏；而「排链」被「编排链路」完整覆盖 → 仍然豁免。

    【真名优先】D15 注入的额外姓名（表格列预扫）与本表冲突时，
      以真名为准——注入名不做豁免。
    """
    words = _get_common_word_guards()
    if not words:
        return False
    matched = text[start:end]
    if not matched:
        return False
    # 【必要条件剪枝】G4 豁免要求命中片段被某个保护词的出现区间完全覆盖，
    # 故命中片段必然是某个保护词的子串。不满足 → 必然不豁免，直接返回。
    # 该剪枝是**语义等价**的（必要条件而非充分条件），仅用于跳过昂贵的
    # 「对全部保护词逐个窗口 find」扫描。详见 _get_common_word_guard_substrings。
    if len(matched) in (2, 3) and matched not in _get_common_word_guard_substrings():
        return False
    # G4-0 真名优先：命中片段与已注入姓名（D15 表格列预扫）重叠 → 不豁免
    for name in _EXTRA_NAMES:
        if not name:
            continue
        lo = max(0, start - len(name))
        hi = min(len(text), end + len(name))
        idx = text.find(name, lo)
        while idx >= 0 and idx < hi:
            if idx < end and start < idx + len(name):
                return False
            idx = text.find(name, idx + 1)
    # G4-a 整体命中：命中片段本身就是表内整词
    #   strict_longer（G4-S）：仅「表内词严格长于命中片段」才算豁免，
    #   等长命中（片段 == 表内词）不算——否则独立组织词永不被脱敏。
    if matched in words and not strict_longer:
        return True
    # G4-b 包含命中：窗口内任一表内词**完整覆盖**命中片段
    #   表内最长词有限，窗口左右各取 maxlen 足以覆盖判定
    hit_len = end - start
    pad = _COMMON_WORD_GUARDS_MAXLEN
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    for w in words:
        if len(w) < 2:
            continue
        if strict_longer and len(w) <= hit_len:
            continue
        idx = text.find(w, lo)
        while idx >= 0 and idx < hi:
            ws, we = idx, idx + len(w)
            if ws <= start and end <= we:
                return True
            idx = text.find(w, idx + 1)
    return False


def _is_overlapped_by_common_word_guard(text: str, start: int, end: int) -> bool:
    """G4-c 重叠命中（**仅供地址通道使用**）：命中片段与整词保护表**相交**即豁免。

    【为什么地址通道要放宽到重叠语义】
    姓名通道的 G4 用「包含」语义，是因为姓名片段若只是保护词的**一部分**
    （如「张三丰」之于「张三」），豁免就会漏掉真名。
    但地址通道的命中片段是「前缀 + 行政后缀」构成的**地理单元**：
        「含编排链路」+「路」 → 地址通道把「链」当地名前缀 → XX路
    此处保护词「编排链路」并不完整覆盖命中片段（命中片段多了一个「含」），
    「包含」语义失效；然而二者**相交**，足以判定这段是业务术语而非地名。

    【真名安全】保护表入库时已与 D16 全名库 / 金标**双向互斥**
    （无任何包含关系），因此相交的保护词不可能是真名本身，
    不会造成真名漏脱。
    """
    words = _get_common_word_guards()
    if not words:
        return False
    # G4-0 真名优先：命中片段与已注入姓名（D15 表格列预扫）重叠 → 不豁免
    for name in _EXTRA_NAMES:
        if not name:
            continue
        lo = max(0, start - len(name))
        hi = min(len(text), end + len(name))
        idx = text.find(name, lo)
        while idx >= 0 and idx < hi:
            if idx < end and start < idx + len(name):
                return False
            idx = text.find(name, idx + 1)
    pad = _COMMON_WORD_GUARDS_MAXLEN
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    for w in words:
        if len(w) < 2:
            continue
        idx = text.find(w, lo)
        while idx >= 0 and idx < hi:
            ws, we = idx, idx + len(w)
            if ws < end and start < we:
                return True
            idx = text.find(w, idx + 1)
    return False


# ---------------------------------------------------------------------------
# G7 跨词边界守卫（Issue #51，真实业务语料实测）
#
# 【问题】姓名规则把一个常用词**切成两半**时，命中片段横跨在两个词的边界上：
#     「依赖大量」→ 命中「赖大」（依赖|大量）
#     「指向真实」→ 命中「向真」（指向|真实）
#     「规范建设」→ 命中「范建」（规范|建设）
#     「基于静态」→ 命中「于静」（基于|静态）
# 这类误伤**不与任何完整词重叠**——它既不在「依赖」内部，也不在「大量」内部，
# 因此 G1（重叠）/ G2（整体）/ G4（整词包含）全都抓不到：
# 它们出错的正是**词与词之间那条缝**。
#
# 【判据】命中片段 [start,end) 若满足下列之一，即判为「跨词边界错切」：
#     A. 左邻字 + 命中首字 ∈ 边界对表   → 左边界切在了一个词的中间
#     B. 命中末字 + 右邻字 ∈ 边界对表   → 右边界切在了一个词的中间
# 之所以可信：判据成立时，被切的那个词**有一半落在命中片段之外**，
# 所以命中片段必然只是那个被切碎的词的一部分，不可能恰好是独立人名。
#
# 【正确性前提：与真名库双向互斥】
#     边界对表由 scripts/mine_boundary_pairs.py 挖掘，入表前已剔除与任何
#     真名（D16 全名库 + 评测金标）有包含关系的组合，故本守卫不会
#     因为「保护了真名的相邻二字」而造成真名漏脱。
#
# 【真名优先】命中片段本身若是真名（D16 / D15 注入名），一律不豁免。
# ---------------------------------------------------------------------------

_BOUNDARY_PAIRS: Optional[set] = None


def _get_boundary_pairs() -> set:
    """取跨词边界对表（config.json → word_boundary_pairs），带缓存。"""
    global _BOUNDARY_PAIRS
    if _BOUNDARY_PAIRS is None:
        cfg = _load_config()
        _BOUNDARY_PAIRS = {w for w in (cfg.get("word_boundary_pairs") or [])
                           if isinstance(w, str) and len(w) == 2}
    return _BOUNDARY_PAIRS


def _is_real_name_fragment(text: str, start: int, end: int) -> bool:
    """命中片段本身是否为真名（D16 全名库 / D15 注入名）。

    真名优先：命中片段本身是真名时，任何守卫都不得豁免它。
    """
    matched = text[start:end]
    if not matched:
        return False
    # D15 注入的额外姓名（表格列预扫）
    for name in _EXTRA_NAMES:
        if name and name in matched:
            return True
    # D16 全名库
    return matched in get_full_name_set()


_TRIMBACK_NAMES: Optional[set] = None


def _get_trimback_names() -> set:
    """取「可修剪真名」集合：D16 全名库 ∪ #55 通用人名词典 ∪ D15 注入名。

    【用途】Issue #75 的 G7 修剪回退（trim-back）——见
    `_find_trimback_name_span`。这是**已知真名**的并集，与 D16 分层边界、
    hot/warm/cold 字频分类完全正交（不改动任何既有闸门与分档结果）。
    """
    global _TRIMBACK_NAMES
    if _TRIMBACK_NAMES is None:
        names = set(get_full_name_set())
        names |= set(_get_common_name_dict())
        names |= {n for n in _EXTRA_NAMES if n}
        _TRIMBACK_NAMES = names
    return _TRIMBACK_NAMES


def _find_trimback_name_span(text: str, start: int, end: int) -> Optional[Tuple[int, int]]:
    """G7 命中时，在命中片段内寻找「可被单独脱敏的已知真名」区间。

    【背景】Issue #75：规则 D / E / S3' 锚点层的名字位是贪婪的
    （`姓氏 + 名字汉字{1,3}`），在「该文件由周竹婷签署」这类上下文里
    会把「周竹婷**签**」整体吞下；而 G7 跨词边界守卫识别出「签署」
    跨词 → **整段回退保留原文** → 人名整段泄漏。

    【解法】不整段回退，而是**只回退被误吞的那部分**：命中片段内若存在
    一个**已知真名**（D16 ∪ #55 词典 ∪ D15），就只脱敏该真名，
    被误吞的尾字原样保留（G7 B 分支的证据恰恰说明尾字属于下一个词）。

    【必要条件（缺一不可）】
    ① **左邻不跨词**：真名首字与其左邻字不成词
       （`指向真实` 里的「向真」因此被拒——左邻「指」+「向」= 指向 成词）；
    ② **吞字证据**：命中末字与其右邻字成词（G7 B 分支同源：说明尾字
       属于下一个词，而非名字的一部分），且真名后面**恰好只多 1 个尾字**
       ——多于此说明「真名 + 若干字」整体才可能是完整姓名
       （如「石艳平汇」→ 前缀「石艳」+「平汇」两字，此时修剪会制造
       「XXX + 残字」的 #57 残尾形态，宁可不脱）；
    ③ **真名可信**：该串是已知真名，首字为姓氏字，且不被任何
       常用词 / 整词保护 / 业务词 / 独立成词守卫豁免。

    【返回值】(name_start, name_end)；不满足条件返回 None（→ 整段回退）。
    """
    if end - start < 3:
        return None
    pairs = _get_boundary_pairs()
    if not pairs:
        return None
    # 条件②：吞字证据（与 G7 B 分支同源）
    if end >= len(text):
        return None
    if (text[end - 1] + text[end]) not in pairs:
        return None
    known = _get_trimback_names()
    for L in (3, 2):
        e = start + L
        # 条件②后半：真名后**恰好只多 1 个尾字**
        if e != end - 1:
            continue
        cand = text[start:e]
        # 条件③：已知真名 + 首字为姓氏字
        if cand not in known:
            continue
        if cand[0] not in SURNAME_SET:
            continue
        # 条件①：左邻不跨词
        if start > 0 and (text[start - 1] + cand[0]) in pairs:
            continue
        # 条件③后半：真名自身仍须过一遍常用词 / 业务词守卫
        if _is_protected_by_common_word(text, start, e):
            continue
        if _is_protected_by_common_word_guard(text, start, e):
            continue
        if _is_protected_by_biz_word(text, start, e):
            continue
        if _is_protected_by_standalone_exempt(text, start, e):
            continue
        return start, e
    return None


def _trimback_redact(m: re.Match, replacement: str,
                     counts: Dict[str, int]) -> Optional[str]:
    """G7 命中时的「修剪回退」脱敏（Issue #75）。

    命中片段形如「周竹婷签」——真名 + 被贪婪吞掉的尾字。G7 判定其跨词
    （尾字与右邻字成词）→ 常规做法是整段回退保留原文 → 人名整段泄漏。
    本函数改为：**只脱敏其中的已知真名**，尾字原样保留。

    返回替换串（供 `pattern.sub` 用）；不满足条件返回 None（→ 整段回退）。
    """
    if callable(replacement):
        # 函数型 replacement（如 D11 的 _list_continuation_repl）不做部分替换
        return None
    span = _find_trimback_name_span(m.string, m.start(), m.end())
    if span is None:
        return None
    ns, ne = span
    matched = m.group(0)
    rel_start = ns - m.start()
    rel_end = ne - m.start()
    counts["姓名"] = counts.get("姓名", 0) + 1
    return matched[:rel_start] + replacement + matched[rel_end:]


def _is_boundary_split_misfire(text: str, start: int, end: int) -> bool:
    """G7 跨词边界守卫：命中片段横跨在两个词的边界上 → 保留原文。"""
    pairs = _get_boundary_pairs()
    if not pairs:
        return False
    matched = text[start:end]
    if len(matched) < 2:
        return False
    # 真名优先：命中片段本身是真名 → 不豁免
    if _is_real_name_fragment(text, start, end):
        return False
    # A. 左邻字 + 命中首字
    if start > 0 and (text[start - 1] + matched[0]) in pairs:
        return True
    # B. 命中末字 + 右邻字
    if end < len(text) and (matched[-1] + text[end]) in pairs:
        return True
    return False


# ---------------------------------------------------------------------------
# 通用人名词典层（Issue #55）
# ---------------------------------------------------------------------------

_COMMON_NAME_DICT: Optional[set] = None

# 词典层候选长度（与 #55 词表构建口径一致：T2 尾字频次 / T3 名字位频次）
_COMMON_NAME_DICT_LENS = (3, 2)


def _get_common_name_dict() -> set:
    """取「通用人名词典层」词表（config.json → common_name_dict_file），带缓存。

    【为什么独立于 D16（Issue #55 工程约束①）】
    D16 全名库（7398 条）的本质是「**本项目语料内出现过的姓名**」，
    不是通用人名词典——实测抽查 86 个高频中文人名，66 个（77%）不在库。
    本层是**独立的词表型名单层**（与 #51 占位名层同类工程模式），
    **不混入 `full_name_list`**，避免「张三丰」被 2 字名层截断的同类问题。

    【与 D16 的关系】
    实测两表交集为 0（`dict ∩ D16 = ∅`）——本层只补 D16 没有覆盖的姓名，
    不与 D16 争抢同一字符串，故本层命中即整体脱敏，无截断风险。

    【数据源与准入】
    词表由 `scripts/data/common_name_dict.txt` 提供（Apache-2.0 可再分发），
    准入漏斗见该文件头部注释：姓氏位限 D16 实证姓氏集、名字位限 D16 实证
    字频、T3 追加「尾 2 字 ∈ T2 ∪ D16」证据、剔除全部保护表/豁免表，
    并已过 102 通用语料 + 1200 真实业务语料双零误伤校验（#55 约束④）。
    """
    global _COMMON_NAME_DICT
    if _COMMON_NAME_DICT is not None:
        return _COMMON_NAME_DICT

    cfg = _load_config()
    if not cfg.get("common_name_dict_enabled", False):
        _COMMON_NAME_DICT = set()
        return _COMMON_NAME_DICT

    rel = (cfg.get("common_name_dict_file") or "").strip()
    if not rel:
        _COMMON_NAME_DICT = set()
        return _COMMON_NAME_DICT

    # 路径解析：相对仓库根（scripts/ 的上一级）或 scripts/ 自身
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / rel,
        here / rel,
        Path(rel),
    ]
    path = None
    for p in candidates:
        if p.exists():
            path = p
            break
    if path is None:
        _COMMON_NAME_DICT = set()
        return _COMMON_NAME_DICT

    words: set = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if not w or w.startswith("#"):
                continue
            if len(w) in _COMMON_NAME_DICT_LENS and all("\u4e00" <= c <= "\u9fa5" for c in w):
                words.add(w)
    _COMMON_NAME_DICT = words
    return _COMMON_NAME_DICT


def _find_common_name_dict_candidates(text: str) -> List[Tuple[int, int, str]]:
    """扫描文本，返回通用人名词典层的候选命中区间（未过守卫）。

    【匹配语义：窗口扫描 + 右边界非汉字】
    Issue #55 工程约束③明确要求「**右边界非汉字**」——候选右侧紧邻汉字时
    不算命中，避免「王明生」类串词误命中。

    【姓氏位防截断（#56 同类问题的预防）】
    若候选左侧紧邻的是**姓氏字**，则该候选很可能是一个更长姓名的**后缀**
    （如「艾成军」中的「成军」）。此时放弃该候选——宁可漏脱，也不输出
    「艾XXX」这类残缺姓名。这与 #56 确立的「长名优先」原则同源。

    【长度互斥】
    同一位置先试 3 字、再试 2 字，命中即止——避免 3 字姓名被 2 字候选
    从后往前切开。

    【性能】
    采用**哈希查表 + 逐位扫描**，不使用 alternation 正则：
    117201 条词典编译成单条 alternation 正则实测耗时 6.91s（不可接受），
    而哈希查表 + 扫描在 1200 条真实语料上实测 < 0.05s。
    """
    words = _get_common_name_dict()
    if not words or not text:
        return []

    out: List[Tuple[int, int, str]] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if not ("\u4e00" <= ch <= "\u9fa5"):
            i += 1
            continue
        hit_len = 0
        for length in _COMMON_NAME_DICT_LENS:
            if i + length > n:
                continue
            cand = text[i:i + length]
            if cand not in words:
                continue
            # 左边界：ASCII 字母/数字相邻 → 跳过（编码拼接场景，如 IDATA-张明）
            if i > 0:
                left = text[i - 1]
                if left.isascii() and left.isalnum():
                    break
                # 左邻为姓氏字 → 可能是更长姓名的后缀，放弃（防截断）
                if "\u4e00" <= left <= "\u9fa5" and left in SURNAME_SET:
                    break
                # 左邻 + 候选本身是更长的词典条目 → 交给更长的分支处理
                if (left + cand) in words:
                    break
            # 右边界：紧邻汉字 → 串词，不算命中（#55 约束③）
            if i + length < n:
                right = text[i + length]
                if "\u4e00" <= right <= "\u9fa5":
                    break
            out.append((i, i + length, cand))
            hit_len = length
            break
        # 命中时跳过整段（避免与内部子串重复命中）；未命中则前进一格
        i += hit_len if hit_len else 1
    return out


def _get_biz_words_by_first() -> Dict[str, set]:
    """业务词按首字分组的倒排索引（带缓存）。"""
    global _BIZ_WORDS_BY_FIRST
    if _BIZ_WORDS_BY_FIRST is None:
        by_first: Dict[str, set] = {}
        for w in _get_biz_guard_words():
            by_first.setdefault(w[0], set()).add(w)
        _BIZ_WORDS_BY_FIRST = by_first
    return _BIZ_WORDS_BY_FIRST


_STANDALONE_EXEMPT_WORDS: Optional[set] = None
_STANDALONE_NAME_CANDIDATES: Optional[set] = None


def _get_standalone_exempt_words() -> set:
    """取「通用词豁免表」（config.json → standalone_exempt_words），带缓存。

    【为什么需要这张表（Issue #49 第三轮复评处置口径）】
    通用词「叶子」被姓名规则命中（叶 ∈ 姓池 + 子 ∈ 名字位），但**不能**
    直接放进整词保护表 `common_word_guards`——那会让「叶子涵」也一并豁免，
    而复评裁决是「叶子」不做姓名脱敏、「叶子涵」作为姓名**必须**脱敏。
    故这里引入**真名优先**语义：豁免词默认不脱敏，只有当该命中片段落在
    某个**已知全名**（D16 真名库 / 注入名）之内时才照常脱敏。
        「叶子」        → 非任何已知全名的一部分 → 豁免
        「叶子涵」      → 命中片段落在全名「叶子涵」内 → 不豁免 → 整体脱敏
        「采购的叶子」  → 非任何已知全名的一部分 → 豁免
    """
    global _STANDALONE_EXEMPT_WORDS
    if _STANDALONE_EXEMPT_WORDS is None:
        cfg = _load_config()
        _STANDALONE_EXEMPT_WORDS = {
            w for w in (cfg.get("standalone_exempt_words") or [])
            if isinstance(w, str) and w
        }
    return _STANDALONE_EXEMPT_WORDS


def _get_standalone_name_candidates() -> set:
    """取「以豁免词开头」的已知全名候选集（真名优先判定用），带缓存。

    只有「叶子涵」这类**确认为姓名**、且**包含**豁免词的全名才会进入本集，
    因此集合很小（当前仅 1 条），判定开销可忽略。
    """
    global _STANDALONE_NAME_CANDIDATES
    if _STANDALONE_NAME_CANDIDATES is None:
        words = _get_standalone_exempt_words()
        cfg = _load_config()
        names = {n for n in _get_full_name_list(cfg) if isinstance(n, str)}
        _STANDALONE_NAME_CANDIDATES = {
            n for n in names if any(n.startswith(w) for w in words)
        }
    return _STANDALONE_NAME_CANDIDATES


def _span_covered_by_known_name(text: str, start: int, end: int) -> bool:
    """命中片段是否落在某个已知全名（D16 真名库 / 注入名）的出现区间内。

    与 G4-0「真名优先」同源思路：豁免词不得让已知真名漏脱。
    """
    candidates = _get_standalone_name_candidates()
    if not candidates:
        return False
    pad = max(len(n) for n in candidates)
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    for name in candidates:
        idx = text.find(name, lo)
        while idx >= 0 and idx < hi:
            if idx < end and start < idx + len(name):
                return True
            idx = text.find(name, idx + 1)
    for name in _EXTRA_NAMES:
        if not name:
            continue
        lo2 = max(0, start - len(name))
        hi2 = min(len(text), end + len(name))
        idx = text.find(name, lo2)
        while idx >= 0 and idx < hi2:
            if idx < end and start < idx + len(name):
                return True
            idx = text.find(name, idx + 1)
    return False


def _is_protected_by_standalone_exempt(text: str, start: int, end: int) -> bool:
    """通用词豁免（Issue #49 复评处置口径，真名优先）。

    判定：命中片段与任一通用词豁免项在原文中的出现区间**相交**，
    且该通用词的出现区间**不被**任何已知全名覆盖 → 保留原文。

        「叶子」          → 与豁免词「叶子」相交，且无全名覆盖 → 豁免
        「叶子涵」        → 豁免词区间被全名「叶子涵」覆盖 → 不豁免 → 整体脱敏
        「小叶子」        → 昵称形态命中「小叶」，与「叶子」相交 → 豁免
        「采购的叶子」    → 与「叶子」相交，无全名覆盖 → 豁免

    【为什么用相交而不是"整词等于"】
    昵称形态（小X/老X）命中的是「小叶」而非「叶子」，若按整词判定会漏过；
    相交语义可同时覆盖「叶子」「小叶子」「老叶子」，而「叶子涵」由**真名优先**
    规则挡下（豁免词区间被全名覆盖即不豁免）。
    """
    words = _get_standalone_exempt_words()
    if not words:
        return False
    for w in words:
        idx = text.find(w)
        while idx >= 0:
            ws, we = idx, idx + len(w)
            if ws < end and start < we:          # 命中片段与豁免词区间相交
                if not _span_covered_by_known_name(text, ws, we):
                    return True
            idx = text.find(w, idx + 1)
    return False


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
    # G6 切词前缀/后缀守卫（Issue #51，真实业务语料实测）
    # 姓名规则常把一个常用词切成两半，命中「后半截 + 下一词首字」：
    #     「检索目标」← 检索|目标     「评估对」← 评估|对
    #     「任务一」 ← 任务|一       「管理中」← 管理|中
    # 判定：命中片段的**前缀**（≥2 字）或**后缀**（≥2 字）是排除常用词，
    #       且该常用词与 D16 真名库无包含关系 → 判定为切词误伤，保留原文。
    # 【为什么必须做真名互斥】排除常用词里混着 7 个**真名后缀**
    #     （海洋/创业/恒信/建设/成立/腾飞/黎明 → 吴海洋/文创业/郭恒信/
    #       王建设/吴成立/陈腾飞/丁黎明）。对这些词做前缀/后缀豁免会漏脱真名，
    #     故互斥检查是本守卫的正确性前提，不可省略。
    if len(matched) >= 3:
        for k in range(2, len(matched)):
            prefix = matched[:k]
            suffix = matched[k:]
            if len(prefix) >= 2 and prefix in _SAFE_CUT_WORDS:
                return True
            if len(suffix) >= 2 and suffix in _SAFE_CUT_WORDS:
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
    # §3.4（Issue #53）通道互斥：银行名判定链命中的机构链区间锁定，
    #   地址通道不得再对其内部收拢（工商银行北京分行 → XX银行北京分行）。
    locked_spans = _bank_chain_locked_spans(text)

    for m in _ADDRESS_SUFFIX_RE.finditer(text):
        s_start, s_end = m.span()
        if s_start < consumed_until:
            continue  # 落在已替换区间内（如"XX省"中的字），跳过
        suf = m.group(0)

        # §3.4（Issue #53）通道互斥断言：银行名判定链命中的机构链
        #   （「XX银行北京分行」）已锁定 → 地址通道不得再切其内部。
        if locked_spans and _is_in_locked_span(locked_spans, s_start, s_end):
            continue

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

        # G5 地址前缀守卫（Issue #51，真实业务语料实测）：
        # 「编排链路正确性」被回溯成「XX路正确性」——地址通道把「链」当作
        # 「XX路」的地名前缀（链+路）。这类误伤与姓名通道同源：前缀是
        # **业务术语**而非地名成分。
        # 复用 G4 整词保护表（common_word_guards）：前缀与表内词重叠 → 保留原文。
        # 同一张表同时服务姓名通道与地址通道，避免维护两份词表。
        if prefix and _is_protected_by_common_word_guard(text, i, i + len(prefix)):
            continue

        # G4-c 重叠豁免（Issue #51）：前缀 + 行政后缀构成的完整片段与整词
        # 保护表**相交**即豁免。
        # 「含编排链路」+「路」：保护词「编排链路」与命中片段相交 → 业务术语
        # 「识别灰」+「区」：保护词「灰区」与命中片段相交 → 业务术语
        # 真名安全：保护表与 D16 全名库 / 金标双向互斥，相交不可能是真名。
        if prefix and _is_overlapped_by_common_word_guard(text, i, s_end):
            continue

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
        # D-11 补强（Issue #63 批次四）：机构链口径——类型后缀链原样保留，
        #   脱敏只作用于「地名性前缀」。
        #   「区」例外：可跨后缀场景下的「区」多为片区名的一部分
        #   （「自贸区分行」「自贸区分行营业部」），与前缀同属一个地名单元，
        #   随前缀一并遮蔽（标准版口径为 XX分行 / XX分行营业部，
        #   实测语料中不存在「XX区分行」形态）；省/市/县属行政层级，
        #   保留可见（XX县支行）。
        admin_visible = "" if crossed_admin == "区" else crossed_admin
        out.append("XX" + admin_visible + suf)
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

def _get_bank_names(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """返回银行名称目标清单（内联 + 外置文件合并）。

    【为什么需要外置】
    `config.json` 的 `bank_names` 是**待脱敏的目标银行名单**（不是检测词库，
    命中的会被遮盖）。仓库里内联的条目来自历史项目的实际客户样本，
    对内网分发而言属于「把某家客户的机构口径带给别人」，不合适。

    因此支持 `bank_names_file`：指向一个**每行一个银行名**的外部文本文件，
    内网部署方填入本行及需要覆盖的目标机构即可。
    合并语义：内联 ∪ 外置（外置优先，去重保序）；外置缺失时静默回退内联，
    保证既有行为完全不变。

    环境变量 `AIKIT_BANK_NAMES` 优先级最高，与 `AIKIT_FULL_NAME_LIST` 同构。
    """
    if cfg is None:
        cfg = _load_config()

    names: List[str] = []
    seen = set()

    for b in (cfg.get("bank_names") or []):
        if isinstance(b, str) and b and b not in seen:
            seen.add(b)
            names.append(b)

    rel = str(cfg.get("bank_names_file") or "").strip()
    env_file = os.environ.get("AIKIT_BANK_NAMES", "").strip()
    if env_file:
        rel = env_file
    if rel:
        for p in _resolve_data_paths(rel):
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for line in text.splitlines():
                b = line.strip()
                if b and not b.startswith("#") and b not in seen:
                    seen.add(b)
                    names.append(b)
            break

    return names


def _build_patterns() -> List[Tuple[re.Pattern, str]]:
    global _PATTERNS_PRE, _PATTERNS_NAME
    cfg = _load_config()
    rep = cfg.get("replacement", {})
    bank_names: List[str] = _get_bank_names(cfg)
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
    #
    # G10 分节编号 / 分数守卫（Issue #51，真实业务语料实测）：
    #   「项目总体管理（1/2）」「结果：10/10 全部通过 ✅」被当作 M/D 日期脱敏。
    #   这两类形态有明确标识：
    #     ① 分子 == 分母          → 分数（10/10）
    #     ② 被全角括号包裹        → 分节编号（（1/2）、（2/2））
    #   中文文档的日期几乎不写成裸 M/D（惯例为「8月2日」或「2026年8月2日」），
    #   故这两种形态判为非日期，保留原文。
    _short_slash_re = re.compile(
        r'(?<![0-9/.])(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12][0-9]|3[01])(?![0-9/])')

    def _short_slash_repl(m: re.Match) -> str:
        whole = m.group(0)
        # ① 分子 == 分母 → 分数
        if whole.split("/")[0].lstrip("0") == whole.split("/")[1].lstrip("0"):
            return whole
        # ② 全角括号包裹 → 分节编号
        lo = max(0, m.start() - 1)
        hi = min(len(m.string), m.end() + 1)
        if m.string[lo:m.start()] == "（" and m.string[m.end():hi] == "）":
            return whole
        return _date_short_slash

    patterns.append((
        _short_slash_re,
        _short_slash_repl
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
        # D-12（Issue #74）：配置化银行名单（专名）集合——
        #   前缀豁免（D-12）不得让这些专名漏脱。
        _BANK_CONFIG_NAMES = {b for b in bank_names if b}
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
            # §3.3（Issue #53）：品牌地名处在「机构链」中时必须让位给地址通道，
            #   否则机构链被从中间切开（深圳高新北支行网点 → 深圳XX高新北支行网点）。
            #   让位后由地址通道整体收拢 → XX支行网点（保留机构类型后缀）。
            if _in_institution_chain(m.string, m.end()):
                return m.group(0)
            # D-11（Issue #63 验收项）：地名整词保护表命中 → 保留原文。
            #   「长江」作为「江苏长江商业银行」的品牌词被本规则改写为「长江XX」，
            #   但在普通词「长江大桥」中它只是地名通名，改写会造成
            #   「长XXX桥」这类误伤（既有缺陷，本次一并修复）。
            if _is_protected_by_place_guard(m.string, m.start(), m.end()):
                return m.group(0)
            # D-12（Issue #74，通用规则）：G4 整词保护表命中品牌词 → 保留原文。
            #   「身份认证平台厦门银联接入三方测试环境」中的「厦门」是
            #   「厦门银联」联合体品牌的前缀，被品牌规则切开成「厦XXX联」，
            #   属于业务术语被误遮。G4 采用**重叠**语义（与地址通道 G4-c 一致）：
            #   保护词「厦门银联」与品牌片段「厦门」相交即判定为业务术语。
            #   真名安全：G4 表入库时已与 D16 全名库 / 金标双向互斥。
            if _is_overlapped_by_common_word_guard(m.string, m.start(), m.end()):
                return m.group(0)
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

        # ---- G8（Issue #51，真实业务语料实测）：银行名「业务修饰词」守卫
        #
        # 问题：贪婪模式 [汉字]{2,6}银行 会把**业务修饰语**当成银行名的一部分：
        #   "资深银行大型项目管理员" → 脱成 "XX银行大型项目管理员"（"资深"被吞）
        #   "检索目标银行同业实践"     → "XX银行同业实践"（"检索目标"被吞）
        #   "须符合银行监管要求"       → "XX银行监管要求"（"须符合"被吞）
        #   "结合某股份制银行项目"     → "XX银行项目"（"结合某股份制"被吞）
        # 根因：银行名的合法前缀是**地名/字号**（宁波/郑州/中原/海峡），
        #   而「资深」「检索目标」「评估对」「须符合」「制定」「凭借在」等是
        #   业务修饰语，出现在银行名前时说明这个「银行」是**泛称**而非专名。
        # 「股份制银行」本身即泛称（不是某家具体银行），一并保留。
        _BANK_MODIFIER_WORDS = {
            # 业务动作/状态修饰
            "资深", "检索", "评估", "符合", "制定", "支撑", "凭借",
            "识别", "移入", "面向", "涉及", "服务", "配套", "实施",
            "运营", "管理", "监管", "风控", "咨询", "顾问", "专业",
            # 名词性修饰
            "目标", "股份制", "综合", "业务", "系统", "核心", "数字",
            "智慧", "手机", "电子", "网上", "直销", "个人", "远程",
            "微信", "视频", "电话", "大型", "小型", "中型", "国有",
            "地方", "民营", "外资", "区域", "跨域", "跨行", "本行",
            "他行", "全行", "重点", "战略",
        }
        _BANK_MODIFIER_RE = re.compile(
            "(?:" + "|".join(re.escape(w) for w in
                             sorted(_BANK_MODIFIER_WORDS, key=len, reverse=True)) + ")"
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
            # G8（Issue #51，真实业务语料实测）：业务修饰词守卫
            # 银行名的合法前缀是**地名/字号**（宁波/郑州/中原/海峡），
            # 而「资深」「检索目标」「评估对」「须符合」「制定」「凭借在」等是
            # 业务修饰语，出现在银行名前说明这个「银行」是**泛称**而非专名。
            if _BANK_MODIFIER_RE.search(text[:-2]):
                return True
            return False

        # D-12（Issue #74，通用规则）：银行名前缀与 G4 整词保护表相交 → 保留原文
        #
        # 银行名的合法前缀是**地名/字号**（宁波/郑州/中原/海峡），
        # 而「发起开立」「自助」「模拟活期」等是**业务修饰语/泛称**，
        # 出现在「银行」前说明这个「银行」是泛称（自助银行、其中银行卡），
        # 不应被兜底规则 `[汉字]{2,6}银行` 连同前缀整体吞掉。
        #   "配合发起开立银行承兑汇票的演练" → 旧："配合XX银行承兑汇票的演练"
        #   "改造39A04自助银行ATM"           → 旧："改造39A04XX银行ATM"
        # 判定采用**重叠**语义（与地址通道 G4-c 一致）：保护词出现在前缀区间内即豁免。
        # 真名安全：G4 表入库时已与 D16 全名库 / #55 词典 / 金标双向互斥；
        #           配置化银行名单（bank_names）为**专名**，显式豁免本规则。
        def _bank_prefix_guard_hit(text: str, start: int, matched: str) -> bool:
            words = _get_common_word_guards()
            if not words:
                return False
            prefix_len = len(matched) - 2  # 去掉尾部「银行」
            if prefix_len <= 0:
                return False
            end = start + prefix_len
            for w in words:
                if len(w) < 2:
                    continue
                idx = text.find(w, max(0, start - 2))
                while idx >= 0:
                    if idx >= end:
                        break
                    ws, we = idx, idx + len(w)
                    if ws < end and start < we:
                        return True
                    idx = text.find(w, idx + 1)
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
            # 1b. D-12（Issue #74）：配置化银行名单为专名，不受前缀豁免影响
            if matched in _BANK_CONFIG_NAMES:
                return rep.get("BANK", "XX银行")
            # 1c. D-12（Issue #74）：前缀命中 G4 整词保护表 → 整体保留
            if _bank_prefix_guard_hit(m.string, m.start(), matched):
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
        # §3.4（Issue #53）通道互斥断言：机构链若锚定在**已脱敏的银行名**之后
        #   （「XX银行深圳分行」），不得再被「城市 + 机构后缀」规则单独收拢——
        #   否则银行链被拆成两段（上游期望「XX银行深圳分行」保留分行层级）。
        if full.endswith(("分行", "支行", "营业部")) and _masked_bank_before(m.string, m.start()):
            return m.group(0)
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
    #
    # ---- D-4 整改（Issue #61-B）：组织名通道接入 L1/L4 守卫，守卫语义拆分
    #   整改前本通道只用 G4-S「前缀成词整体保留」，语义含混：
    #     · 「科技部」独立出现 → 脱敏为 XXXX（通用机构词误伤）；
    #     · 「信息科技部」→ 命中的是「科技部」，因前缀成词而**整词保留**
    #       （机构实体漏脱）。
    #   两种表现出自同一条规则的两种副作用，必须按语义拆成两张表：
    #     · `generic_words`（独立通用词）：本身即通用机构通名，**保留**
    #       ——口径与人工标准版一致（实测标准版保留「科技部/作战部/运营中心/
    #       运营部/营运部/管理部/营运管理部/信息技术部/财务部」等）；
    #     · `composite_words`（前缀成词）：「通名 + 组织后缀」构成的机构
    #       实体，**整段脱敏**——杜绝「信息XXXX」这类混合态。
    _org_rules = cfg.get("org_name_rules") or {}
    _org_generic_words = set(w for w in (_org_rules.get("generic_words") or []))
    _org_composite_words = set(w for w in (_org_rules.get("composite_words") or []))
    if org_suffixes:
        suffix_alt = '|'.join(re.escape(s) for s in org_suffixes)
        # D-11 补强（Issue #63 批次四）：「营业部」不再整词替换为 XXXX。
        #   人工标准版口径：机构链 = [地名性前缀] + [机构类型后缀链]，
        #   脱敏只作用于前缀，类型后缀链原样保留——
        #     「自贸区分行营业部」→ XX分行营业部、「总行营业部」→ 总行营业部、
        #     「鼓楼支行营业部」→ XX支行营业部。
        #   若在此整词替换「营业部」，会得到「总行XXXX / XX区分行XXXX」等
        #   混合残态（G3 文本级 over 的最大单一来源，47 处）。
        #   注：「营业部」若独立出现（前后均非汉字）仍由 suffix_alt 分支兜底，
        #   而 org_suffixes 不含「营业部」，故独立形态不再被本通道吞掉——
        #   实测源语料中「营业部」从不独立出现。
        _ORG_NAME_RE = re.compile(
            rf'(?:科技部|运营部|管理部|董事会|监事会|管委会|事业部)|'
            rf'(?<![一-龥])(?:{suffix_alt})(?![一-龥])|'
            rf'(?<![一-龥])[一-龥]{{2}}(?:部|科|中心|管委会|办公室)(?![一-龥])'
        )

        # 前缀成词表 + D-7 机构实体白名单：先于通用组织名规则执行，整段替换
        # （不产生残尾）
        #   · `composite_words`（D-4 前缀成词）：整段脱敏（Issue #61 §四 验收 1）；
        #   · `entity_whitelist`（D-7 业务侧机构实体白名单）：整段脱敏——
        #     即 #64 §四 验收第 1 条所要求的「营运管理部」类机构实体口径。
        _org_entity_words = set(_org_composite_words)
        _d7_layer = _org_rules.get("org_entity_layer") or {}
        if _d7_layer.get("enable"):
            _org_entity_words |= {
                w for w in (_d7_layer.get("entity_whitelist") or []) if w}
        if _org_entity_words:
            _composite_alt = '|'.join(
                re.escape(w) for w in sorted(_org_entity_words, key=len, reverse=True))
            _ORG_COMPOSITE_RE = re.compile(rf'(?:{_composite_alt})')

            def _org_composite_repl(m):
                """前缀成词 / 白名单机构实体：整段脱敏（Issue #61 §四 验收 1、
                Issue #64 §四 验收 1）。"""
                return rep.get("ORG", "XXXX")

            patterns.append((_ORG_COMPOSITE_RE, _org_composite_repl))

        def _org_name_repl(m):
            """组织名替换 + 守卫语义拆分（D-4：Issue #61-B + Issue #53 §3.2）。

            【独立通用词 → 保留】
              命中片段本身就是通用机构通名（「科技部」「作战部」「运营中心」…）
              时保留原文——它们不含可定位到具体机构的信息，
              且与人工标准版口径一致（实测标准版全部保留这些词）。

            【前缀成词 → 整段脱敏】
              「信息科技部」这类「通名 + 组织后缀」的机构实体由
              `_ORG_COMPOSITE_RE` 先行整段替换，不留「信息XXXX」残尾。

            【G4-S 兜底】
              其余前缀成词情形（如保护表中的更长词）仍按 Issue #53 §3.2
              整词保留。

            【D-7 机构实体层（Issue #64）】
              命中片段若落在某个「通名 + 后缀」组合式机构实体上
              （如「零售部」「贸金部」「计划财务部」），一律**保留**：
                · 「零售部」这类通用部门名在人工标准版中一律保留，
                  整段遮盖属过度脱敏（G3 文本级 over 的机构类来源）；
                · 「计划财务部」若被切成「计划财务 + 管理部」，
                  则产生「混合残态」，与 D-7 的立意相反。
            """
            frag = m.group(0)
            if frag in _org_generic_words:
                return frag
            if _is_org_layer_combo_span(m.string, m.start(), m.end()):
                return frag
            if _is_protected_by_common_word_guard(m.string, m.start(), m.end(),
                                                  strict_longer=True):
                return frag
            return rep.get("ORG", "XXXX")

        patterns.append((_ORG_NAME_RE, _org_name_repl))


# ---------- 13. 人员姓名（2-3个汉字，粗筛） ----------
    # ↓↓↓ 姓名规则从本行开始单独归组：apply_redactions 中地址通道在其之前执行 ↓↓↓
    _pre_pattern_count = len(patterns)

    # -----------------------------------------------------------------------
    # D-4 补丁（Issue #61-B）：通用机构词保留后，紧邻的人名仍须脱敏
    # -----------------------------------------------------------------------
    # 【问题】D-4 把「运营部」这类通用机构词改为**保留原文**后，
    # 姓名通道的左边界（「前一字不得为汉字」）不再成立：
    #     "运营部韩慧丽向部门领导汇报"
    # 整改前 "运营部" 被脱敏成 XXXX（ASCII），边界成立 → 韩慧丽被脱敏；
    # 整改后 "运营部" 保留 → 边界失效 → **韩慧丽漏脱**（金标回归）。
    #
    # 【解法】把「保留的通用机构词」当作**边界豁免点**：
    # 其后紧跟的 2-3 字若具备**强**姓名证据（D16 精确全名 / 通用人名词典），
    # 则整段脱敏。只用强证据而不用字频路径，避免把
    # 「运营部配合完成」这类业务短语误判为人名。
    if _org_generic_words:
        _org_lb = "|".join(
            f"(?<={re.escape(w)})"
            for w in sorted(_org_generic_words, key=len, reverse=True))
        _ORG_GEN_NAME_RE = re.compile(
            rf'(?:{_org_lb})(?P<name>[\u4e00-\u9fa5]{{2,3}})')

    # D16（陈黎 Issue #45，2026-10）：精确全名匹配（7398人名优先于字符池规则）
    # - 从 config.json full_name_list 加载 7398 个已知全名
    # - 长名优先排序（防止"欧阳"截断"欧阳娜娜"）
    # - 边界：仅排除 ASCII/数字（允许中文连接符如"和/在/是/等"）
    # - 执行顺序：精确匹配 → 字符池规则
    #
    # 【必须走 _get_full_name_list() 而非 cfg.get("full_name_list")】
    #   外置花名册（full_name_list_file / AIKIT_FULL_NAME_LIST）是内网投产时
    #   **精确匹配层的唯一名单来源**（分发包已剥离内联人名库）。
    #   若此处只读内联列表，外置花名册在 D16 直配层完全失效 ——
    #   实测「袁伟杰」既不进 D16 正则、也匹配不到，等于花名册形同虚设。
    #   _get_full_name_list() 返回内联 ∪ extra_name_list ∪ 外置 的合并结果，
    #   语义向后兼容（含 Issue #60 的 D-5 补充名单）。
    _full_name_list: List[str] = _get_full_name_list(cfg)
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
    #
    # D16-P 占位名层（Issue #51，真实业务语料实测）：
    #   真实业务文档里存在**占位人名**「张三」「李四」「王五」「赵六」等，
    #   它们在组织架构图/角色分工表里承担真实人名的语义位
    #   （「主任：张三（CIO）」「负责人：李四（总经理）」），
    #   因此**必须脱敏**（否则占位名泄漏即真名位泄漏）。
    #   但它们不在 D16 全名库（7398 条真实人名）里，通用姓名规则也不覆盖
    #   （「三」「四」不在名字用字池的可用闸门内），导致整体漏脱。
    #
    #   实现要点：**独立的占位名表**（config.json → placeholder_names），
    #   不混入 full_name_list——混入会让「张三」被 2 字名层优先匹配，
    #   把真名「张三丰」截断成「XXX丰」（实测降级）。
    #   占位名层要求**右边界非汉字**（防止截断更长真名），
    #   左边界宽松（允许「组长张三」这类前置角色词）。
    _placeholder_names: List[str] = cfg.get("placeholder_names") or []

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
        # -------------------------------------------------------------------
        # Issue #56：层序改为「长名层优先」（2026-11-03）
        # -------------------------------------------------------------------
        #
        # 【缺陷现象】
        # 2 字层原在前，导致**库内确有更长全名**时被 2 字名抢先命中、输出残缺：
        #   「郑安琪」→ 「郑XXX」（而「郑安琪」就在 D16 库里）
        #   「孙安琪」→ 「孙XXX」
        # 实测 D16 库内共 **33 个 2 字名是更长库内名的后缀**（如 安琪←郑安琪、
        # 成文←杨成文、明辉←张明辉、李军←郭李军…），这 33 个二字名全部存在该截断。
        #
        # 【根因】
        # 两层是**独立正则 + 顺序 sub**（不是单层 alternation），
        # 因此后执行的层无法回收已被前一层替换掉的片段——
        # 2 字层先执行时，「郑安琪」先被切成「郑」+「安琪」，
        # 3 字层再执行时文本里已无「郑安琪」可匹配。
        #
        # 【修复：长名层先执行】
        # 两层各自**保留原有边界语义**（2 字层左边界严格 / 3 字层左边界放宽，
        # 见 #49 P1-I 的实测依据），只调换执行顺序 →
        # 「郑安琪」由 3 字层整体命中 → 「XXX」，
        # 而「组余翔」「宋广举」类（2 字层严格左边界的守卫场景）行为不变。
        #
        # 【实测影响（2026-11-03，完整生产管线）】
        #   102 通用语料改动 0 → 0（零变化）
        #   1200 真实业务语料改动 0 → 0（零变化）
        #   金标命中 260/260 → 260/260（零变化）
        #   定向修复：33 个被覆盖二字名对应的更长全名不再被截断
        #
        # 【为什么不用「单层 alternation + 长度降序」】
        # 实测该方案会把 3 字层的宽松左边界也套到 2 字名上
        # （原 2 字层左边界 `(?<![a-zA-Z0-9])` 严格、3 字层 `(?<![\u4e00-\u9fa5])` 宽松，
        # 两者语义不同、不可合并），实测会丢掉
        # 「FB-APPSVR卫海啸」「研发组11张海翔」等 **ASCII/数字前缀** 的召回 ——
        # 那正是 #49 P1-I 分层要保住的能力，故不采用。
        if _long_names:
            # 3 字及以上：左边界允许 ASCII/数字/标点，只排除紧跟汉字
            _alt = "|".join(re.escape(n) for n in sorted(_long_names, key=len, reverse=True))
            _d16_layers.append(re.compile(rf'(?<![\u4e00-\u9fa5])(?:{_alt})(?![a-zA-Z])'))
        if _short_names:
            # 2 字名：左边界严格（排除 ASCII/数字在前），右边界允许数字/中文跟随
            _alt = "|".join(re.escape(n) for n in sorted(_short_names, key=len, reverse=True))
            _d16_layers.append(re.compile(rf'(?<![a-zA-Z0-9])(?:{_alt})(?![a-zA-Z])'))
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

    # S1b 复姓精确层：2字复姓 + 1~3个名字汉字（防止复姓第二字被单姓规则误匹配）
    # 复姓本身是强信号（双字姓氏在现代汉语里几乎不与常用词碰撞），
    # 故复姓通道使用**全池**名字位，不叠字频闸门。
    #
    # 【Issue #50 §4.8 规则A 退出门槛】本层已从规则A 家族拆出，
    # 独立受 `compound_surname_enabled` 控制：
    #   - 规则A（单姓笛卡尔积）退出后，本层是唯一仍能捞「名单外复姓人名」
    #     的机制（实测捞回 `欧阳娜娜` / `司马光`）；
    #   - 实测在 102 条通用语料 + 1200 条真实业务语料上**零误伤**。
    _rule_a_enabled = _is_rule_a_enabled(cfg)
    compound_surnames = cfg.get("compound_surnames") or list(_NAME_GATE_COMPOUND_SURNAMES)
    if compound_surnames and _is_compound_surname_enabled(cfg):
        compound_alt = '|'.join(re.escape(s) for s in sorted(compound_surnames, key=len, reverse=True))
        patterns.append((
            re.compile(
                rf'(?<![a-zA-Z0-9])'
                rf'(?:{compound_alt}){name_char_class}{{1,3}}'
            ),
            rep.get("NAME", "XXX")
        ))

    if _name_rule_mode == "graded" and _rule_a_enabled:
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
        # 高危姓为空时（如未配置 high_risk_surnames，或闸门后无交集），
        # _high_alt 必须有兜底值——否则后续 R3b 通道引用它会抛
        # UnboundLocalError，整条规则链崩溃（历史缺陷，基线提交同样存在）。
        _high_alt = '|'.join(re.escape(s) for s in _high_surnames)
        if _high_surnames:
            _high_pos = _build_name_pos_class(_hot_chars, _warm_chars, _hi_min, _hi_max)
            patterns.append((
                re.compile(
                    rf'(?<![a-zA-Z0-9])'
                    rf'(?:{_high_alt}){_high_pos}'
                ),
                rep.get("NAME", "XXX")
            ))
    elif _name_rule_mode == "graded":
        # 【Issue #50 §4.8 规则A 退出】R2 / R3 已退出主链路，
        # 但 **R3b 保留**——它不是笛卡尔积粗筛，而是**有右边界证据**的通道：
        #   「高危姓 + 单字名」后面必须紧跟职位/职责类词
        #   （负责/牵头/副组长/组长/主任/经理/主管/专员/工程师/负责人/…）。
        # 「樊霖负责」「樊霖副组长」中，「负责/副组长」证明前面的两字是
        # 「人名 + 职务」组合，而非一个双字常用词。
        #
        # 实测：通用语料 102 条 **0 命中**、真实业务语料 1200 条 **0 命中**；
        # 能捞回规则A 退出后的库外真名（「樊霖」不在 D16 库中）。
        #
        # 为什么不能整体放宽高危姓的单字名：全局放宽会命中 11 处
        # （宁波/方向/孔明/安庆/孟子/孔子/庄子/石家庄…）——
        # 收紧的**唯一依据就是右边界**，有右边界时误伤面为零。
        _gated_surnames = _get_gated_surnames(cfg)
        _high_risk = set(cfg.get("high_risk_surnames") or "")
        _high_surnames = sorted(s for s in _gated_surnames if s in _high_risk)
        _high_alt = '|'.join(re.escape(s) for s in _high_surnames)
        _role_suffixes = [
            '负责', '牵头', '副组长', '组长', '主任', '经理', '主管', '专员',
            '工程师', '负责人', '联系人', '成员', '参会', '出席', '签名', '签字',
        ]
        _role_suffix_alt = '|'.join(re.escape(s) for s in _role_suffixes)
        _single_pos = _build_name_pos_class(_hot_chars, _warm_chars, 1, 1)
        # 无高危姓时不追加本通道——否则空姓氏组会退化成「任意单字 + 职务后缀」，
        # 造成大面积误伤（守卫：必须有姓氏可选）。
        if _high_surnames:
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
        # 【Issue #50 §4.8】同属规则A 家族，受 `rule_a_enabled` 开关控制。
        if _rule_a_enabled:
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

    # 规则C：姓氏 + 名字汉字（左侧必须是全角冒号，解决"组长：樊霖"型漏检）
    #
    # 【D-1 整改（Issue #57）】恢复 `{1,3}` 贪婪量词：
    #   整改前本规则只吃「姓 + 1 个名字汉字」，导致全角冒号场景的**三字名**
    #   被截断成「XXX + 尾字」（"负责人：蔡昀煜" → "负责人：XXX煜"）——
    #   残留的尾字能与人名库反查，等同未脱敏（隐蔽型合规事故）。
    #   半角冒号规则C-1 与介词规则D 一直用 `{1,3}`，三条并列规则口径必须一致。
    #
    # 【贪婪后如何不吞职务词】
    #   贪婪会把职务词一起吞进来（"组长：樊霖副组长" → 吞成 "樊霖副组"）。
    #   正确解法不是"永久限制 1 字"，也不是"回退固定 1 字"
    #   （回退 1 字会把职务词拦腰截断成 "XXX副组"），
    #   而是**按职务词起点剥离**：命中片段内若出现职务词，
    #   从该职务词的**起点**切开，姓名部分整段遮盖、职务词整体保留。
    #
    # 【为什么职务词通常根本吞不进来】
    #   名字用字池不含「副/组/主/管/骨」等职务构词字，
    #   贪婪匹配天然停在这些字之前；剥离逻辑是**兜底**
    #   （例如「行长」中的「长」在池内，贪婪会多吃一个字）。
    patterns.append((
        re.compile(
            rf'(?<=：)(?:{surname_alt}){name_char_class}{{1,3}}'
        ),
        _make_colon_name_repl(rep.get("NAME", "XXX"))
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

    # D16-P 占位名层（Issue #51，真实业务语料实测）——置于姓名规则组**最末**
    #
    # 【背景】真实业务文档里存在**占位人名**「张三」「李四」「王五」「赵六」，
    #   它们在组织架构图/角色分工表里承担真实人名的语义位：
    #     「主任：张三（CIO）」「负责人：李四（总经理）」
    #   属于**必须脱敏**的语义位（占位名不脱 = 人名位泄漏）。
    #   但它们不在 D16 全名库（7398 条真实人名）里，
    #   通用姓名规则也不覆盖（「三」「四」不在名字用字池的可用闸门内）→ 整体漏脱。
    #
    # 【为什么放在最末而不是混入 D16 库】
    #   混入 full_name_list 会让「张三」被 **2 字名层**优先匹配，
    #   把真名「张三丰」截断成「XXX丰」（实测降级，已验证）。
    #   放在所有通用规则之后：通用规则先把「张三丰」整体脱敏为 XXX，
    #   残留的裸「张三」才由本层兜底 —— 两者互不干扰。
    #
    # 【右边界】允许紧跟汉字：「张三负责建设」中的「张三」是姓名位，须脱敏。
    #   为何不怕截断更长真名「张三丰」？因为本层置于**所有通用规则之后**——
    #   通用规则若能识别「张三丰」，已先把它整体替换为 XXX，本层根本遇不到；
    #   若通用规则识别不了，本层脱敏「张三」也仍是脱敏（不产生泄漏），
    #   只是粒度上由「张三丰」降级为「张三」+ 残留一字。
    #   仅排除 ASCII 字母/数字相邻（避免吃掉 IDATA-张三 这类编码拼接）。
    if _placeholder_names:
        _ph_alt = "|".join(re.escape(n) for n in sorted(_placeholder_names, key=len, reverse=True))
        patterns.append((
            re.compile(rf'(?<![A-Za-z0-9])(?:{_ph_alt})(?![A-Za-z0-9])'),
            rep.get("NAME", "XXX")
        ))


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
    # 【关键修复（批次二 G7 回归暴露）】
    # 姓氏用 `|` 拼接时**必须**加非捕获组 `(?:...)`：
    #   否则正则解析为 `(丁|万|...|刘[名用字]{1,3})`——
    #   名字用字符类只挂在**最后一个**姓氏分支上，
    #   其余姓氏可「零名用字」单字命中（"位及时" → 位 + 及 + 时）。
    # 加组后语义回归设计意图：「姓氏字 + 1~3 名用字」两侧各一。
    surname_alt = "(?:" + "|".join(re.escape(c) for c in sp) + ")"
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


# ---------------------------------------------------------------------------
# 证据分层：L1 姓名证据层 / L2 职务标签层（2026-11-08 统一整改方案 §三）
#
# 【为什么要分层】
# 整改前，槽位类通道（锚点扩散 / 顿号名单）只有**形态判据**：
# 「2-4 字 CJK 块」「整行 CJK + 顿号」——凡形态像名字就脱敏。
# 结果是「内部资料，禁止外传」这类并列短语被整段抹除、
# 「各条线副行长」这类职务标签被当人名切走（Issue #58 / #59）。
#
# 【分层后的判定方向】
#   L1 姓名证据层：**正向证据**（D16 全名 / 通用人名词典 / 姓氏+名用字+字频）
#                    ——「这一块确实是人名」的证据；
#   L2 职务标签层：**反向判据**（职务词后缀 / 通名前缀+职务后缀的组合式声明）
#                    ——「这一块是职务标签」的证据；
#   两者皆无 → **不可判定 → 保留**（缺省方向不对称：结构化槽位 strict，
#              非结构化正文 loose——见整改方案 §三）。
#
# 【与 Issue #57 的分工】
#   #57 解决「**确定是名字却切错了**」（精确匹配问题 → 贪婪 + 职务词剥离）；
#   #58/#59 解决「**不确定是不是名字**」（保守判定问题 → 无证据不脱）。
#   两条的判定依据不同、落点不同，互不覆盖。
# ---------------------------------------------------------------------------

_ROLE_LABEL_PATTERNS: Optional[Dict[str, Any]] = None
_ROLE_SUFFIX_SORTED: Optional[List[str]] = None


def _get_role_label_patterns() -> Dict[str, Any]:
    """L2 职务标签层配置（config.json → role_label_patterns），带缓存。

    【组合式声明】配置里给出的是「职务后缀 suffixes」+「通名前缀
    prefix_forms」两张表，判定时按「prefix_forms + suffixes → 整体职务标签」
    的组合规则合成，而不是逐词列举——这才跟得上「各条线副行长」这类
    「通名 + 职务」的组合（Issue #59 根因 1：枚举法追不上组合爆炸）。
    """
    global _ROLE_LABEL_PATTERNS
    if _ROLE_LABEL_PATTERNS is None:
        cfg = _load_config()
        raw = cfg.get("role_label_patterns") or {}
        suffixes = [str(w).strip() for w in (raw.get("suffixes") or []) if str(w).strip()]
        prefixes = [str(w).strip() for w in (raw.get("prefix_forms") or []) if str(w).strip()]
        _ROLE_LABEL_PATTERNS = {"suffixes": suffixes, "prefix_forms": prefixes}
    return _ROLE_LABEL_PATTERNS


def _get_role_suffixes_sorted() -> List[str]:
    """职务后缀词表（按长度降序，保证最长匹配优先）。"""
    global _ROLE_SUFFIX_SORTED
    if _ROLE_SUFFIX_SORTED is None:
        _ROLE_SUFFIX_SORTED = sorted(
            _get_role_label_patterns()["suffixes"], key=len, reverse=True)
    return _ROLE_SUFFIX_SORTED


def _find_role_start(word: str, limit: Optional[int] = None) -> Optional[int]:
    """在 `word` 中定位**职务词起点**（最长匹配优先，同起点取最长词）。

    返回起点下标；未命中返回 None。
    `limit` 限定起点必须 < limit（用于「职务词起点必须落在贪婪匹配片段内部」
    的判定）。
    """
    if not word:
        return None
    best: Optional[int] = None
    for w in _get_role_suffixes_sorted():
        idx = word.find(w)
        if idx < 0:
            continue
        if limit is not None and idx >= limit:
            continue
        if best is None or idx < best:
            best = idx
            if best == 0:
                break
    return best


def _has_name_evidence(word: str) -> bool:
    """L1 姓名证据层：判断 `word` 是否具备「人名」的正向证据。

    判据（四者之一即可，见整改方案 §三 L1）：
      ① 该块 ∈ D16 精确全名表（config.full_name_list）；
      ② 该块 ∈ 通用人名词典层（Issue #55 落地的那张表）；
      ③ 该块首字 ∈ 实证姓氏集，且其余字 ∈ 名字用字池（name_pool），
         且**尾字**字频 ∈ hot ∪ warm（D16 自举字频）；
      ④ 反向护栏：命中业务词表 / 整词保护表的词**不算**人名证据。

    【为什么尾字要过字频闸门】
    「方案设计」「金融同业」这类词首字也可能是姓、其余字也在名字池里，
    只有尾字字频能把它们挡在门外（实测：朴素判据误判 2/28，
    叠加字频闸门与护栏表后归零）。

    【为什么不要求首字字频】
    高危姓/低危姓的门槛已在规则组内部处理；这里只做「像不像人名」的
    粗判，避免把「王根利」这类尾字字频偏低的真名误杀。
    """
    if not word or not (2 <= len(word) <= 4):
        return False
    if not all("\u4e00" <= c <= "\u9fa5" for c in word):
        return False
    # ④ 护栏：业务词 / 整词保护表命中 → 不是人名
    if word in _get_biz_guard_words():
        return False
    if word in _get_common_word_guards():
        return False
    # 通用词豁免表（#49 裁决：独立出现时豁免，如「叶子」）同样不构成人名证据
    if word in _get_standalone_exempt_words():
        return False
    # ① D16 精确全名
    if word in get_full_name_set():
        return True
    # ② 通用人名词典
    if word in _get_common_name_dict():
        return True
    # ③ 姓氏 + 名用字 + 尾字字频
    if word[0] not in SURNAME_SET:
        return False
    rest = word[1:]
    if not all(c in _get_name_pool_set() for c in rest):
        return False
    tail = rest[-1]
    cfg = _load_config()
    return tail in _get_gated_name_chars(cfg) or tail in _get_warm_name_chars(cfg)


def _is_role_label(word: str) -> bool:
    """L2 职务标签层：判断 `word` 是否是职务标签（含组合式）。

    判据：
      ① 以职务后缀结尾（副行长 / 总经理 / 骨干 / 总监 …）；
      ② 本身就是一个通名前缀（各业务部门 / 外部专家 …）；
      ③ 「通名前缀 + 职务后缀」的组合——由 ① 自动覆盖，此处仅作说明。
    """
    if not word:
        return False
    pat = _get_role_label_patterns()
    if word in pat["prefix_forms"]:
        return True
    return _find_role_start(word) is not None


def _is_pure_role_label(word: str) -> bool:
    """判断 `word` 是否为**纯职务标签**（不含人名成分）。

    与 `_is_role_label`（L2 粗判）的区别：L2 命中「职务后缀」即可，
    但「王组长」这类是「人名 + 职务」的混合形态——职务标签可识别，
    姓氏却是个人信息，必须脱敏。本函数把两者区分开。

    判据（自上而下，命中即返回）：
      ① 整词 ∈ 通名前缀表（外部专家 / 各条线 / 业务 …）→ 纯标签；
      ② 「通名前缀 + 职务后缀」组合（技术骨干 / 外部专家骨干 …）→ 纯标签；
      ③ 职务词起点之前是**姓氏**（王组长 / 李经理 …）→ 混合形态，非纯标签；
      ④ 其余（通名 + 职务后缀，如"业务骨干"）→ 纯标签。

    【为什么要单列这个函数】
    D-2（Issue #59）的槽位分型需要「纯职务标签 → 保留」这一档；
    角色上下文通道（`_apply_role_context_pass`）此前对
    「成员：技术骨干」这类槽位整段打码，把职务标签当成了人名。
    """
    if not word:
        return False
    pat = _get_role_label_patterns()
    if word in pat["prefix_forms"]:
        return True
    k = _find_role_start(word)
    if k is None:
        return False
    prefix = word[:k]
    # ② 通名前缀 + 职务后缀 → 纯职务标签
    for pf in pat["prefix_forms"]:
        if prefix.startswith(pf):
            return True
    # ③ 姓 + 职务后缀 → 「某人 + 职务」，姓氏属个人信息
    if prefix and prefix[0] in SURNAME_SET:
        return False
    return True


def _find_name_spans(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    """在 text[start:end) 内查找具备 L1 证据的姓名片段（最长优先）。

    【为什么要扫描子串而不是整块】
    槽位内容常带尾随虚词（「王健负责」「张三的培训」），整块判定会把
    真名和虚词混在一起判不出证据；按子串扫描则能定位到「王健」本身。
    """
    spans: List[Tuple[int, int]] = []
    i = start
    while i < end:
        if not ("\u4e00" <= text[i] <= "\u9fa5"):
            i += 1
            continue
        j = i
        while j < end and ("\u4e00" <= text[j] <= "\u9fa5"):
            j += 1
        seg = text[i:j]
        hit: Optional[Tuple[int, int]] = None
        for length in (4, 3, 2):
            for k in range(0, len(seg) - length + 1):
                if _has_name_evidence(seg[k:k + length]):
                    hit = (i + k, i + k + length)
                    break
            if hit:
                break
        if hit:
            spans.append(hit)
            i = hit[1]
        else:
            i = j
    return spans


_SLOT_ITEM_SEP_RE = re.compile(r"[、，,]")


def _iter_slot_items(text: str) -> List[Tuple[int, int]]:
    """把槽位内容按顿号/逗号切成条目，返回各条目的 (start, end) 区间。

    【为什么要切条目】
    槽位内容是「条目列表」而非单一文本：「各条线副行长、总监」是**两个
    职务标签**，「孙海刚、毛月珍」是**两个人名**。不切条目就无法逐条
    分型（Issue #59 §三 第二级）。
    """
    items: List[Tuple[int, int]] = []
    start = 0
    for m in _SLOT_ITEM_SEP_RE.finditer(text):
        items.append((start, m.start()))
        start = m.end()
    items.append((start, len(text)))
    return items


def _find_person_title_span(text: str, start: int, end: int) -> Optional[Tuple[int, int]]:
    """「人名 + 职务」混合项：返回应脱敏的人名部分区间（无则 None）。

    场景：「王组长」「李经理」——职务词之前是**姓氏**时，说明这是
    「某人 + 职务」而不是纯职务标签，姓氏仍属个人信息，必须脱敏。
    """
    seg = text[start:end]
    if not seg:
        return None
    k = _find_role_start(seg)
    if k is None or k == 0:
        return None
    prefix = seg[:k]
    if prefix[0] not in SURNAME_SET:
        return None
    if not all("\u4e00" <= c <= "\u9fa5" for c in prefix):
        return None
    return (start, start + k)


def _make_colon_name_repl(mask: str):
    """构造规则C（全角冒号）的替换器：贪婪匹配 + 按职务词起点剥离。

    【剥离规则】
      命中片段内若出现职务词（如「副行长」「组长」「经理」），
      则从该职务词的**起点**切开：
        · 姓名部分（姓氏 + ≥1 名用字）整段替换为占位符；
        · 职务词**整体保留**（不得拦腰截断）。

    例：
      "组长：樊霖副组长" → "组长：XXX副组长"
      "负责人：蔡昀煜"    → "负责人：XXX"
      "组长：李经理"      → "组长：XXX"（姓氏+职务整体遮盖，避免尾字泄漏）

    【为什么不是「回退 1 字」】
      回退 1 字会把职务词切成 "XXX副组"——职务词被拦腰截断，
      既不是完整职务也不是完整姓名（Issue #57 §五 方案 A 的缺陷）。
    """
    def _repl(m: re.Match) -> str:
        s = m.group(0)
        # 职务词可能跨越贪婪匹配边界（"樊霖副组" + 后续 "长"），
        # 故向右多看几个字符再判定起点是否落在命中片段内部。
        ext = m.string[m.start(): m.end() + 4]
        k = _find_role_start(ext, limit=len(s))
        if k is not None and k >= 2:
            return mask + s[k:]
        return mask
    return _repl

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

_ENGLISH_TERM_WHITELIST: Optional[set] = None


def _get_english_term_whitelist() -> set:
    """取英文业务术语白名单（config.json → english_term_whitelist），带缓存。

    【为什么需要】Issue #51 真实业务语料实测：「Agent 协同工作的中枢」
    「Agent Skill 验收测试报告」中的 Agent/Skill 是**产品术语**而非英文人名，
    但 `_ENGLISH_NAME_RE`（首字母大写 + ≥3 个小写字母）会一刀切脱敏。
    白名单由真实语料频次挖掘（≥2 次）产出，并剔除常见英文人名
    （Lisa/David/Wang/Lee 等）——**真名优先**，宁可漏脱也不误伤真名。
    """
    global _ENGLISH_TERM_WHITELIST
    if _ENGLISH_TERM_WHITELIST is None:
        cfg = _load_config()
        _ENGLISH_TERM_WHITELIST = {w for w in (cfg.get("english_term_whitelist") or [])
                                  if isinstance(w, str) and w}
    return _ENGLISH_TERM_WHITELIST


def _apply_english_name_pass(text: str, counts: Dict[str, int]) -> str:
    """段落含角色词时，遮蔽段内英文人名（Lisa/David 等）。
    全大写术语（UAT/POS）与句首普通词不受影响——仅匹配首字母大写
    且非全大写的独立词，且限定角色上下文。
    G9 英文术语白名单（Issue #51）：Agent/Skill/Excel 等产品术语豁免。"""
    if not text or not isinstance(text, str):
        return text
    whitelist = _get_english_term_whitelist()
    out_lines = []
    for line in text.split("\n"):
        if any(k in line for k in _ROLE_KEYWORDS):
            def _repl(m: re.Match) -> str:
                # G9 英文术语白名单：产品/业务术语不是人名 → 保留原文
                if m.group(0) in whitelist:
                    return m.group(0)
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
            # D-2（Issue #59）：槽位内容分型——纯职务标签保留，不得当人名打码
            #   "成员：技术骨干" → "成员：技术骨干"（此前被抹成 "成员：XXX"）
            #   "组长：王组长"    → "组长：XXX"（姓 + 职务的混合形态仍脱敏）
            if _is_pure_role_label(name_candidate):
                return m.group(0)
            # 【批次二 G4 回归修复】候选姓名以连词/虚词起首 → 非人名
            # "审核和发布" → 角色词"审核"后的"和发布"不是人名
            if name_candidate[0] in _SLOT_HEAD_VETO:
                return m.group(0)
            if (name_candidate in EXCLUDED_COMMON_WORDS
                    or name_candidate[:2] in EXCLUDED_COMMON_WORDS
                    or name_candidate[:3] in EXCLUDED_COMMON_WORDS):
                return m.group(0)  # 保护，跳过替换
            # G4/G7（Issue #51）：整词保护表 / 跨词边界守卫
            # "规划文档生成与管理"曾被切成 "文档生成|与管理" 而误伤
            g_start = m.start(1)
            g_end = m.end(1)
            if _is_protected_by_common_word_guard(m.string, g_start, g_end):
                return m.group(0)
            if _is_boundary_split_misfire(m.string, g_start, g_end):
                return m.group(0)
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
            # G4/G7（Issue #51）：连词两侧若为业务术语，不脱敏
            # "规划文档生成与管理"曾被切成 "生成|与|管理" → "生XXX与XXX理"
            for gi in (1, 3):
                if _is_protected_by_common_word_guard(m.string, m.start(gi), m.end(gi)):
                    return m.group(0)
                if _is_boundary_split_misfire(m.string, m.start(gi), m.end(gi)):
                    return m.group(0)
            counts["姓名"] = counts.get("姓名", 0) + 2
            return "XXX" + m.group(2) + "XXX"
        new_line = _get_role_conjunction_re().sub(_conj_repl, new_line)
        out_lines.append(new_line)
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Issue #52（赵辉，2026-10）：职务与称谓式人名识别通道
#
# 背景：#52 §二 归纳出四类失败形态，其中「姓氏 + 职务后缀」「昵称形态」
#   「单字姓 + 动词边界」三类是 Skill 侧完全缺失的（P0）。
#
# 【为什么不能全局粗筛】—— Issue #49 的直接教训
#   在真实业务语料（biz_corpus 1200 条 + 误伤语料 102 条）上枚举
#   「实证姓 × 职务后缀」组合，实际出现 11 种，**全部是业务词**
#   （项工/务总/别总/单总/加工/和工/段工/项总/户经理），
#   无一真实称谓。若做全局「姓 + 后缀 → XXX」的粗筛，会把这批
#   业务词全部吃掉（"专项工作推进"→"专项XXX推进"），等价于制造
#   #49 同等级别的回归。因此本通道采用 **#52 §3.4 的区域化口径**：
#   称谓形态**只在角色锚点附近生效**（组织/小组后缀、请求动词、
#   职责动词邻近），区域外一律不启用。
#
# 【三类形态】
#   ① 姓 + 职务后缀：刘主任 / 王总 / 陈总 / 李工 / 张经理
#   ② 昵称形态：小李 / 老李
#   ③ 单字姓 + 动词边界：会议由刘主持
#
# 【保护词优先级】#52 §3.1 要求「保护词命中应作用于独立出现的职务词，
#   不得覆盖姓 + 职务后缀的组合形态」——本实现落在两层：
#   · 独立职务词（"经理"/"主任"）由 EXCLUDED_COMMON_WORDS 保护，不在本通道匹配范围
#     （本通道要求姓氏字 + 后缀的**组合**形态）；
#   · 组合形态若整体被更长业务词覆盖（"业务经理" ⊃ "务经理"），
#     则按**包含语义**豁免，避免把职务词连同人名一起放过。
# ---------------------------------------------------------------------------

_TITLE_NAME_CACHE: Optional[Dict[str, Any]] = None
_TITLE_SUFFIX_RE_CACHE: Optional[re.Pattern] = None
_TITLE_NICK_RE_CACHE: Optional[re.Pattern] = None
_TITLE_SINGLE_RE_CACHE: Optional[re.Pattern] = None


def _get_title_name_cfg() -> Dict[str, Any]:
    """读取职务/称谓形态配置（config.json → title_suffix_names），带缓存。"""
    global _TITLE_NAME_CACHE
    if _TITLE_NAME_CACHE is not None:
        return _TITLE_NAME_CACHE
    raw = _load_config().get("title_suffix_names") or {}
    if not isinstance(raw, dict):
        raw = {}

    def _sl(key: str) -> List[str]:
        val = raw.get(key) or []
        return [x for x in val if isinstance(x, str) and x]

    def _cs(key: str, default: str = "") -> set:
        val = raw.get(key)
        if isinstance(val, str):
            return set(val)
        if isinstance(val, list):
            return set(x for x in val if isinstance(x, str))
        return set(default)

    _TITLE_NAME_CACHE = {
        "enable": bool(raw.get("enable", True)),
        "suffixes": sorted(set(_sl("suffixes")), key=len, reverse=True),
        "nickname_prefixes": sorted(set(_sl("nickname_prefixes")), key=len, reverse=True),
        "org_anchor_chars": _cs("org_anchor_chars", "部组处室科队班所司厂园院"),
        "left_anchor_words": _sl("left_anchor_words"),
        "role_verbs_near": _sl("role_verbs_near"),
        "role_verbs_far": _sl("role_verbs_far"),
        "nickname_block_followers": _cs("nickname_block_followers"),
        "title_modifier_chars": _cs("title_modifier_chars"),
        "single_char_exclusions": _cs("single_char_exclusions"),
        "skip_phrases": _sl("skip_phrases"),
    }
    return _TITLE_NAME_CACHE


def _title_surname_class() -> str:
    """姓氏字符类（正则用）。"""
    return "".join(sorted(SURNAME_SET))


_ATTESTED_SURNAMES_CACHE: Optional[set] = None


def _get_attested_surnames() -> set:
    """D16 精确全名表实证姓氏首字集合（393 字）。

    【为什么昵称/单字姓通道要用「实证姓」而不是姓氏池】
    姓氏池（415 字）是**字面候选集**，含大量只作姓氏、不作人名首字的字
    （如"经"——"老经理"里的"老经"会被昵称通道误匹配）。
    D16 全名表（7398 条）的首字是**真实出现过的人名首字**，
    用它做昵称/单字姓通道的准入，可从源头砍掉这类伪姓氏。
    """
    global _ATTESTED_SURNAMES_CACHE
    if _ATTESTED_SURNAMES_CACHE is not None:
        return _ATTESTED_SURNAMES_CACHE
    try:
        names = _load_config().get("full_name_list") or []
        attested = set(n[0] for n in names
                       if isinstance(n, str) and len(n) >= 2)
    except Exception:
        attested = set()
    _ATTESTED_SURNAMES_CACHE = attested or set(SURNAME_SET)
    return _ATTESTED_SURNAMES_CACHE


def _get_title_suffix_re() -> Optional[re.Pattern]:
    """「姓 + 职务后缀」正则。"""
    global _TITLE_SUFFIX_RE_CACHE
    if _TITLE_SUFFIX_RE_CACHE is not None:
        return _TITLE_SUFFIX_RE_CACHE
    tc = _get_title_name_cfg()
    if not tc["enable"] or not tc["suffixes"]:
        _TITLE_SUFFIX_RE_CACHE = None
        return None
    alt = "|".join(re.escape(s) for s in tc["suffixes"])
    _TITLE_SUFFIX_RE_CACHE = re.compile(
        rf"([{_title_surname_class()}])(?:{alt})"
    )
    return _TITLE_SUFFIX_RE_CACHE


def _get_title_nick_re() -> Optional[re.Pattern]:
    """「昵称前缀 + 姓」正则。"""
    global _TITLE_NICK_RE_CACHE
    if _TITLE_NICK_RE_CACHE is not None:
        return _TITLE_NICK_RE_CACHE
    tc = _get_title_name_cfg()
    if not tc["enable"] or not tc["nickname_prefixes"]:
        _TITLE_NICK_RE_CACHE = None
        return None
    alt = "|".join(re.escape(p) for p in tc["nickname_prefixes"])
    attested = "".join(sorted(_get_attested_surnames()))
    _TITLE_NICK_RE_CACHE = re.compile(
        rf"(?:{alt})([{attested}])"
    )
    return _TITLE_NICK_RE_CACHE


def _get_title_single_re() -> Optional[re.Pattern]:
    """「动词 + 单字姓 + 职责动词」正则（#52 §3.2）。"""
    global _TITLE_SINGLE_RE_CACHE
    if _TITLE_SINGLE_RE_CACHE is not None:
        return _TITLE_SINGLE_RE_CACHE
    tc = _get_title_name_cfg()
    if not tc["enable"] or not tc["role_verbs_near"]:
        _TITLE_SINGLE_RE_CACHE = None
        return None
    verbs = "|".join(re.escape(v) for v in tc["role_verbs_near"])
    lefts = "|".join(re.escape(v) for v in tc["left_anchor_words"]) or r"由请派"
    attested = "".join(sorted(_get_attested_surnames()))
    _TITLE_SINGLE_RE_CACHE = re.compile(
        rf"(?:{lefts})([{attested}])(?=(?:{verbs}))"
    )
    return _TITLE_SINGLE_RE_CACHE


def _title_span_blocked(line: str, start: int, end: int) -> bool:
    """职务形态片段是否被业务词覆盖（按包含语义豁免）。

    两类覆盖源：
      ① 整词保护表 / 排除词表（EXCLUDED_COMMON_WORDS + common_word_guards）
         —— 只在「保护词**完整覆盖**命中片段」时豁免（span ⊆ phrase）。
         反向（phrase ⊆ span）**不适用**：保护表里就有"主任/经理/组长"
         这些独立职务词，若反向豁免会把"刘主任/张经理"整体放过，
         恰好违背 #52 §3.1「保护不得覆盖姓 + 职务后缀组合形态」的要求。
      ② 职务形态专属 skip_phrases（config）—— 显式配置的"姓 + 后缀"业务词
         （施工/加工/支行长…），双向包含即豁免。
    """
    tc = _get_title_name_cfg()
    span = line[start:end]
    if not span:
        return True
    # ① 保护表 / 排除词表：命中片段**本身就是**受保护词 → 豁免
    #    （独立出现的职务词，如"经理"/"组长"，不属称谓形态）
    if span in EXCLUDED_COMMON_WORDS or span in _get_common_word_guards():
        return True
    # ① 保护表 / 排除词表：span ⊆ phrase → 豁免
    for w in EXCLUDED_COMMON_WORDS:
        if len(w) > len(span) and span in w:
            return True
    for w in _get_common_word_guards():
        if len(w) > len(span) and span in w:
            return True
    # ② skip_phrases：双向包含即豁免
    for p in tc["skip_phrases"]:
        if p == span or (len(p) >= 2 and p in span):
            return True
    return False


def _title_left_anchor(line: str, start: int) -> bool:
    """左侧锚点：组织/小组后缀字符，或请求/指派类动词。"""
    tc = _get_title_name_cfg()
    if start > 0 and line[start - 1] in tc["org_anchor_chars"]:
        return True
    prefix = line[:start]
    for w in tc["left_anchor_words"]:
        if prefix.endswith(w):
            return True
    return False


def _title_right_anchor(line: str, end: int) -> bool:
    """右侧锚点：紧邻职责动词（near）或窗口内到会类动词（far）。"""
    tc = _get_title_name_cfg()
    tail = line[end:]
    for v in tc["role_verbs_near"]:
        if tail.startswith(v):
            return True
    window = line[end:end + 8]
    for v in tc["role_verbs_far"]:
        idx = window.find(v)
        if idx != -1 and idx <= 6:
            return True
    return False


def _apply_title_name_pass(text: str, counts: Dict[str, int]) -> str:
    """职务与称谓式人名识别通道（Issue #52）。

    仅在角色锚点附近生效（#52 §3.4 区域化口径），三类形态：
      ① 姓 + 职务后缀   ② 昵称形态   ③ 单字姓 + 动词边界
    """
    tc = _get_title_name_cfg()
    if not tc["enable"] or not text or not isinstance(text, str):
        return text
    suffix_re = _get_title_suffix_re()
    nick_re = _get_title_nick_re()
    single_re = _get_title_single_re()
    if not suffix_re and not nick_re and not single_re:
        return text
    skip_texts = _get_skip_texts()

    out_lines = []
    for line in text.split("\n"):
        hits: List[Tuple[int, int]] = []

        # ① 姓 + 职务后缀
        if suffix_re:
            for m in suffix_re.finditer(line):
                s, e = m.start(), m.end()
                if line[s] in tc["title_modifier_chars"]:
                    continue
                if _title_span_blocked(line, s, e):
                    continue
                # 通用词豁免（Issue #49 复评处置口径，真名优先）：「刘主任」等
                # 称谓形态若落在通用词（如「叶子」）之内 → 豁免。
                if _is_protected_by_standalone_exempt(line, s, e):
                    continue
                if line[s:e] in skip_texts:
                    continue
                if not (_title_left_anchor(line, s) or _title_right_anchor(line, e)):
                    continue
                hits.append((s, e))

        # ② 昵称形态（小X / 老X）
        if nick_re:
            for m in nick_re.finditer(line):
                s, e = m.start(), m.end()
                if line[m.start(1)] in tc["nickname_block_followers"]:
                    continue
                if e < len(line) and line[e] in tc["nickname_block_followers"]:
                    continue
                if _title_span_blocked(line, s, e):
                    continue
                # 通用词豁免：「小叶子」「老叶子」等昵称形态不脱敏
                if _is_protected_by_standalone_exempt(line, s, e):
                    continue
                if line[s:e] in skip_texts:
                    continue
                if not (_title_left_anchor(line, s) or _title_right_anchor(line, e)):
                    continue
                hits.append((s, e))

        # ③ 单字姓 + 动词边界（会议由刘主持）
        if single_re:
            for m in single_re.finditer(line):
                s, e = m.start(1), m.end(1)
                if line[s] in tc["single_char_exclusions"]:
                    continue
                if _title_span_blocked(line, s, e):
                    continue
                if _is_protected_by_standalone_exempt(line, s, e):
                    continue
                if line[s:e] in skip_texts:
                    continue
                hits.append((s, e))

        if not hits:
            out_lines.append(line)
            continue
        # 去重叠（优先保留靠前 / 靠长的命中）
        hits.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        merged: List[Tuple[int, int]] = []
        last_end = -1
        for s, e in hits:
            if s >= last_end:
                merged.append((s, e))
                last_end = e
        # 从后往前替换，避免位移
        new_line = line
        for s, e in reversed(merged):
            counts["姓名"] = counts.get("姓名", 0) + 1
            new_line = new_line[:s] + "XXX" + new_line[e:]
        out_lines.append(new_line)
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# D-10（Issue #62）：正文叙述型人名 —— 「姓名 + 职务后缀」形态
#
# 【问题】Issue #62 §二 指出：程序召回人名依赖「D16 名单库」与
#   「区域上下文（名单区 / 角色槽位 / 签名落款区）」双闸门。两者在
#   **正文叙述句**中同时失效：
#       "孙海刚行长指示业务部门"  →  原文一字未动
#   该形态既不在 D16 精确全名库（孙海刚为库外姓名），
#   也不在名单区（是正文散落叙述）。
#   七组比对中 G2 组 9 处真实残留全部为「孙海刚」，标准版遮盖为 XXX。
#
# 【解法】新增独立通道 `_apply_narrative_title_pass`：
#   匹配「姓 + 名用字{1,3} + 职务后缀」形态，命中时**只遮姓名**，
#   职务后缀整体保留（"孙海刚行长" → "XXX行长"）。
#
# 【证据口径：上下文证据 + 部分字频证据（Issue #62 §四 方案 A）】
#   职务后缀本身就是**上下文证据**——「某某+行长/经理/主任」里的前置二~四字
#   几乎不可能是业务术语（业务术语的职务位在**后**："业务资深经理"）。
#   故本通道**不要求尾字字频达标**：这正是 #62 漏脱的主因——
#   "李义申"的尾字「申」字频仅 2，被 L1 字频闸门拦死在门外。
#   但上下文证据不能单独用（否则"客户关系管理主管"类误伤），
#   故按姓名长度分档收紧（见 `_has_narrative_name_evidence`）：
#     · 2 字（姓 + 1 名用字）：尾字必须 **hot**（与 S1 高危姓规则同口径）
#       ——「管理主管」（尾字「理」仅 warm）、「方案经理」（尾字「案」非 hot）豁免；
#     · 3~4 字（姓 + 2~3 名用字）：尾字可低频，但要求其余字 ∈ hot∪warm
#       且至少一个名用字 ∈ hot∪warm。
#   姓氏位用 D16 实证姓氏集、护栏（业务词 / 整词保护 / 通用词豁免 / 边界误判）
#   与纯职务标签判定一应俱全，保证「各条线副行长」这类纯职务标签不被拆。
#
# 【与既有通道的分工】
#   · `_apply_title_name_pass`（#52）：「姓 + 职务后缀」（"李工"），2 字形态；
#   · 本通道（#62）：「姓 + 名用字{1,3} + 职务后缀」（"孙海刚行长"）；
#   · `_apply_role_context_pass`（#59）：「角色锚点 + 姓名」，锚点在**前**。
#   三者形态互斥，不重叠。
# ---------------------------------------------------------------------------

# 叙述型职务后缀（L2 职务标签表之外的补充）
#   "总" 是「李义申总提出」这类叙述句里的**单字**职务后缀（Issue #62 §四 方案 A
#   给出的实例）。单字后缀的误伤面远大于双字后缀（"张总共"/"李总结"），
#   故单列并收紧 —— 要求姓名部分 ≥ 3 字时才生效（见 _apply_narrative_title_pass）。
_NARRATIVE_EXTRA_SUFFIXES = ("总", "董事长", "监事长", "首席", "会长", "社长")

# 单字职务后缀集合（须收紧：姓名部分 ≥ 3 字）
_NARRATIVE_SINGLE_SUFFIXES = frozenset({"总"})

# 后缀 + 下一字 的组织形式排除：委员会 / 理事会 是「组织」而非「职务」——
#   "风险管理委员会"里的"管理"不是人名（真实业务语料实测误伤点）。
_NARRATIVE_SUFFIX_NEXT_GUARDS = {("委员", "会"), ("理事", "会")}

# 姓名字尾字 + 职务后缀 组成的**词**（不是「人名 + 职务」）——必须排除。
# 真实语料实测点（G7）："各条线执行人员提供执行数据，张瑜汇总，在决策会…"
#   D-10 把「张瑜汇」当成名字、把「总」当成职务后缀 → 遮成 "XXX总"，
#   把"汇总"拦腰切坏。此处按「字尾 + 单字后缀」成词表拦截。
_NARRATIVE_NAME_TAIL_WORD_GUARDS = {
    ("汇", "总"),   # 汇总
    ("结", "总"),   # 总结
    ("归", "总"),   # 归总
    ("全", "总"),   # 全总
}

# 「姓 + 名用字{1,3} + 职务后缀」正则（带缓存）
_NARRATIVE_TITLE_RE_CACHE: Optional[re.Pattern] = None


def _get_narrative_title_re() -> Optional[re.Pattern]:
    """「姓 + 名用字{1,3} + 职务后缀」正则（带缓存）。

    姓氏位用 **D16 实证姓氏集**（gated surnames）——排除「刁/阚/…」等仅见于
    《百家姓》的冷僻姓，它们是业务术语首字的高发区（水平/单元/沙子/明天）。
    名用字用**名字用字池**；池外生僻字（谧/熔/缋）属 #60 的名字位字池缺口，
    本通道不承担，候补由 #60 承接。
    """
    global _NARRATIVE_TITLE_RE_CACHE
    if _NARRATIVE_TITLE_RE_CACHE is not None:
        return _NARRATIVE_TITLE_RE_CACHE
    pat = _get_role_label_patterns()
    suffixes = {s for s in pat.get("suffixes", []) if s}
    suffixes |= set(_NARRATIVE_EXTRA_SUFFIXES)
    if not suffixes:
        _NARRATIVE_TITLE_RE_CACHE = None
        return None
    # 长后缀优先（副行长 > 行长）
    suf_sorted = sorted(suffixes, key=len, reverse=True)
    suf_alt = "|".join(re.escape(s) for s in suf_sorted)
    cfg = _load_config()
    surnames = "".join(sorted(_get_gated_surnames(cfg)))
    name_class = _build_name_char_class(_get_name_pool_chars())
    _NARRATIVE_TITLE_RE_CACHE = re.compile(
        rf"(?P<name>[{surnames}]{name_class}{{1,3}}?)(?P<suffix>{suf_alt})"
    )
    return _NARRATIVE_TITLE_RE_CACHE


def _has_narrative_name_evidence(name: str) -> bool:
    """D-10 证据层：**上下文证据**（职务后缀）+ **部分字频证据**。

    方案 A 的立论：职务后缀本身就是上下文证据——「某某+行长/经理/主任」里的
    前置二~四字几乎不可能是业务术语（业务术语的职务位在**后**：
    "业务资深经理"/"市场推广经理"）。故本通道**不要求尾字字频达标**，
    这正是 #62 漏脱的主因（"李义申"的尾字「申」字频为 2，被字频闸门拦死）。

    但上下文证据不能单独用（否则"客户关系管理主管"类误伤），故按姓名长度分档：
      · **2 字**（姓 + 1 名用字）：与 S1 高危姓规则**同口径**——尾字必须 hot。
        「管理主管」（尾字「理」仅 warm）、「方案经理」（尾字「案」非 hot）
        由此豁免；「张明经理」（尾字「明」hot）仍可脱敏。
      · **3~4 字**（姓 + 2~3 名用字）：字频闸门放宽到"尾字可低频"，
        但要求
          ① 名字位**除尾字外**的字 ∈ hot∪warm；
          ② 至少一个名用字 ∈ hot∪warm（避免"冷僻姓 + 全冷僻名"整串无证据）；
          ③ L1 四合一判据（`_has_name_evidence`）命中时直接通过
             —— 那是更强的证据（D16 全名 / 通用人名词典 / 尾字热）。
    """
    if not name or len(name) < 2:
        return False
    cfg = _load_config()
    hot = _get_gated_name_chars(cfg)
    warm = _get_warm_name_chars(cfg)
    if len(name) == 2:
        return name[1] in hot
    if _has_name_evidence(name):
        return True
    mid = name[1:-1]
    if mid and not all(c in hot or c in warm for c in mid):
        return False
    return any(c in hot or c in warm for c in name[1:])


def _apply_narrative_title_pass(text: str, counts: Dict[str, int]) -> str:
    """D-10（Issue #62 方案 A）：正文叙述型人名「姓名 + 职务后缀」召回通道。

    例：
      "孙海刚行长指示业务部门"  →  "XXX行长指示业务部门"
      "由李义申总安排"          →  "由XXX总安排"
      "李义申总提出"            →  "XXX总提出"
      "各条线副行长、总监"      →  原样保留（纯职务标签，非人名）
      "金融同业" / "电子银行"   →  原样保留（无职务后缀）
    """
    if not text or not isinstance(text, str):
        return text
    rx = _get_narrative_title_re()
    if rx is None:
        return text
    out_lines = []
    for line in text.split("\n"):
        hits: List[Tuple[int, int]] = []
        for m in rx.finditer(line):
            name = m.group("name")
            suffix = m.group("suffix")
            s0, s1 = m.start("name"), m.end("name")
            if len(name) < 2:
                continue
            # 单字职务后缀（"总"）误伤面大 → 收紧到姓名部分 ≥ 3 字
            if suffix in _NARRATIVE_SINGLE_SUFFIXES and len(name) < 3:
                continue
            # 组织形式排除（委员会 / 理事会）——"风险管理委员会"不是人名
            nxt = line[m.end("suffix")] if m.end("suffix") < len(line) else ""
            if (suffix, nxt) in _NARRATIVE_SUFFIX_NEXT_GUARDS:
                continue
            # 「字尾 + 后缀」成词排除（汇总 / 总结）——"张瑜汇总"不是"张瑜 + 总"
            if (name[-1], suffix) in _NARRATIVE_NAME_TAIL_WORD_GUARDS:
                continue
            # 纯职务标签（"各条线副行长"）→ 保留
            if _is_pure_role_label(name):
                continue
            # 护栏：整词保护表 / 通用词豁免 / 边界误判
            if _is_protected_by_common_word_guard(line, s0, s1):
                continue
            if _is_protected_by_standalone_exempt(line, s0, s1):
                continue
            if _is_boundary_split_misfire(line, s0, s1):
                continue
            # 业务词表命中（"业务资深经理"）→ 保留
            if name in _get_biz_guard_words():
                continue
            # 证据层（上下文证据 + 部分字频证据）
            if not _has_narrative_name_evidence(name):
                continue
            hits.append((s0, s1))
        if not hits:
            out_lines.append(line)
            continue
        # 去重叠：靠前优先；同起点取更长（贪婪匹配优先）
        hits.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        merged: List[Tuple[int, int]] = []
        last_end = -1
        for s, e in hits:
            if s >= last_end:
                merged.append((s, e))
                last_end = e
        new_line = line
        for s, e in reversed(merged):
            counts["姓名"] = counts.get("姓名", 0) + 1
            new_line = new_line[:s] + "XXX" + new_line[e:]
        out_lines.append(new_line)
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Issue #54 D1：命中通道分类（供灰区候选清单标注"命中通道"）
# ---------------------------------------------------------------------------

# 通道标签口径（与规则链一一对应，便于灰区裁决时定位责任通道）
CHANNEL_TITLE = "职务称谓"          # Issue #52 姓 + 职务后缀
CHANNEL_NICKNAME = "昵称形态"       # Issue #52 小X / 老X
CHANNEL_SINGLE = "单字姓锚点"       # Issue #52 会议由刘主持
CHANNEL_ROLE = "角色槽位"           # _ROLE_NAME_RE 角色前缀 + 姓名
CHANNEL_NAME = "姓名规则"           # 通用姓名通道
CHANNEL_ADDRESS = "地址通道"
CHANNEL_DATE = "日期规则"
CHANNEL_ENGLISH = "英文名"
CHANNEL_LLM = "LLM"
CHANNEL_REGEX = "正则兜底"
CHANNEL_UNKNOWN = "未知"


def classify_hit_channel(text: str, start: int, end: int,
                          source: str = "") -> str:
    """判定一个命中片段来自哪条规则通道（Issue #54 D1 候选清单字段）。

    参数：
      text   —— 命中所在的完整文本
      start/end —— 命中片段的字符区间 [start, end)
      source —— 实体来源标记（regex / llm / role_word）

    返回：通道标签（如"职务称谓"/"角色槽位"/"姓名规则"）。
    判定顺序：按**特异性从高到低**——先看是否命中形态最明确的通道
    （职务称谓 / 昵称 / 单字姓锚点），再看角色槽位，最后回落到来源标记。
    """
    if not text or start is None or end is None or not (0 <= start < end <= len(text)):
        return CHANNEL_UNKNOWN
    span = text[start:end]
    if not span:
        return CHANNEL_UNKNOWN

    tc = _get_title_name_cfg()
    suffix_re = _get_title_suffix_re()
    if suffix_re and suffix_re.fullmatch(span):
        return CHANNEL_TITLE
    nick_re = _get_title_nick_re()
    if nick_re and nick_re.fullmatch(span):
        return CHANNEL_NICKNAME
    single_re = _get_title_single_re()
    if single_re and len(span) == 1:
        m = single_re.search(text, max(0, start - 4), min(len(text), end + 12))
        if m and m.start(1) == start and m.end(1) == end:
            return CHANNEL_SINGLE
    if _ROLE_NAME_RE.fullmatch(span) or _ROLE_NAME_RE.search(span):
        return CHANNEL_ROLE
    if source == "role_word":
        return CHANNEL_ROLE
    if source == "llm":
        return CHANNEL_LLM
    if source == "regex":
        return CHANNEL_REGEX
    # 日期 / 地址 / 英文的粗判（仅用于通道标注，不参与脱敏决策）
    if re.match(r"^\d{4}[-/年]", span):
        return CHANNEL_DATE
    if span and span[-1] in "市县区镇乡村街路巷弄号楼室":
        return CHANNEL_ADDRESS
    if re.match(r"^[A-Z][a-z]{2,}$", span):
        return CHANNEL_ENGLISH
    if tc["enable"]:
        return CHANNEL_NAME
    return CHANNEL_NAME


def find_entity_span(text: str, snippet: str, offset: int = 0) -> Tuple[Optional[int], Optional[int]]:
    """在文本中定位实体的字符区间（Issue #54 D1 候选清单 span 字段）。

    从 `offset` 起向后查找首个完整出现的位置；找不到返回 (None, None)。
    """
    if not text or not snippet:
        return (None, None)
    idx = text.find(snippet, offset)
    if idx < 0:
        return (None, None)
    return (idx, idx + len(snippet))


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


def _get_list_evidence_gate() -> Dict[str, Any]:
    """名单通道证据门控配置（config.json → list_evidence_gate）。

    - `enable`：是否启用门控；
    - `mode`：`strict`（默认，需正向证据才脱敏）/ `loose`（保持整改前行为，
      作为召回优先场景的回退档，与 #54 的 `--auto-gray` 灰区机制同风格）；
    - `min_evidence_ratio`：名单中具备 L1 证据的块占比下限。
    """
    raw = (_load_config().get("list_evidence_gate") or {})
    try:
        ratio = float(raw.get("min_evidence_ratio", 0.5))
    except (TypeError, ValueError):
        ratio = 0.5
    return {
        "enable": bool(raw.get("enable", True)),
        "mode": str(raw.get("mode") or "strict").lower(),
        "min_evidence_ratio": ratio,
    }


def _list_evidence_ratio(text: str) -> Tuple[int, int]:
    """统计名单中具备 L1 人名证据的块数与总块数。"""
    blocks = [p for p in re.split(r'[、，]', text) if p]
    if not blocks:
        return (0, 0)
    hit = sum(1 for b in blocks if _has_name_evidence(b))
    return (hit, len(blocks))


def _list_name_repl(m: re.Match) -> str:
    """顿号名单保形：逐块脱敏，保留顿号/逗号。

    【D-3 整改（Issue #58）：名单通道接入 L1 证据门控】
    整改前本通道只做**形态**判定——「整行是 2-4 字 CJK 块 + 顿号」即当名单，
    未被枚举保护表覆盖的块一律当人名，于是页脚保密标识被整段抹除：
        "内部资料，禁止外传" → "XXXX，XXXX"   ← 密级声明失效（合规要素丢失）
        "各方代表、专家骨干" → "XXXX、XXXX"   ← 业务表述被抹
    整改后**反转判定方向**：先要求该行至少有一项具备 L1 人名证据，
    否则**整行保留**：
        "名单的判定应由『名单里的真名』来证明自己，
          而不是由『它长得像名单』来证明自己。"（Issue #58 §三）
    """
    text = m.group(0)
    gate = _get_list_evidence_gate()
    if gate["enable"] and gate["mode"] == "strict":
        hit, total = _list_evidence_ratio(text)
        if total == 0 or (hit / total) < gate["min_evidence_ratio"]:
            # 证据不足 → 整行保留（不可判定 → 保留）
            return text
    parts = re.split(r'([、，])', text)
    result = []
    offset = 0  # 各块在原文中的起始偏移（供 G4/G7 定位）
    for p in parts:
        seg_start = offset
        offset += len(p)
        if re.match(r'^[\u4e00-\u9fa5]{2,4}$', p):
            # 检查残字：若替换后前一块末字+当前块=残字组合则跳过
            if result and len(p) >= 2 and p[0] in _RESIDUE_STOP:
                result.append(p)
            # R-㊷：列表项本身命中业务词（活期/定期/票据池/商票保贴…）→ 保留原项
            elif _is_protected_by_biz_word(p, 0, len(p)):
                result.append(p)
            # G4/G7（Issue #51）：整词保护表 / 跨词边界守卫
            #   「报告生成、会议纪要」是并列业务术语，不是人名名单
            elif (_is_protected_by_common_word_guard(text, seg_start, seg_start + len(p))
                  or _is_boundary_split_misfire(text, seg_start, seg_start + len(p))):
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


_ORG_GEN_NAME_CTX: Optional[Tuple[re.Pattern, set, set]] = None


def _get_org_generic_name_ctx():
    """通用机构词 + 紧邻人名的判定上下文（正则 + 强证据词表），带缓存。"""
    global _ORG_GEN_NAME_CTX
    if _ORG_GEN_NAME_CTX is not None:
        return _ORG_GEN_NAME_CTX
    cfg = _load_config()
    words = set((cfg.get("org_name_rules") or {}).get("generic_words") or [])
    # D-8（Issue #64）：通用部门名（机构实体词表）同样「保留原文」，
    # 同样需要作为姓名通道的**左边界豁免点**——
    # 否则「运营管理部韩慧丽」这类「机构词 + 紧邻人名」会漏脱。
    words |= set(_get_org_entity_words())
    if not words:
        _ORG_GEN_NAME_CTX = (None, set(), set())
        return _ORG_GEN_NAME_CTX
    # 定长回顾：匹配片段**只含姓名**，机构词不在匹配范围内
    lb_list = [f"(?<={re.escape(w)})"
               for w in sorted(words, key=len, reverse=True)]
    # D-7（Issue #64）：白名单机构实体被整段脱敏后，其**脱敏形态**
    #   （哨兵 + ORG 占位符 + 哨兵）同样充当人名边界豁免点——
    #   否则「郑侃营运管理部钱媛」中的「钱媛」会因紧邻 ASCII 占位符
    #   而失去左边界 → 人名漏脱（实测 G3 语料 34 处「营运管理部 + 人名」）。
    d7_rules = _get_org_entity_layer_rules()
    if d7_rules.get("enable") and (d7_rules.get("entity_whitelist") or []):
        _d7_anchor = (_ORG_LAYER_MASK_OPEN
                      + re.escape(get_replacement("ORG", "XXXX"))
                      + _ORG_LAYER_MASK_CLOSE)
        lb_list.append(f"(?<={_d7_anchor})")
    lb = "|".join(lb_list)
    pattern = re.compile(rf'(?:{lb})(?P<name>[\u4e00-\u9fa5]{{2,3}})')
    _ORG_GEN_NAME_CTX = (pattern, get_full_name_set(), _get_common_name_dict())
    return _ORG_GEN_NAME_CTX


def _apply_org_generic_name_pass(text: str, counts: Dict[str, int]) -> str:
    """D-4 补丁（Issue #61-B）：通用机构词保留后，紧邻的人名仍须脱敏。

    【问题】D-4 把「运营部」这类通用机构词改为**保留原文**后，姓名通道的
    左边界（「前一字不得为汉字」）不再成立：
        "运营部韩慧丽向部门领导汇报"
    整改前 "运营部" 被脱敏成 XXXX（ASCII），边界成立 → 韩慧丽被脱敏；
    整改后 "运营部" 保留 → 边界失效 → **韩慧丽漏脱**（金标回归）。

    【解法】把「保留的通用机构词」当作**边界豁免点**：其后紧跟的 2-3 字
    若具备**强**姓名证据（D16 精确全名 / 通用人名词典），则整段脱敏。
    只用强证据而不用字频路径，避免把「运营部配合完成」这类业务短语
    误判为人名。

    【为什么不进姓名规则组】姓名规则组的外层守卫会在调用替换函数**之前**
    就给「姓名」计数；本通道需要「无证据则不替换」，计数必须由本通道
    自己掌控，故独立成通道执行。
    """
    if not text or not isinstance(text, str):
        return text
    pattern, d16, cdict = _get_org_generic_name_ctx()
    if pattern is None:
        return text
    out = text
    pos = 0
    while True:
        m = pattern.search(out, pos)
        if not m:
            break
        name = m.group("name")
        length = 0
        for cand_len in (3, 2):
            cand = name[:cand_len]
            if cand in d16 or cand in cdict:
                length = cand_len
                break
        if not length:
            pos = m.end()
            continue
        s = m.start("name")
        e = s + length
        if (_is_protected_by_common_word_guard(out, s, e)
                or _is_boundary_split_misfire(out, s, e)):
            pos = m.end()
            continue
        counts["姓名"] = counts.get("姓名", 0) + 1
        out = out[:s] + "XXX" + out[e:]
        pos = s + 3
    return out


def _apply_anchor_expand_pass(text: str, counts: Dict[str, int]) -> str:
    """
    N-B 层（Issue #16 REQ-NAME-001）：段落角色锚点扩散。

    对含角色关键词 + 冒号的段落，扩散遮蔽冒号后的姓名。
    在 _apply_role_context_pass 之后执行（叠加而非替代）。

    【D-2 整改（Issue #59）：槽位内容分型】
    整改前本通道只做**形态**判定——「2-4 字 CJK 块」即当人名遮蔽，
    于是职务标签被整段抹掉：
        "副组长：各条线副行长、总监"   → "副组长：XXXXXX、XXX"   ❌
        "成员：业务和技术骨干、外部专家骨干" → "成员：……、XXXXXX"   ❌
    整改后按**槽位内容性质**分型（先按顿号/逗号切条目，再逐条目判定）：

      | 条目类型   | 判据                        | 动作     |
      |------------|-----------------------------|----------|
      | 纯人名     | L1 正向证据（D16/词典/字频） | 脱敏     |
      | 人名+职务  | 职务词前的部分是姓氏        | 脱敏人名 |
      | 纯职务标签 | L2 命中（职务后缀/组合式）   | 保留     |
      | 不可判定   | 无任何证据                  | 保留     |

    【为什么「不可判定」默认保留】
    银行投产场景下，一处漏脱是**可追溯的单点事件**（可走人工复核），
    一处误伤是**大面积不可预期的可用性事故**（如 64 页 PPT 全页页脚被抹）。
    两者不同量级，缺省值倒向可追溯的一侧。

    示例：
      "核心组：蔡新发、张瑜、李明" → "核心组：XXX、XXX、XXX"
      "组长：孙海刚、毛月珍"      → "组长：XXX、XXX"
      "副组长：各条线副行长、总监" → 原样保留
      "组长张斌负责"              → 由 _apply_role_context_pass 处理（已有规则）
    """
    if not text or not isinstance(text, str):
        return text

    mask = get_replacement("NAME", "XXX")
    out_lines = []
    for line in text.split("\n"):
        new_line = line

        def _anchor_repl(m: re.Match) -> str:
            prefix = m.group(1)  # e.g. "核心组："
            rest = m.group(2)    # e.g. "蔡新发、张瑜、李明"

            # ---- 槽位内容分型：逐条目判定应脱敏的区间 ----
            spans: List[Tuple[int, int]] = []
            for item_start, item_end in _iter_slot_items(rest):
                found = _find_name_spans(rest, item_start, item_end)
                if found:
                    spans.extend(found)
                    continue
                pt = _find_person_title_span(rest, item_start, item_end)
                if pt:
                    spans.append(pt)
            if not spans:
                return prefix + rest

            result_parts = []
            last_end = 0
            for span_start, span_end in sorted(spans):
                if span_start < last_end:
                    continue
                word = rest[span_start:span_end]
                # 排除词保护
                if word in _ANCHOR_EXCLUDED_NAMES:
                    continue
                # 排除数字为主的词块（日期/编码碎片）
                digit_ratio = sum(c.isdigit() for c in word) / len(word)
                if digit_ratio >= 0.4:
                    continue
                # G4/G7（Issue #51）：整词保护表 / 跨词边界守卫
                abs_start = m.start(2) + span_start
                abs_end = m.start(2) + span_end
                if _is_protected_by_common_word_guard(m.string, abs_start, abs_end):
                    continue
                if _is_boundary_split_misfire(m.string, abs_start, abs_end):
                    continue
                # 替换
                counts["姓名"] = counts.get("姓名", 0) + 1
                result_parts.append(rest[last_end:span_start])
                result_parts.append(mask)
                last_end = span_end

            # 保留未匹配的后缀
            if last_end < len(rest):
                result_parts.append(rest[last_end:])

            return prefix + "".join(result_parts)

        new_line = _ANCHOR_COLON_RE.sub(_anchor_repl, new_line)
        out_lines.append(new_line)

    return "\n".join(out_lines)


def _apply_nc_region_name_pass(text: str, counts: Dict[str, int]) -> str:
    """N-C 区域内逐项姓名判定（Issue #50 §4.8 规则A 退出的召回替代通道）。

    【职责】
    规则A 退出主链路后，本通道承接它的**召回职责**，但把判定域从
    「全局形态粗筛」收缩到「已确认的姓名集中区内逐项判定」：

      1. N-C 层（T2 正文名单段 / T3 签名落款区）先定位姓名集中区；
      2. 区域内每项做**形态判定**（`is_name_shaped`）；
      3. 形态成立即遮盖。

    【为什么这样能替代规则A】
    - 规则A 的问题是**无上下文**的全局笛卡尔积匹配：
      「姓 + 任意名字池字」在全文任意位置都成立，误伤无上界；
    - 本通道的判定域被区域约束：区域已满足 Issue #50 §4.3 三条准则
      （同构 + 锚点词或 D16 种子 + 每项形态成立），
      因此区域内逐项判定的误伤面被上游准则收敛；
    - 区域内**不再施加字频闸门**——区域上下文本身就是更强的证据，
      这正是规则A 无法给到、而人做脱敏时天然会给到的东西。

    【实测】
    - 102 条通用语料：零误伤；
    - 1200 条真实业务语料：零误伤；
    - 真实文档（xlsx/docx/pptx）端到端：金标真名零泄漏。

    【执行时机】
    在**姓名规则组之后**执行：此时已脱敏的部分已是占位符形态，
    区域检测会把占位符视作正向姓名证据（`_is_masked_name`），
    未被规则层脱敏的漏网项则由本通道补位。
    """
    if not text or not isinstance(text, str):
        return text
    try:
        from name_region_detector import region_name_spans
    except ImportError:
        return text

    spans = region_name_spans(text)
    if not spans:
        return text

    mask = get_replacement("NAME", "XXX")
    out = text
    # 逆序替换，保证前面的坐标不受影响
    for start, end in sorted(spans, key=lambda x: -x[0]):
        seg = out[start:end]
        # 幂等性守卫：占位符不重复计数、不重复替换
        if re.fullmatch(r"[Xx]{2,4}", seg or ""):
            continue
        # G4/G7（Issue #51）：整词保护表 / 跨词边界守卫
        if _is_protected_by_common_word_guard(out, start, end):
            continue
        if _is_boundary_split_misfire(out, start, end):
            continue
        counts["姓名"] = counts.get("姓名", 0) + 1
        out = out[:start] + mask + out[end:]
    return out


def _apply_common_name_dict_pass(text: str, counts: Dict[str, int]) -> str:
    """通用人名词典层脱敏通道（Issue #55）。

    【职责】
    补齐 #50 规则A 退出主链路后遗留的「**孤立 + 库外 + 短名**」缺口：
    如「王明」「张明」「李雷」这类**不在 D16 精确库**里的高频中文人名，
    在无上下文锚点时不再被主链路脱敏。

    【与其它通道的关系】
    - **独立成层**（#55 约束①）：不混入 `full_name_list`，与 D16 库交集为 0；
    - **精确匹配**（#55 约束②）：哈希查表逐位比对，不做任何笛卡尔积/形态粗筛；
    - **右边界非汉字**（#55 约束③）：由 `_find_common_name_dict_candidates` 保证；
    - **双零误伤校验**（#55 约束④）：词表入库前已过 102+1200 双语料校验。

    【执行时机】
    在**姓名规则组之前**执行——本层是「库外补位」，不应被 D16 层的
    2 字名截断行为抢先（如「韩梅梅」会被 D16 的「梅梅」截成「韩XXX」）。
    本层先整体脱敏后，残余文本交由 D16 与其它规则层处理。

    【守卫链】
    复用既有全部守卫（银行链锁定 / 常用词覆盖 / 独立成词豁免 / 业务词 /
    G4 整词保护 / G7 跨词边界），保证本层与其它通道的误伤口径一致。
    """
    if not text or not isinstance(text, str):
        return text
    cands = _find_common_name_dict_candidates(text)
    if not cands:
        return text

    mask = get_replacement("NAME", "XXX")
    locked_spans = _bank_chain_locked_spans(text)
    out = text
    # 逆序替换，保证前面的坐标不受影响
    for start, end, cand in sorted(cands, key=lambda x: -x[0]):
        if locked_spans and _is_in_locked_span(locked_spans, start, end):
            continue
        # Agent 评审决策跳过：命中片段在 skip 集合中 → 不遮盖
        if _get_skip_texts() and cand in _get_skip_texts():
            continue
        # 幂等性守卫：占位符不重复替换
        if re.fullmatch(r"[Xx]{2,4}", out[start:end] or ""):
            continue
        # G4 整词保护表（Issue #51）
        if _is_protected_by_common_word_guard(out, start, end):
            continue
        # G7 跨词边界守卫（Issue #51）
        if _is_boundary_split_misfire(out, start, end):
            continue
        # 常用词覆盖保护（Issue #49）
        if _is_protected_by_common_word(out, start, end):
            continue
        # 独立成词豁免（Issue #49：「叶子」豁免，「叶子涵」真名优先）
        if _is_protected_by_standalone_exempt(out, start, end):
            continue
        # R-㊷ 业务词保护守卫（G1/G2/G3）
        if _is_protected_by_biz_word(out, start, end):
            continue
        counts["姓名"] = counts.get("姓名", 0) + 1
        out = out[:start] + mask + out[end:]
    return out


# ============================================================================
# 批次二：D-5（#60 方案 2）/ D-8（#64 机构清单）/ D-9（#63 数字口令）
# ============================================================================

# ---------------------------------------------------------------------------
# D-5（Issue #60 方案 2）：角色槽位内姓名强判别
#
# 【问题一：低频生僻字系统性漏脱】
#   「李熔 / 王谧 / 成葭 / 刘兆缋」的名字位字在 D16 库与名字位字频中均为 0，
#   L1 证据层（#58 落地）判不出证据 → 槽位内不脱敏。
#
# 【问题二：槽位正则的右边界断言存在字符类转义缺陷】
#   `_ROLE_NAME_RE` 的断言写作 `(?![\\u4e00-\\u9fa5\\d])`，
#   在 Python re 中被解析为「0x30–U+9FA5 的连续区间」，
#   于是槽位姓名后紧跟「、」「，」时断言失败，
#   整条名单（"负责人：李熔、王谧"）一条都不脱敏。
#
# 【方案】
#   槽位语义本身就是强证据：角色锚点之后的条目若为 2–4 字汉字串，
#   具备「姓氏起点」形态，且不是 L2 职务标签 / 通用词 / 机构词，
#   则直接判为人名——不再受 D16 字池与字频闸门约束。
#   作用域严格限定在角色槽位内，不影响正文散落人名（#62 承接）。
# ---------------------------------------------------------------------------

_SLOT_ANCHOR_RE: Optional[re.Pattern] = None
# 槽位条目分隔符（比 D-2 的 _SLOT_ITEM_SEP_RE 多含分号）
_SLOT_ITEM_SEP_WIDE = re.compile(r"[、，,；;]")
# 行政/场所/机构尾字：槽位条目以这些字结尾时判为非人名
_SLOT_TAIL_VETO = frozenset("市区县镇乡村路街道巷弄号院所校局委班组厅厂矿站")
# 【批次二 G4 回归修复】槽位/角色条目**起首虚词/连词**否决集
#
# 根因：「和」既是真实姓氏（He），又是极高频的连词。D-5 槽位强判别与
#   职务姓名通道仅凭「姓氏起点」放行，导致
#   "质量管理补充流程审核和发布" 中的 "和发布" 被判为人名 → "审核XXX"。
#
# 口径：候选姓名**首字**为下列虚词/连词时一律不判为人名。
#   取值范围刻意收窄为「几乎不作为姓氏」的虚词——于/向/石/单/谷 等
#   真实姓氏不在其中，避免漏脱真实人名。
#   D-5 兜底名单通道（extra_name_list，人工确认真名）不受此约束。
_SLOT_HEAD_VETO = frozenset("和与及或但则若因所被把将且而")


def _get_slot_anchor_re() -> re.Pattern:
    """角色槽位锚点正则：`(角色词)(分隔符)(槽位内容)`。

    槽位内容限定为「汉字 + 条目分隔符」序列，遇句读/换行即止，
    避免把后续叙述文本卷入槽位判定。
    """
    global _SLOT_ANCHOR_RE
    if _SLOT_ANCHOR_RE is not None:
        return _SLOT_ANCHOR_RE
    alt = "|".join(re.escape(k) for k in _ROLE_KEYWORDS)
    _SLOT_ANCHOR_RE = re.compile(
        r"(?P<kw>(?:" + alt + r"))(?P<sep>[:：\s]*)"
        r"(?P<rest>[\u4e00-\u9fa5、，,；;]{1,40})"
    )
    return _SLOT_ANCHOR_RE


def _iter_slot_items_wide(text: str) -> List[Tuple[int, int]]:
    """按顿号/逗号/分号切分槽位条目，返回各条目 (start, end) 区间。"""
    items: List[Tuple[int, int]] = []
    start = 0
    for m in _SLOT_ITEM_SEP_WIDE.finditer(text):
        items.append((start, m.start()))
        start = m.end()
    items.append((start, len(text)))
    return items


def _slot_item_name_span(item: str) -> Optional[Tuple[int, int]]:
    """槽位条目 → 应脱敏的人名区间（无则 None）。

    判据（D-5 强判别）：
      ① 形态：2–4 字纯汉字；
      ② 非 L2 纯职务标签（"技术骨干"/"外部专家" 保留）；
      ③ 非通用词 / 业务词 / 豁免词 / 整词保护表命中；
      ④ 尾字非行政/机构后缀（"海淀区"/"营业部" 保留）；
      ⑤ 姓氏起点（支持复姓）——这是把「像人名」与「是业务词」分开的关键约束；
      ⑥ 「人名 + 职务」混合形态只脱敏人名部分（"李明组长" → "XXX组长"）。
    """
    if not item or not (2 <= len(item) <= 4):
        return None
    if not all("\u4e00" <= c <= "\u9fa5" for c in item):
        return None
    if _is_pure_role_label(item):
        return None
    if item in _get_biz_guard_words() or item in _get_common_word_guards():
        return None
    if item in _get_standalone_exempt_words():
        return None
    if item in _ANCHOR_EXCLUDED_NAMES:
        return None
    if (item in EXCLUDED_COMMON_WORDS or item[:2] in EXCLUDED_COMMON_WORDS
            or item[:3] in EXCLUDED_COMMON_WORDS):
        return None
    if item[-1] in _SLOT_TAIL_VETO:
        return None
    # 【批次二 G4 回归修复】条目起首为连词/虚词 → 不判为人名
    if item[0] in _SLOT_HEAD_VETO:
        return None
    if not (item[0] in SURNAME_SET
            or item[:2] in _NAME_GATE_COMPOUND_SURNAMES):
        return None
    # 「人名 + 职务」混合形态：职务词起点之前才是人名
    k = _find_role_start(item)
    if k is not None and k >= 2:
        return (0, k)
    return (0, len(item))


def _apply_slot_strong_name_pass(text: str, counts: Dict[str, int]) -> str:
    """D-5（Issue #60 方案 2）：角色槽位内姓名强判别。

    必须在**姓名规则组之前**执行——否则「刘兆缋」会先被规则切成
    「刘兆」而留下残尾「缋」（部分脱敏残留）。
    """
    if not text or not isinstance(text, str):
        return text
    mask = get_replacement("NAME", "XXX")
    out_lines: List[str] = []
    for line in text.split("\n"):
        def _repl(m: re.Match) -> str:
            kw, sep, rest = m.group("kw"), m.group("sep"), m.group("rest")
            parts: List[str] = []
            last = 0
            changed = False
            for s, e in _iter_slot_items_wide(rest):
                item = rest[s:e]
                span = _slot_item_name_span(item)
                if span is None:
                    continue
                a, b = span
                abs_s = m.start("rest") + s + a
                abs_e = m.start("rest") + s + b
                if _is_protected_by_common_word_guard(m.string, abs_s, abs_e):
                    continue
                if _is_boundary_split_misfire(m.string, abs_s, abs_e):
                    continue
                parts.append(rest[last:s])
                parts.append(mask)
                last = s + b
                changed = True
                counts["姓名"] = counts.get("姓名", 0) + 1
            if not changed:
                return m.group(0)
            parts.append(rest[last:])
            return kw + sep + "".join(parts)
        out_lines.append(_get_slot_anchor_re().sub(_repl, line))
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# D-5 补强（Issue #60）：确认真名清单的**精确匹配通道**
#
# 【为什么单独开一路】
# D16 精确全名层对 3 字及以上名字使用左边界 `(?<![\u4e00-\u9fa5])`
# （只排除「紧跟汉字」，见 #49 P1-I 实测依据），因此
#     "该文件由蔡昀煜签署"  ← 「由」是汉字 → 左边界不成立 → 不脱敏
# 这条边界对「库内 7398 个常见全名」是必要的（防 "组余翔" 类误伤），
# 但对 `extra_name_list` 里**人工确知的真实姓名**不适用——
# 这些名字本身就是人名，前后带汉字完全正常。
#
# 【口径】
#   ① 只对 `config.extra_name_list` 中的条目生效（人工确认清单）；
#   ② 边界：**允许**中文相邻（"该文件由蔡昀煜签署" 必须命中），
#      仅排除紧邻 ASCII 字母（避免命中拼音串内部）；
#   ③ 长度降序（长名先匹配，避免截断）。
# 零误伤风险：清单条目均为确认真名，其字符组合（含熔/谧/葭/缋/钊/瑜/晔等
# 生僻字）与常用词无交集；实测通用语料 102 条、真实业务语料 1200 条
# 改动均为 0。
# ---------------------------------------------------------------------------

_EXTRA_NAME_RE_CACHE: Optional[re.Pattern] = None


def _get_extra_name_re() -> Optional[re.Pattern]:
    global _EXTRA_NAME_RE_CACHE
    if _EXTRA_NAME_RE_CACHE is not None:
        return _EXTRA_NAME_RE_CACHE
    cfg = get_config()
    names = [n for n in _get_extra_name_list(cfg)
             if isinstance(n, str) and len(n) >= 2]
    if not names:
        _EXTRA_NAME_RE_CACHE = None
        return None
    alt = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
    _EXTRA_NAME_RE_CACHE = re.compile(rf"(?<![A-Za-z])(?:{alt})(?![A-Za-z])")
    return _EXTRA_NAME_RE_CACHE


def _apply_extra_name_pass(text: str, counts: Dict[str, int]) -> str:
    """D-5 补强：确认真名清单在**任意上下文**按精确匹配脱敏。"""
    if not text or not isinstance(text, str):
        return text
    pat = _get_extra_name_re()
    if pat is None:
        return text
    mask = get_replacement("NAME", "XXX")

    def _repl(m: re.Match) -> str:
        counts["姓名"] = counts.get("姓名", 0) + 1
        return mask

    return pat.sub(_repl, text)


# ---------------------------------------------------------------------------
# D-6（Issue #60 方案 1）：GB2312 生僻名用字候选池
#
# 【问题】名字位字频闸门（#49 P1）按 D16 全名库自举字频，分三档：
#   热字（freq ≥ 12，尾字门槛）/ 温字（freq ≥ 3，非尾字门槛）/ 冷字（freq < 3）。
#   「尾字落在温字或冷字档」的生僻字人名——「陈鲲」「韩晗」「孟芸」「陈曦」
#   「蔡菁」「阮榕」「何嵘」「肖娅」——在**裸上下文**（无角色槽位、无职务
#   后缀、无名单结构）会被闸门整名拦死。批次二 D-5 只覆盖了槽位内场景，
#   「槽位外泛化召回」因此一直是 #60 的未闭合缺口。
#
# 【解法】GB2312 二级汉字（3008 字）作为**生僻名用字候选池**：
#   GB2312 按区（row）排序——16–55 区为一级汉字（3755 字，高频常用），
#   56–87 区为二级汉字（3008 字，次常用/生僻）。本层只在「尾字（或名字位
#   首字）∈ 二级字」时放行：二级字在现代汉语中本身罕见，「姓 + 二级字」
#   几乎总是人名；而一级字（支撑/占比/软件/覆盖/经验）正是 #49 复评实证的
#   误伤区，**不入本层**（这正是 D-5b「freq == 0 即判名」被证伪的原因）。
#
# 【闸门关系】hot 集**不放宽**（#60 §三 方案 1 的硬约束）：
#   本层是与既有 hot/warm 闸门**并列**的「冷档放行通道」，
#   不改动热字集/温字集，也不改动既有任何规则的正则。
#
# 【双向互斥】候选池与 L4 护栏表（common_word_guards / biz_guard_words）
#   互斥：护栏表出现过的字不进入候选池——避免把业务术语用字当成名用字
#   （「客户凭」「信息查」这类业务词不会被误拆成人名）。
#
# 【误伤控制】四道闸门：
#   ① 通用词否决表（config.json → name_rare_char_layer.veto_words）：
#      「姓 + 二级字」构成的通用词（时髦/房檐/江豚/白鹭/黄鳝…）整词保留；
#   ② 职务 / 机构 / 地名后缀守卫：尾字是「部/处/所/岛/洲…」等后缀字时保留；
#   ③ 残尾守卫：命中片段的下一字若与片段构成**已知全名**
#      （D16 全名库 ∪ 通用人名词典），说明本片段只是全名的前缀 → 放弃本次
#      匹配（宁可不脱，也不制造「XXX + 残字」形态，#57 口径）；
#   ④ 边界守卫：左右不得紧邻 ASCII 字母 / 数字。
#
# 【实测】通用业务语料 102 条、真实业务语料 1200 条**零命中**（误伤增量 0）；
#   裸上下文金标漏脱由 1 条（柳长春）降为 0 条；幂等 / 确定性 100%。
# ---------------------------------------------------------------------------

_GB2312_CHARS_CACHE: Dict[str, str] = {}


def _gen_gb2312_hanzi(first_row: int, last_row: int) -> str:
    """按 GB2312 区（row）生成汉字序列。

    纯标准库实现：逐字节解码 gb2312 编码表，不依赖任何外部数据文件
    或第三方词库（内网离线环境的硬约束）。
    """
    out: List[str] = []
    for hi in range(first_row, last_row + 1):
        for lo in range(0xA1, 0xFF):
            try:
                ch = bytes((hi, lo)).decode("gb2312")
            except UnicodeDecodeError:
                continue
            if len(ch) == 1 and "\u4e00" <= ch <= "\u9fa5":
                out.append(ch)
    return "".join(out)


def _get_gb2312_chars(level: str = "2") -> str:
    """取 GB2312 汉字序列（带缓存）。

    level="1"   → 一级汉字（16–55 区，3755 字，高频常用）
    level="2"   → 二级汉字（56–87 区，3008 字，次常用/生僻）——D-6 默认档
    level="all" → 全量（6763 字）
    """
    key = str(level or "2")
    cached = _GB2312_CHARS_CACHE.get(key)
    if cached is not None:
        return cached
    if key == "1":
        chars = _gen_gb2312_hanzi(0xB0, 0xD7)
    elif key == "all":
        chars = _gen_gb2312_hanzi(0xB0, 0xF7)
    else:
        chars = _gen_gb2312_hanzi(0xD8, 0xF7)
    _GB2312_CHARS_CACHE.clear()
    _GB2312_CHARS_CACHE[key] = chars
    return chars


# D-6 尾字守卫：机构 / 地名类后缀字（与候选池求交后生效）。
# 只拦「同时是二级生僻字」的那几个——避免「包公祠」类机构名被误拆成姓名。
# 仅对**三字及以上**片段生效：二字片段（丁祠）放行，避免误伤真实人名。
_RARE_NAME_TAIL_GUARD_CHARS = "祠"


_RARE_NAME_LAYER_CACHE: Dict[str, Any] = {}


def _get_rare_name_layer(cfg: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """D-6 层的配置视图（正则 / 候选池 / 否决表 / 守卫集），按配置 id 缓存。

    返回 None 表示本层不可用（未启用 / 配置缺失 / 候选池为空），
    调用方据此直接跳过（保持与既有通道一致的「缺配置即降级」风格）。
    """
    if cfg is None:
        cfg = _load_config()
    layer = cfg.get("name_rare_char_layer") or {}
    if not layer.get("enable", True):
        return None
    cfg_id = id(cfg)
    cached = _RARE_NAME_LAYER_CACHE.get("v")
    if cached is not None and cached[0] == cfg_id:
        return cached[1]

    level = str(layer.get("gb2312_level", "2"))
    rare_all = _get_gb2312_chars(level)
    if not rare_all:
        return None
    surnames = _get_gated_surnames(cfg)
    if not surnames:
        return None

    # ---- 双向互斥：候选池 ∩ L4 护栏表用字 = 空 ----
    guard_chars = set()
    for w in _get_common_word_guards():
        guard_chars.update(w)
    for w in _get_biz_guard_words():
        guard_chars.update(w)

    hot = set(_get_gated_name_chars(cfg))
    warm = set(_get_warm_name_chars(cfg))
    # 放行档 = 二级生僻字 − L4 护栏表 − hot 集。
    #
    # 【不放宽 hot 集】（#60 §三 方案 1 硬约束）的准确含义：
    #   hot 集**内容与判定阈值完全不变**（仍由 D16 全名库自举，freq ≥ 12），
    #   本层是一个**并列**的放行通道，不修改 hot/warm 的分类结果。
    #   hot 字本身已被既有闸门放行，无需本层重复；warm 字（3 ≤ freq < 12）
    #   被既有「尾字门槛」拦死，正是 D-6 要救的对象（「陈曦」「孟芸」
    #   「蔡菁」「阮榕」「韩晗」——尾字均为 warm 档二级字）。
    cold = "".join(
        ch for ch in rare_all
        if ch not in guard_chars and ch not in hot
    )
    if not cold:
        return None
    cold_set = set(cold)
    # 名字位可延展字：放行档 + hot/warm 闸门字。
    # 热/温字本身是强名用字证据，允许在「姓 + 生僻字」之后再延展一格，
    # 避免「李姗姗」被切成「李姗」「何嵘斌」被切成「何嵘」。
    ext = "".join(sorted(cold_set | hot | warm))
    ext_set = set(ext)

    ext_body = _build_name_char_class_body(ext)
    surn_alt = _build_name_char_class("".join(sorted(surnames)))

    # D-6 主形态：**最大匹配** —— 姓 + 1~3 个名字位用字。
    #
    # 为什么用最大匹配而不是「姓 + 生僻字」的固定形态：
    #   固定形态会把「白茯苓」切成「白茯」（残留「苓」）、把「付梓」连同
    #   前置的「于」一起吃掉（「于付梓」）——两者都会在通用词上产生
    #   「XXX + 残字」的残尾形态（#57 同类缺陷）。最大匹配让整词一次成型，
    #   通用词否决表才能按**整词**精确生效。
    #
    # 尾部 `(?![名字位用字])` 保证不会把后面的字截断成残字。
    # 注意：ext_body 是**字符类内容部分**（无方括号），必须显式补上 `[...]`
    # 才能构成字符类；直接用 `(?:{ext_body}){1,3}` 会退化成「匹配 3000+ 字的
    # 字面串」，导致整层永不命中。
    rx = re.compile(
        rf"(?<![A-Za-z0-9])"
        rf"(?P<name>(?:{surn_alt})(?:[{ext_body}]){{1,3}})(?![{ext_body}])"
    )

    veto = {w for w in (layer.get("veto_words") or []) if w}
    tail_guard = {c for c in _RARE_NAME_TAIL_GUARD_CHARS if c in cold_set}
    known_names = set(get_full_name_set()) | set(_get_common_name_dict())

    view = {
        "rx": rx,
        "veto": veto,
        "tail_guard": tail_guard,
        "known_names": known_names,
        "cold_set": cold_set,
        "ext_set": ext_set,
        # 兼容别名：rare_set 即「放行档」= 候选池 ∩ 护栏表补集 ∩ 冷档。
        # 与 L4 护栏表互斥、且不含 hot 集（#60 §三 方案 1 硬约束）。
        "rare_set": cold_set,
    }
    _RARE_NAME_LAYER_CACHE.clear()
    _RARE_NAME_LAYER_CACHE["v"] = (cfg_id, view)
    return view


def _veto_overlap(
    span: str,
    text: str,
    s: int,
    e: int,
    veto: set,
) -> bool:
    """通用词否决表的**位置重叠**判定。

    否决词与匹配片段只要在原文中**占据重叠的字符位置**即视为否决命中。
    单纯全等判定会漏掉三类真实误伤：
      * 前缀型：正则只匹配到「白茯」，而否决词是「白茯苓」；
      * 后缀型：正则匹配到「于付梓」（把前置的「于」也吃进来），
        而否决词是「付梓」；
      * 内嵌型：正则从否决词**内部**起匹配（「武侯祠」→「侯祠」），
        而否决词是「武侯祠」。

    实现口径：对每个否决词，在原文中枚举其全部出现位置；
    只要存在一处出现与片段 [s, e) 有交集，即判为否决命中。
    三类都会把通用词拆成「XXX + 残字」，属于 #57 同类的残尾形态。
    """
    if not veto:
        return False
    if span in veto:
        return True
    for w in veto:
        if not w:
            continue
        start = text.find(w)
        while start != -1:
            w_end = start + len(w)
            # 区间相交判定
            if start < e and w_end > s:
                return True
            start = text.find(w, start + 1)
    return False


def _apply_rare_name_pass(text: str, counts: Dict[str, int]) -> str:
    """D-6（Issue #60 方案 1）：GB2312 生僻名用字候选池兜底层。

    例：
      "会议纪要 陈鲲 汇报"   →  "会议纪要 XXX 汇报"
      "由刘兆缋签署"          →  "由XXX签署"
      "本季度营收增长明显"      →  原样保留（通用词，无生僻名用字）
    """
    if not text or not isinstance(text, str):
        return text
    layer = _get_rare_name_layer()
    if layer is None:
        return text
    rx = layer["rx"]
    veto = layer["veto"]
    tail_guard = layer["tail_guard"]
    known_names = layer["known_names"]
    cold_set = layer["cold_set"]
    mask = get_replacement("NAME", "XXX")

    hits: List[Tuple[int, int]] = []
    for m in rx.finditer(text):
        s, e = m.start("name"), m.end("name")
        span = m.group("name")
        # 放行条件：片段里必须**至少含一个冷档生僻字**。
        # 全是 hot/warm 字的组合由既有闸门负责，本层不重复放行，
        # 也避免把「王总」这类职务/称谓误当人名。
        if not any(c in cold_set for c in span[1:]):
            continue
        # ① 通用词否决表：按**重叠**判定而非全等——
        #    否决词可能是匹配片段的前缀（白茯 ⊂ 白茯苓）或后缀（付梓 ⊂ 于付梓）。
        if _veto_overlap(span, text, s, e, veto):
            continue
        # ② 机构 / 地名后缀守卫（三字及以上才拦，避免误伤「丁祠」类人名）
        if len(span) >= 3 and span[-1] in tail_guard:
            continue
        # ③ 残尾守卫：下一字与本片段构成已知全名 → 本片段只是全名前缀
        nxt = text[e] if e < len(text) else ""
        if nxt and (span + nxt) in known_names:
            continue
        hits.append((s, e))
    if not hits:
        return text

    out = text
    for s, e in reversed(hits):
        counts["姓名"] = counts.get("姓名", 0) + 1
        out = out[:s] + mask + out[e:]
    return out


# ---------------------------------------------------------------------------
# D-8（Issue #64）：机构实体清单（银行全称 / 银行简称 / 通用部门名）
#
# 【清单来源】业务方提供的全国主要银行名称与通用银行部门设置清单。
# 【三类语义】
#   ① bank_entities（银行全称）：整段脱敏，保留「银行」尾字
#      （口径与既有 BANK 占位符一致："中国工商银行" → "XX银行"）。
#      解决的问题：既有通道对「广州农村商业银行」会留下
#      「广州XX农村商业银行」的混合残态。
#   ② bank_abbreviations（银行简称）：整段脱敏（"工行" → "XXX"）。
#      解决的问题：简称此前既不脱敏，又会被姓名规则误拆
#      （"农行"→"XXX"、"花旗"→"XXX"，属通用词误伤）。
#   ③ entity_words（通用部门名）：**整段保留**，作为机构实体词表。
#      解决的问题：部门名此前会被部分拆开——"董事会办公室"→"XXXX办公室"、
#      "纪检监察组"→"XXX监察组"（误伤）。
#      保留口径与人工标准版一致（标准版保留「营运管理部」等通用部门名）；
#      若业务侧要求整段脱敏，把对应词移入 composite_words 即可。
# ---------------------------------------------------------------------------

_ORG_ENTITY_SENTINEL = "\x00WBORGENT{}\x00"
_ORG_ENTITY_WORDS_CACHE: Optional[List[str]] = None


def _get_org_entity_words() -> List[str]:
    """通用部门名（机构实体词表，整段保留），按长度降序。"""
    global _ORG_ENTITY_WORDS_CACHE
    if _ORG_ENTITY_WORDS_CACHE is None:
        rules = _load_config().get("org_name_rules") or {}
        _ORG_ENTITY_WORDS_CACHE = sorted(
            (w for w in (rules.get("entity_words") or []) if w),
            key=len, reverse=True)
    return _ORG_ENTITY_WORDS_CACHE


def _apply_org_entity_pass(text: str, counts: Dict[str, int]) -> str:
    """D-8：银行全称/简称整段脱敏 + 通用部门名整段保护。"""
    if not text or not isinstance(text, str):
        return text
    rules = _load_config().get("org_name_rules") or {}
    banks = sorted((w for w in (rules.get("bank_entities") or []) if w),
                   key=len, reverse=True)
    abbrs = sorted((w for w in (rules.get("bank_abbreviations") or []) if w),
                   key=len, reverse=True)
    if banks:
        alt = "|".join(re.escape(w) for w in banks)
        bank_re = re.compile(rf"(?:{alt})")

        def _bank_repl(m: re.Match) -> str:
            frag = m.group(0)
            if frag.endswith("银行"):
                counts["银行名"] = counts.get("银行名", 0) + 1
            else:
                counts["组织名"] = counts.get("组织名", 0) + 1
            if frag.endswith("银行"):
                return get_replacement("BANK", "XX银行")
            return get_replacement("ORG", "XXXX")
        text = bank_re.sub(_bank_repl, text)
    if abbrs:
        alt = "|".join(re.escape(w) for w in abbrs)
        abbr_re = re.compile(rf"(?:{alt})")

        def _abbr_repl(m: re.Match) -> str:
            counts["组织名"] = counts.get("组织名", 0) + 1
            return get_replacement("ORG", "XXXX")
        text = abbr_re.sub(_abbr_repl, text)
    # 通用部门名：整段保护（占位符在流水线末尾还原）
    for i, w in enumerate(_get_org_entity_words()):
        if w in text:
            text = text.replace(w, _ORG_ENTITY_SENTINEL.format(i))
    return text


def _restore_org_entity_words(text: str) -> str:
    """还原 D-8 保护占位符（流水线末尾调用）。"""
    if not text or "\x00WBORGENT" not in text:
        return text
    words = _get_org_entity_words()
    for i, w in enumerate(words):
        text = text.replace(_ORG_ENTITY_SENTINEL.format(i), w)
    return text


# ---------------------------------------------------------------------------
# D-7（Issue #64，2026-11-09）：L3 机构实体层（org_stems × org_suffixes 组合式）
#
# 【问题】机构名通道此前只有两层语义：
#   · `generic_words`（独立通用词）→ **保留**原文；
#   · `composite_words`（前缀成词）→ **整段脱敏**。
# 「通名 + 后缀」组合（如「零售部」「贸金部」「营运管理部」）既不在两张表里，
# 只能落到通用机构名正则的兜底分支：
#     (?<![一-龥])[一-龥]{2}(?:部|科|中心|管委会|办公室)(?![一-龥])
# 于是两种错误同时存在：
#   ① 通用部门名被整段脱敏——「零售部 / 贸金部 / 特资部 / 金市部 / 公司部」
#      在人工标准版中**一律保留**（实测 G3 语料各 1 处，我方全遮）→ 过度脱敏；
#   ② 真实机构实体只能按通用词保留——「营运管理部」无法按业务需要整段脱敏，
#      口径无法切换（#64 验收第 1 条长期无法满足的根因）。
#
# 【方案】引入 L3 机构实体层：**组合式生成** + 白名单决策
#   · `org_stems`（通名）× `org_suffixes`（后缀）→ 候选机构实体组合；
#   · 组合命中且在 `entity_whitelist`（业务侧机构实体白名单）内
#     → **整段脱敏**（杜绝「营运XXXX」这类混合态）；
#   · 组合命中但不在白名单 → **整段保留**（回退批次一通用词口径），
#     并以哨兵整体占位，防止下游通用机构名正则把它切开成混合态；
#   · `composite_words`（D-4 前缀成词层）同样属「整段脱敏」，一并由本层接管，
#     保证「信息科技部」在两层之间**只有一种口径**。
#
# 【与通用词层互斥】实体层先于通用词层执行：命中即以哨兵占位，
#   通用词层再也看不到同一段文本 → 同一条文本不可能同时命中两种口径。
#
# 【哨兵】私用区码位（与 D-11 补强同源，避免被英文/数字通道吞掉）：
#   保留哨兵 \ue002…\ue003（原文）、脱敏哨兵 \ue004…\ue005（ORG 占位符）。
#   哨兵在流水线末尾统一还原，还原后即为最终文本。
# ---------------------------------------------------------------------------

_ORG_LAYER_KEEP_OPEN = "\ue002"
_ORG_LAYER_KEEP_CLOSE = "\ue003"
# 组合正则缓存（通名 × 后缀 组合式生成）
_ORG_LAYER_COMBO_RE: Optional[re.Pattern] = None
_ORG_LAYER_COMBO_MAXLEN = 0
_ORG_LAYER_COMBO_CFG_ID = 0


def _get_org_entity_layer_rules() -> Dict[str, Any]:
    """D-7 配置块（org_stems / org_suffixes / entity_whitelist）。"""
    rules = (_load_config().get("org_name_rules") or {})
    return rules.get("org_entity_layer") or {}


def _get_org_entity_layer_sets() -> Tuple[set, set]:
    """返回 (整段脱敏集合, 整段保留集合)。

    · 整段脱敏集合 = `entity_whitelist` ∪ `composite_words`
      （业务侧机构实体白名单 + D-4 前缀成词层）；
    · 整段保留集合 = 由 `org_stems` × `org_suffixes` 组合生成、
      且不在脱敏集合中的候选机构实体。
    """
    rules = _get_org_entity_layer_rules()
    stems = {w for w in (rules.get("org_stems") or []) if w}
    sufs = {w for w in (rules.get("org_suffixes") or []) if w}
    wl = {w for w in (rules.get("entity_whitelist") or []) if w}
    # D-4 前缀成词层（composite_words）同属「整段脱敏」口径，由本层一并接管，
    # 保证同一文本在两层之间只有一种口径。
    parent = (_load_config().get("org_name_rules") or {})
    composite = {w for w in (parent.get("composite_words") or []) if w}
    combos = {s + x for s in stems for x in sufs}
    mask_set = wl | composite
    return mask_set, (combos - mask_set)


def _get_org_layer_combo_re() -> Optional[re.Pattern]:
    """D-7 组合正则（通名 × 后缀）。

    缓存与配置对象绑定（id 变化即重建），以便测试通过
    `AIKIT_CONFIG_PATH` 切换配置时口径即时生效。
    """
    global _ORG_LAYER_COMBO_RE, _ORG_LAYER_COMBO_MAXLEN, _ORG_LAYER_COMBO_CFG_ID
    cfg = _load_config()
    if _ORG_LAYER_COMBO_RE is not None and _ORG_LAYER_COMBO_CFG_ID == id(cfg):
        return _ORG_LAYER_COMBO_RE
    _mask_set, keep_set = _get_org_entity_layer_sets()
    if not keep_set:
        _ORG_LAYER_COMBO_RE = re.compile(r"(?!x)x")   # 空匹配（永不命中）
        _ORG_LAYER_COMBO_MAXLEN = 0
    else:
        alt = "|".join(re.escape(w) for w in sorted(keep_set, key=len, reverse=True))
        _ORG_LAYER_COMBO_RE = re.compile(rf"(?:{alt})")
        _ORG_LAYER_COMBO_MAXLEN = max(len(w) for w in keep_set)
    _ORG_LAYER_COMBO_CFG_ID = id(cfg)
    return _ORG_LAYER_COMBO_RE


def _is_org_layer_combo_span(text: str, start: int, end: int) -> bool:
    """判断 [start, end) 是否与某个 D-7 组合式机构实体相交。

    用于通用机构名通道的守卫：命中「通名+后缀」组合的片段一律**保留**，
    杜绝「零售部」这类通用部门名被整段遮掉（过度脱敏），
    也杜绝「计划财务部」被切成「计划财务 + 管理部」这类混合残态。
    """
    combo_re = _get_org_layer_combo_re()
    if combo_re is None or _ORG_LAYER_COMBO_MAXLEN <= 0:
        return False
    pad = _ORG_LAYER_COMBO_MAXLEN + 2
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    window = text[lo:hi]
    for m in combo_re.finditer(window):
        if m.start() + lo < end and start < m.end() + lo:
            return True
    return False


# 白名单实体的脱敏哨兵：首字符为私用区码位（非 ASCII 字母），
#   保证「实体前紧邻的人名」右边界仍然成立——
#   否则「郑侃营运管理部」会产出「郑侃XXXX」，人名因右边界被占位符
#   打断而漏脱（实测：2 字名规则右边界为 (?!ASCII 字母)）。
_ORG_LAYER_MASK_OPEN = "\ue004"
_ORG_LAYER_MASK_CLOSE = "\ue005"
_ORG_LAYER_MASK_RE = re.compile(
    _ORG_LAYER_MASK_OPEN + r"(?P<mask>.*?)" + _ORG_LAYER_MASK_CLOSE, re.DOTALL)


def _apply_org_entity_mask_pass(text: str, counts: Dict[str, int]) -> str:
    """D-7：机构实体白名单整段脱敏（哨兵包裹，末尾统一还原）。

    · 命中白名单 → **整段**替换为 ORG 占位符，不产生「营运XXXX」混合态；
    · 哨兵包裹保证相邻人名的左右边界不被占位符打断。
    """
    if not text or not isinstance(text, str):
        return text
    rules = _get_org_entity_layer_rules()
    if not rules.get("enable"):
        return text
    wl = sorted({w for w in (rules.get("entity_whitelist") or []) if w},
                 key=len, reverse=True)
    if not wl:
        return text
    alt = "|".join(re.escape(w) for w in wl)
    ph = get_replacement("ORG", "XXXX")
    label = _label_for_replacement(ph)

    def _repl(m):
        counts[label] = counts.get(label, 0) + 1
        return _ORG_LAYER_MASK_OPEN + ph + _ORG_LAYER_MASK_CLOSE

    return re.sub(rf"(?:{alt})", _repl, text)


def _restore_org_entity_layer(text: str) -> str:
    """还原 D-7 机构实体白名单脱敏哨兵（流水线末尾调用）。"""
    if not text or _ORG_LAYER_MASK_OPEN not in text:
        return text
    return _ORG_LAYER_MASK_RE.sub(lambda m: m.group("mask"), text)


# ---------------------------------------------------------------------------
# D-11 补强（Issue #63 批次四）：「地名性前缀 vs 专名网点」判别
#
# 【问题】人工标准版对机构名的脱敏存在**语境判别**，同一名称在两种语境
#   下处理相反——「洪山支行」在机构清单里被遮蔽（XX支行），在叙述句里
#   保留原文。因此判别必须按**结构**驱动，而非按词表驱动：
#     · 机构清单/枚举语境 → 前缀遮蔽
#         「叫号机：由总行营业部、三山支行、福新支行、洪山支行、永兴支行
#           进行演练」→ 总行营业部、XX支行、XX支行、XX支行、XX支行
#     · 散文叙述语境   → 专名网点保留原文
#         「（3）由三山支行负责配合发起定期借记往账演练操作」
#         「本轮补录由洪山支行和晋江支行配合执行」
#         「由台江支行营业部配合发起开立银行承兑汇票的演练」
#         「模拟操作指定福新支行、科技支行、温泉支行执行」
#
# 【口径】三步判别（仅对**精确命中专名网点**的机构名生效）：
#   ① 精确匹配 named_org_keep（如「三山」「台江」）→ 进入保留候选；
#   ② 更长前缀含地名性词（geo_org_prefixes，如「福州三山支行」）→ 仍遮蔽；
#   ③ 清单语境 → 仍遮蔽。清单语境三判据（任一成立）：
#        · 顿号机构链成员 ≥4（如「叫号机」行 5 个）；
#        · 顿号机构链中存在**非专名**成员（如「总行营业部」「福清分行营业部」
#          ——链中出现行政区/通用机构成员即说明这是机构清单）；
#        · 位于清单标记域（参演机构/演练网点/参演网点…）之前。
#      三判据均不成立 → 判定为叙述语境 → **保留原文**。
#
# 【实现】保留以哨兵占位（与 D-8 机构实体保护同一机制）：哨兵串不可被
#   任何正则命中，故下游姓名/地址通道无法再切分该机构名——
#   这同时解决了「洪山支行」被姓名通道误吞成「XXX支行」的问题。
#   哨兵在流水线末尾统一还原。
# ---------------------------------------------------------------------------

_ORG_KEEP_SENTINEL = "\ue000{}\ue001"
# 机构类型后缀链（可多级连写，如「支行营业部」「分行营业部」）
_ORG_TYPE_CHAIN_RE = re.compile(r"(?:分行|支行|营业部|分理处|网点)+")
_ORG_NAMED_RULES_CACHE: Optional[Tuple[List[str], List[str], List[str]]] = None
# 哨兵 ↔ 原文映射（按线程隔离，保证并发一致性）
_ORG_KEEP_STATE = threading.local()


def _get_org_named_rules() -> Tuple[List[str], List[str], List[str]]:
    """(专名网点保留词, 地名性前缀词, 清单标记词)。"""
    global _ORG_NAMED_RULES_CACHE
    if _ORG_NAMED_RULES_CACHE is None:
        cfg = _load_config()
        rules = cfg.get("place_rules") or {}
        keep = sorted((w for w in (rules.get("named_org_keep") or []) if w),
                      key=len, reverse=True)
        org_rules = cfg.get("org_name_rules") or {}
        geo = sorted(set(rules.get("geo_org_prefixes") or [])
                     | set(org_rules.get("admin_divisions") or []),
                     key=len, reverse=True)
        markers = [w for w in (rules.get("org_list_markers") or []) if w]
        _ORG_NAMED_RULES_CACHE = (keep, geo, markers)
    return _ORG_NAMED_RULES_CACHE


def _apply_named_org_keep_pass(text: str, counts: Dict[str, int] = None) -> str:
    """D-11 补强：专名网点在**非清单语境**保留原文（哨兵保护）。"""
    if not text or not isinstance(text, str):
        return text
    keep, geo, markers = _get_org_named_rules()
    if not keep:
        return text

    # ① 机构名实例：[起点, 终点, 前缀原文, 类型链]
    insts: List[List[Any]] = []
    for m in _ORG_TYPE_CHAIN_RE.finditer(text):
        cs, ce = m.span()
        i = cs
        while i > 0 and "一" <= text[i - 1] <= "鿿":
            i -= 1
        insts.append([i, ce, text[i:cs], text[cs:ce]])
    if not insts:
        return text

    # ② 顿号机构链：以「、」直接相连的机构名归为同一链
    chain_id = list(range(len(insts)))
    for k in range(len(insts) - 1):
        if text[insts[k][1]:insts[k + 1][0]].strip() == "、":
            chain_id[k + 1] = chain_id[k]
    groups: Dict[int, List[int]] = {}
    for k, cid in enumerate(chain_id):
        groups.setdefault(cid, []).append(k)

    # ③ 逐实例判定
    is_named = [False] * len(insts)   # 是否精确命中专名网点
    for k, (i, ce, praw, _chain) in enumerate(insts):
        w = next((x for x in keep if len(praw) >= len(x) and praw.endswith(x)), None)
        if w:
            is_named[k] = True

    protected: List[Tuple[int, int]] = []
    for k, (i, ce, praw, _chain) in enumerate(insts):
        if not is_named[k]:
            continue
        w = next(x for x in keep if len(praw) >= len(x) and praw.endswith(x))
        # ② 更长前缀含地名性词 → 仍按遮蔽处理（「福州三山支行」）
        head = praw[:-len(w)][-8:]
        if any(g in head for g in geo):
            continue
        # ③ 清单语境
        grp = groups[chain_id[k]]
        if len(grp) >= 4:
            continue
        if any(not is_named[j] for j in grp):
            continue
        ctx = text[max(0, i - 60):i]
        if any(mk in ctx for mk in markers):
            continue
        # 保护范围仅限「专名网点 + 类型链」，不含其左侧的叙述性前缀
        #   （「本轮补录由洪山支行」→ 只保护「洪山支行」）
        protected.append((i + len(praw) - len(w), ce))

    if not protected:
        return text

    # ④ 哨兵替换（按位置倒序，避免下标漂移）；同一原文共用同一序号
    table: List[str] = []
    frag_index: Dict[str, int] = {}
    out = text
    for i, ce in sorted(protected, reverse=True):
        frag = text[i:ce]
        idx = frag_index.get(frag)
        if idx is None:
            idx = len(table)
            frag_index[frag] = idx
            table.append(frag)
        out = out[:i] + _ORG_KEEP_SENTINEL.format(chr(0xE100 + idx)) + out[ce:]
    _ORG_KEEP_STATE.table = table
    return out


def _restore_named_org_keep(text: str) -> str:
    """还原 D-11 补强的专名网点保护占位符（流水线末尾调用）。"""
    if not text or "\ue000" not in text:
        return text
    table = getattr(_ORG_KEEP_STATE, "table", None) or []
    for i, frag in enumerate(table):
        text = text.replace(_ORG_KEEP_SENTINEL.format(chr(0xE100 + i)), frag)
    return text


# ---------------------------------------------------------------------------
# D-9（Issue #63）：独立数字口令
#
# 【口径】
#   「纯数字」本身不是敏感判据（业务编号、18 位协议号必须保留），
#   必须叠加**口令语义**：
#     ① 形态特征：全同数字（888888）、周期重复（123123）、连续升降（123456）；
#     ② 上下文语义：同段落/相邻单元格出现「口令/密码/初始/默认/临时/登录」等。
#   日期形态（20111012 / 2011-10-12 / 201110）一律保留。
# ---------------------------------------------------------------------------

# 【批次四修复（D-9 边界口径，原 Issue #63 复现残留）】
#   原式用 `\w` 作边界，但 Python 3 的 `\w` 在 Unicode 模式下**匹配汉字**，
#   于是「默认初始密码为888888」这类**中文紧邻**的独立口令完全匹配不上
#   （实测 findall 结果为 []），形成真实漏脱。
#   改为 ASCII 边界后：中文上下文可命中；而「字母/数字/小数点」串内部
#   （如 Hxitsm01、18 位协议号）仍然不会命中。
_DIGIT_PWD_RE = re.compile(r"(?<![0-9A-Za-z.])(\d{6,8})(?![0-9A-Za-z.])")
_DIGIT_PWD_KEYWORDS = (
    "口令", "密码", "初始密码", "初始值", "初始口令", "默认密码", "默认口令",
    "临时密码", "临时口令", "登录密码", "登录口令", "重置密码", "测试密码",
    "账号", "帐号", "pwd", "passwd", "password", "pin",
)


def _is_date_like_digits(d: str) -> bool:
    """日期形态判定：8 位 YYYYMMDD / 6 位 YYYYMM。"""
    if len(d) == 8:
        y, m, day = int(d[:4]), int(d[4:6]), int(d[6:8])
        return 1900 <= y <= 2099 and 1 <= m <= 12 and 1 <= day <= 31
    if len(d) == 6:
        y, m = int(d[:4]), int(d[4:6])
        return 1900 <= y <= 2099 and 1 <= m <= 12
    return False


def _is_patterned_digits(d: str) -> bool:
    """口令形态特征：全同数字 / 周期重复（123123、ababab）。

    【为什么不把「连续升降」（123456）算进来】
      连号在业务侧极常见（金额、流水号、序列号都可能是 123456），
      只有**重复结构**才是口令的典型特征；连号仅在明确的口令语义
      上下文里才脱敏。
    """
    if len(set(d)) == 1:
        return True
    for period in (2, 3):
        if len(d) % period == 0 and len(d) >= 2 * period:
            if d[:period] * (len(d) // period) == d:
                return True
    return False


def _digit_segment_bounds(line: str, start: int, end: int) -> Tuple[int, int]:
    """数字所在「单元格/段落」片段的边界。

    分隔符：制表符、`|`（表格文本）以及 `<` / `>`（XML 标签）。

    【为什么把 XML 标签也算作分隔符（批次二 G5 回归修复）】
      XML 感知路径下整份文档被当作**一条长行**处理，若「片段」只按
      tab/| 切分，则一个 sheet 的全部单元格落入同一片段——口令语义词
      出现在 A1 就会把 B31 的业务编号（050103-往账处理）一并脱敏。
      标签边界天然对应「单元格/文本节点」边界，纳入分隔符后，
      语义词判定回到「与数字同片段」的原始口径。
    """
    left = 0
    for sep in ("\t", "|", "<", ">"):
        idx = line.rfind(sep, 0, start)
        if idx + 1 > left:
            left = idx + 1
    right = len(line)
    for sep in ("\t", "|", "<", ">"):
        idx = line.find(sep, end)
        if idx != -1 and idx < right:
            right = idx
    return left, right


def _is_compound_business_code(line: str, end: int) -> bool:
    """数字后接「-中文描述」形态 → 业务编号，非口令。

    例："（050103-往账处理）"、"（051512-网银跨行转账异常处理）"——
      这是交易码/流程码的复合写法，数字只是编号前缀，
      不具备「独立口令」特征。
    """
    if end >= len(line) or line[end] != "-":
        return False
    nxt = line[end + 1:end + 2]
    return bool(nxt) and "\u4e00" <= nxt <= "\u9fa5"


# 口令语义词的邻近窗口大小（字符数）
_DIGIT_KW_WINDOW_BACK = 60
_DIGIT_KW_WINDOW_FWD = 40
# 邻近窗口内允许跨越的 XML 标签数上限（相邻单元格场景够用，
# 跨整张 sheet 命中则会被拦下）
_DIGIT_KW_MAX_TAGS = 2


def _digit_kw_nearby(line: str, start: int, end: int) -> bool:
    """数字邻近窗口内是否有口令语义词（跨标签数受限）。

    用途：支持「标签-密码」「密码-值」这类**相邻单元格**场景
    （语义词与数字不在同一片段，但物理相邻），同时避免
    「整张 sheet 任意单元格命中」。
    """
    lo = max(0, start - _DIGIT_KW_WINDOW_BACK)
    hi = min(len(line), end + _DIGIT_KW_WINDOW_FWD)
    window = line[lo:hi].lower()
    if not any(k in window for k in _DIGIT_PWD_KEYWORDS):
        return False
    # 窗口内标签数超限 → 视为跨度过大，不采信
    if window.count("<") > _DIGIT_KW_MAX_TAGS:
        return False
    return True


# 「标签-值」形态的**口令类**标签词。
#
# 【为什么不把「账号/帐号」算进来】
#   账号是**业务标识**（与付款协议号、业务编号同类），不是口令；
#   G3 实测：「账号：123123、206900、456456」中的人工标准版只遮盖了
#   形态化的 123123/456456，**保留了非形态的 206900**。
#   若把账号当成口令标签，整列账号都会被遮成 ******，属过度脱敏。
#   故「账号」只保留在**邻近窗口**语义词表里（可触发形态化数字脱敏），
#   不作为「标签-值」的触发标签。
_DIGIT_FIELD_VALUE_KEYWORDS = (
    "口令", "密码", "初始密码", "初始值", "初始口令", "默认密码", "默认口令",
    "临时密码", "临时口令", "登录密码", "登录口令", "重置密码", "测试密码",
    "pwd", "passwd", "password", "pin",
)


def _digit_is_field_value(line: str, start: int) -> bool:
    """数字是否处于「口令标签-值」形态（口令类标签词紧邻其前，可隔分隔符）。

    例：「初始密码：888888」「初始密码 888888」→ True；
        「账号：123123、206900、456456」中的 206900 → False
        （账号是业务标识；且其前是列表分隔符「、」，回退后仍是数字而非标签）。
    """
    i = start
    while i > 0 and line[i - 1] in "：:＝=、,，/／ \t":
        i -= 1
    tail = line[max(0, i - 12):i]
    return any(tail.endswith(k) for k in _DIGIT_FIELD_VALUE_KEYWORDS)


def _apply_digital_password_pass(text: str, counts: Dict[str, int]) -> str:
    """D-9：独立数字口令脱敏（口径见上方注释）。

    脱敏条件（任一）：
      ① 所在片段（单元格/段落）**只含该数字**，且数字具备口令形态
         （全同 / 周期重复）——对应「独立单元格中的 123123、888888」；
      ② **同一片段内**出现口令语义词（口令/密码/初始/登录…），
         此时无论数字形态如何都脱敏。

    【批次二 G5 回归修复：语义词判定由「整行」收窄为「同片段/邻近窗口」】
      原实现对整行做关键词扫描。XML 感知路径下整份 sheet 是一条长行，
      A1 单元格的「账号」会让 B31 的业务编号（050103-往账处理）被脱敏。
      收窄口径：
        ① 语义词与数字处于**同一片段**（同一单元格/段落）→ 命中；
        ② 语义词在数字**邻近窗口**内（前 60 字 / 后 40 字），
           且窗口跨越的 XML 标签数 ≤ 2（允许「标签-密码」「密码-值」
           这类相邻单元格场景，但不允许跨整张 sheet 命中）。
    """
    if not text or not isinstance(text, str):
        return text
    out_lines: List[str] = []
    for line in text.split("\n"):
        low = line.lower()

        def _repl(m: re.Match) -> str:
            d = m.group(1)
            if _is_date_like_digits(d):
                return m.group(0)
            # 复合业务编号（"050103-往账处理"）→ 数字只是编号前缀
            if _is_compound_business_code(line, m.end()):
                return m.group(0)
            left, right = _digit_segment_bounds(line, m.start(), m.end())
            segment = line[left:right]
            # 【批次四修复】口令判据由「语义词命中即脱敏」收紧为**双证据**：
            #   ① 形态证据（全同 / 周期重复），或
            #   ② 语义证据（邻近窗口内有口令语义词）**且**该数字是其
            #      片段（单元格/段落）内的**唯一**数字。
            # 【为什么必须收紧】
            #   原口径在「账号：123123、206900、456456」这类**清单**里会把
            #   远离形态特征的普通业务号一并脱敏（G3 实测 206900 → ******，
            #   而人工标准版保留该业务号），属过度脱敏。
            #   收紧后：清单中的 123123/456456（周期形态）照常脱敏，
            #   206900（非形态）保留，与人工标准版一致。
            kw_hit = _digit_kw_nearby(line, m.start(), m.end())
            sole_in_segment = (segment.strip() == d)
            patterned = _is_patterned_digits(d)
            field_value = _digit_is_field_value(line, m.start())
            if kw_hit:
                if not (patterned or field_value):
                    return m.group(0)
            else:
                if not (sole_in_segment and patterned):
                    return m.group(0)
            counts["其他"] = counts.get("其他", 0) + 1
            return "******"
        out_lines.append(_DIGIT_PWD_RE.sub(_repl, line))
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# D-11（Issue #63）：地名 / 网点名 / 职场名
#
# 【问题】
#   `config.replacement` 里早就有 `LOCATION: "XX"` 占位符，但规则体系中
#   **没有任何一条规则产出它**——纯地名、网点名、职场名处于完全无覆盖状态：
#       '八卦岭'      → '八卦岭'
#       '地点：八卦岭职场501会议室。' → '地点：八卦岭职场501会议室。'
#
# 【为什么不走「行政区划通名（XX市/XX县）+ 地名通名（岭/江/山…）」的裸匹配】
#   Issue #63 §3.1 风险提示已明确：地名的通名后缀极易与普通词碰撞
#   （「翻山越岭」「长江大桥」），必须要求专名上下文或词典支持。
#   因此本通道采用**词典驱动**（Issue #63 §3.1 通道 B「由使用方提供」）：
#       place_rules.place_names —— 地名
#       place_rules.site_names  —— 网点 / 场地 / 职场名
#   词典来源：七组真实交付文件的人工标准版比对（真实漏脱片段取证）。
#
# 【与既有地址通道的分工】
#   地址通道（_apply_address_pass）是**回溯式**的：从「市/路/支行/大厦…」等
#   行政与道路后缀向左收集地名前缀（漳平支行 → XX支行）。
#   它不认识「职场/场地/网点/园区/基地」这类**职场与场地后缀**，
#   故本通道补齐该缺口，并按「前缀脱敏 + 后缀保留」的可回溯口径产出：
#       八卦岭职场501会议室 → XX职场501会议室
#       旭辉职场A栋六楼     → XX职场A栋六楼
#
# 【关键守卫：地名性前缀 vs 专名网点（Issue #63 §三 判别分歧）】
#   同一个地名在不同上下文里的敏感性完全不同：
#       '连江敖江支行'（网点名）→ 需脱敏
#       '连江自来水'  （外部机构名）→ 保留（与人工标准版一致）
#   故规则为：**后接 keep_after_words（机构行业词）→ 整体保留**，
#   其余上下文按地名脱敏。地名整词保护表 place_guards 额外兜底
#   （如「长江大桥」——地名通名与普通词碰撞的高发区）。
# ---------------------------------------------------------------------------

_PLACE_RULES_CACHE: Optional[Dict[str, Any]] = None


def _get_place_rules() -> Dict[str, Any]:
    """D-11 词典（config.json → place_rules），带缓存。"""
    global _PLACE_RULES_CACHE
    if _PLACE_RULES_CACHE is None:
        rules = (_load_config().get("place_rules") or {})
        names = [w for w in (list(rules.get("place_names") or [])
                             + list(rules.get("site_names") or []))
                 if isinstance(w, str) and w]
        _PLACE_RULES_CACHE = {
            "names": sorted(set(names), key=len, reverse=True),
            "net_suffixes": tuple(sorted(
                {w for w in (rules.get("net_suffixes") or []) if w},
                key=len, reverse=True)),
            "keep_after": tuple(sorted(
                {w for w in (rules.get("keep_after_words") or []) if w},
                key=len, reverse=True)),
            "guards": tuple(sorted(
                {w for w in (rules.get("place_guards") or []) if w},
                key=len, reverse=True)),
        }
    return _PLACE_RULES_CACHE


def _is_protected_by_place_guard(text: str, start: int, end: int) -> bool:
    """D-11 地名整词保护表（**相交**语义）。

    【为什么用相交而不是「包含」语义】
      保护词（如「长江大桥」）比命中片段（如品牌规则命中的「长江」）更长，
      命中片段只是保护词的一部分——「包含」语义要求保护词完整覆盖命中片段，
      这里恰好成立；但对姓名类命中片段（如「江大」⊂「长江大桥」）同样成立。
      本函数统一按**相交**判定，语义更保守：只要命中片段落在任一保护词
      的出现区间内，即判定为「普通词/地名通名组合」，不脱敏。

    【真名安全】
      保护词表由人工维护且均为地名/普通词，与 D16 全名库、金标无交集。
    """
    for w in _get_place_rules()["guards"]:
        lo = max(0, start - len(w))
        idx = text.find(w, lo)
        while idx >= 0 and idx <= end:
            if idx < end and start < idx + len(w):
                return True
            idx = text.find(w, idx + 1)
    return False


def _is_place_word_in_name_form(text: str, start: int, end: int, word: str) -> bool:
    """D-11 人名形态守卫：词典词是否处于「姓 + 词 + 名」的姓名片段中。

    【为什么需要】
      D-11 词典词（旭辉 / 连江 / 深圳 / 张江 / 漳平…）本身全部由**名字用字**
      组成，与人名用字高度同形。若不区分「地名用法」与「姓名用法」，
      本通道（执行于姓名通道之前）会把姓名里的词先切掉：
          '负责人：龚旭辉' → '负责人：龚XX'   （标准版为 '负责人：XXX'）
      真名因此丢失，且占位符形态与标准版不一致。

    【判据（三者同时满足才判为姓名）】
      ① 词典词**全部字符**都在名字用字池（否则不可能是姓名片段）；
      ② 紧**前**一个字是姓氏（姓名片段的姓氏位）；
      ③ 紧**后**一个字是名字用字，或该词已到串尾/非汉字边界
         （姓名片段的名位或结束）。

    【为什么 ③ 不要求「后一个字是名字用字」才算数】
      「负责人：龚旭辉」中「旭辉」已是姓名的后两字（后接串尾），
      属典型姓名片段；而「旭辉职场A栋六楼」中「旭辉」后接「职」（非名字用字）
      是场地名用法——两者靠 ③ 区分。

    【真名安全】
      本守卫只会让**人名**保留原文（后续姓名通道仍会脱敏），
      不放行任何地名/机构名，故不引入漏脱。
    """
    if not word or start <= 0:
        return False
    name_pool = _get_name_pool_set()
    if any(ch not in name_pool for ch in word):
        return False
    prev_ch = text[start - 1]
    if prev_ch not in SURNAME_SET:
        return False
    if end < len(text):
        nxt = text[end]
        if nxt in name_pool:
            return True
        # 后接非名字用字 → 不是姓+词的姓名片段（如「旭辉职场」）
        return False
    # 到串尾 → 「姓 + 词」即完整姓名
    return True


def _apply_place_site_pass(text: str, counts: Dict[str, int]) -> str:
    """D-11：地名 / 网点名 / 职场名脱敏（词典驱动，前缀脱敏 + 后缀保留）。"""
    if not text or not isinstance(text, str):
        return text
    rules = _get_place_rules()
    names = rules["names"]
    if not names:
        return text
    keep_after = rules["keep_after"]
    mask = get_replacement("LOCATION", "XX")
    out = text
    for name in names:
        idx = 0
        while True:
            i = out.find(name, idx)
            if i < 0:
                break
            j = i + len(name)
            idx = j
            # ⓿ 银行判定链锁定区间（Issue #53 §3.4 通道互斥）：机构链归银行链所有，
            #   其内部地名**不得**再被本通道改写（「中国银行深圳分行」→ 应为
            #   「XX银行深圳分行」，而非「XX银行XX分行」）。
            #   区间随替换实时重算（文本长度会变）。
            locked_spans = _bank_chain_locked_spans(out)
            if locked_spans and _is_in_locked_span(locked_spans, i, j):
                continue
            # ① 地名整词保护表（长江大桥 等）
            if _is_protected_by_place_guard(out, i, j):
                continue
            # ② 机构名前缀形态（连江自来水）→ 整体保留
            if keep_after and out.startswith(keep_after, j):
                continue
            # ③ 前接占位符字母（已脱敏区域）→ 跳过，保证幂等
            if i > 0 and out[i - 1] in "Xx":
                continue
            # ④ 人名形态守卫：「姓 + 词典词」构成姓名时，本通道必须让位给姓名通道。
            #   词典词（如「旭辉」「连江」「深圳」）本身全部由名字用字组成，
            #   与人名用字高度同形；实测「负责人：龚旭辉」被本通道切成
            #   「负责人：龚XX」——既非标准版的「XXX」，也丢失了真名。
            #   判据：词典词**全部字符都在名字用字池**、且**紧前一个字是姓氏**、
            #   且**紧后一个字也在名字用字池或为串尾** → 判为姓名片段，跳过。
            if _is_place_word_in_name_form(out, i, j, name):
                continue
            # ⑤ 紧邻占位符吸收：银行品牌规则已把「深圳」改写成「深圳XX」
            #    （保留地名前两字 + XX），本通道再脱敏会得到「XXXX」——
            #    占位符长度与人工标准版（XX）不一致。此处把紧邻的占位符
            #    一并吸收为单个 LOCATION 占位符（深圳XX → XX）。
            absorb = 0
            while out[j + absorb:j + absorb + 1] == "X":
                absorb += 1
            if absorb:
                out = out[:i] + mask + out[j + absorb:]
                counts["地址"] = counts.get("地址", 0) + 1
                idx = i + len(mask)
                continue
            out = out[:i] + mask + out[j:]
            counts["地址"] = counts.get("地址", 0) + 1
            idx = i + len(mask)
    return out


# ---------------------------------------------------------------------------
# D-12（Issue #63）：企业 / 机构全称整段脱敏
#
# 【问题】
#   组织名通道只覆盖 org_suffixes 的 14 项后缀（部/中心/办公室/处/科/室/所/
#   站/小组/科室/事业部/职能部门/分室/中心组），**不含「公司/有限公司」**，
#   导致企业全称完全漏脱：
#       '漳平市泽源水电开发有限公司' → '漳平市泽源水电开发有限公司'
#
# 【口径：行政区划 + 企业字号 + 组织形式 整段匹配】
#   只在**行政区划锚点**（org_name_rules.admin_divisions）之后匹配，
#   因此「某金融科技公司」「顶级咨询公司」「母公司的CFO」这类
#   **无行政区划前缀**的泛称写法不受影响（通用语料误伤增量 0）。
#
# 【为什么必须整段脱敏】
#   局部替换会产生「XX市XX」这类混合残态（既残留行政区划、又残留字号），
#   人工标准版的做法是整段替换（XXX），本通道与之对齐。
# ---------------------------------------------------------------------------

_FULL_ORG_RE_CACHE: Optional[List[Tuple[str, re.Pattern]]] = None


def _get_full_org_res() -> List[Tuple[str, re.Pattern]]:
    """D-12：行政区划 × 组织形式后缀的整段匹配正则（按锚点长度降序）。"""
    global _FULL_ORG_RE_CACHE
    if _FULL_ORG_RE_CACHE is None:
        rules = (_load_config().get("org_name_rules") or {})
        admins = sorted({w for w in (rules.get("admin_divisions") or [])
                         if isinstance(w, str) and w}, key=len, reverse=True)
        suffixes = sorted({w for w in (rules.get("full_org_suffixes") or [])
                           if isinstance(w, str) and w}, key=len, reverse=True)
        res: List[Tuple[str, re.Pattern]] = []
        for admin in admins:
            if not suffixes:
                break
            suf_alt = "|".join(re.escape(s) for s in suffixes)
            # 字号 2–10 个汉字；后缀按长度降序（股份有限公司 先于 有限公司）
            res.append((admin, re.compile(
                rf"{re.escape(admin)}[一-龥]{{2,10}}(?:{suf_alt})")))
        _FULL_ORG_RE_CACHE = res
    return _FULL_ORG_RE_CACHE


def _apply_full_org_name_pass(text: str, counts: Dict[str, int]) -> str:
    """D-12：企业 / 机构全称整段脱敏（产出 ORG 占位符）。"""
    if not text or not isinstance(text, str):
        return text
    res = _get_full_org_res()
    if not res:
        return text
    mask = get_replacement("ORG", "XXXX")
    out = text

    def _repl(m: re.Match) -> str:
        frag = m.group(0)
        if _is_protected_by_place_guard(m.string, m.start(), m.end()):
            return frag
        # 幂等：已是占位符的区域不再处理
        if "X" in frag:
            return frag
        counts["组织名"] = counts.get("组织名", 0) + 1
        return mask

    for _admin, pat in res:
        out = pat.sub(_repl, out)
    return out


# ---------------------------------------------------------------------------
# D-8 派生（Issue #64）：机构简称派生
#
# 【问题】
#   机构全称清单（bank_entities）只覆盖银行全称；现实中大量文档以
#   **简称 + 机构通名**的形态出现（平安科技 / 平安金服 / 平安信用卡），
#   这些简称对定位具体机构同样敏感，却完全漏脱。
#
# 【口径】
#   ① abbreviation_derivations：「简称 + 指定机构通名」→ **只脱敏简称**、
#      保留通名（平安科技 → XX科技），与人工标准版一致；
#   ② standalone_abbreviations：独立出现的部门/机构简称 → 整词脱敏
#      （计财 → XXX）；但**后接部门通名**时属部门名、保留
#      （「营运计财部」是 generic_words 里的通用机构词，不脱敏）。
# ---------------------------------------------------------------------------

_ORG_ABBREV_CACHE: Optional[Dict[str, Any]] = None


def _get_org_abbrev_rules() -> Dict[str, Any]:
    global _ORG_ABBREV_CACHE
    if _ORG_ABBREV_CACHE is None:
        rules = (_load_config().get("org_name_rules") or {})
        derivations = rules.get("abbreviation_derivations") or {}
        norm: Dict[str, List[str]] = {}
        for abbr, sufs in derivations.items():
            if not isinstance(abbr, str) or not abbr:
                continue
            norm[abbr] = sorted({s for s in (sufs or []) if isinstance(s, str) and s},
                                 key=len, reverse=True)
        _ORG_ABBREV_CACHE = {
            "derivations": norm,
            "standalone": sorted({w for w in (rules.get("standalone_abbreviations") or [])
                                  if isinstance(w, str) and w}, key=len, reverse=True),
        }
    return _ORG_ABBREV_CACHE


def _apply_org_abbrev_pass(text: str, counts: Dict[str, int]) -> str:
    """D-8 派生：机构简称脱敏（简称 + 通名 / 独立简称）。"""
    if not text or not isinstance(text, str):
        return text
    rules = _get_org_abbrev_rules()
    derivations = rules["derivations"]
    standalone = rules["standalone"]
    if not derivations and not standalone:
        return text
    mask = get_replacement("ORG", "XXXX")
    out = text

    # ① 简称 + 指定机构通名 → 只脱敏简称
    for abbr, sufs in sorted(derivations.items(), key=lambda kv: -len(kv[0])):
        for suf in sufs:
            token = abbr + suf
            idx = 0
            while True:
                i = out.find(token, idx)
                if i < 0:
                    break
                idx = i + len(token)
                if _is_protected_by_place_guard(out, i, i + len(token)):
                    continue
                if i > 0 and (out[i - 1] in "Xx"
                              or re.match(r"[0-9A-Za-z]", out[i - 1])):
                    continue
                out = out[:i] + mask + out[i + len(abbr):]
                counts["组织名"] = counts.get("组织名", 0) + 1
                idx = i + len(mask)

    # ② 独立简称
    for abbr in standalone:
        idx = 0
        while True:
            i = out.find(abbr, idx)
            if i < 0:
                break
            j = i + len(abbr)
            idx = j
            if _is_protected_by_place_guard(out, i, j):
                continue
            # 后接部门通名 → 属通用部门名（营运计财部），保留
            if j < len(out) and out[j] in "部处科室所":
                continue
            if i > 0 and (out[i - 1] in "Xx"
                          or re.match(r"[0-9A-Za-z]", out[i - 1])):
                continue
            if j < len(out) and re.match(r"[0-9A-Za-z]", out[j]):
                continue
            out = out[:i] + mask + out[j:]
            counts["组织名"] = counts.get("组织名", 0) + 1
            idx = i + len(mask)
    return out


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

    # §3.4（Issue #53）通道互斥断言：银行名/机构名判定链命中的区间锁定，
    #   姓名通道不得再对其内部做任何替换（「XX银行北京分行」不被拆成
    #   「XX银行北XXX行」）。
    locked_spans = _bank_chain_locked_spans(text)

    def _repl(m: re.Match) -> str:
        if locked_spans and _is_in_locked_span(locked_spans, m.start(), m.end()):
            return m.group(0)
        # 姓名形态须**全为汉字**（Issue #49 M9 遗留约束 / Issue #54 幂等性）。
        # 命中片段含 ASCII 字母 → 说明规则把**占位符**（XXX / YYYY）当成
        # 名字用字吞掉了（"演XXX和XXX线" → "演XXXXXX线"），
        # 这既会误伤已脱敏文本，也是二次脱敏不幂等的根因。
        # 英文人名由独立的英文通道处理，不走本通道。
        if re.search(r"[A-Za-z]", m.group(0)):
            return m.group(0)
        if _is_protected_by_common_word(m.string, m.start(), m.end()):
            return m.group(0)
        # 独立成词豁免（Issue #49 复评处置口径：「叶子」独立成词不做姓名脱敏，
        # 但「叶子涵」作为姓名仍须脱敏）——须排在整词保护表**之前**，
        # 否则「叶子」若被加入整词保护表，「叶子涵」会被连带放过。
        if _is_protected_by_standalone_exempt(m.string, m.start(), m.end()):
            return m.group(0)
        # R-㊷：业务词保护守卫（G1 跨界 / G2 整体 / G3 指示词）
        if _is_protected_by_biz_word(m.string, m.start(), m.end()):
            return m.group(0)
        # G4 整词保护表（Issue #51）：与真实业务术语重叠 → 保留原文
        if _is_protected_by_common_word_guard(m.string, m.start(), m.end()):
            return m.group(0)
        # G7 跨词边界守卫（Issue #51）：命中横跨在两个词的边界上 → 保留原文
        if _is_boundary_split_misfire(m.string, m.start(), m.end()):
            # Issue #75：G7「修剪回退」——命中片段里若含**已知真名**，
            # 只脱敏该真名，被贪婪吞掉的尾字原样保留（尾字属于下一个词），
            # 而不是整段回退导致人名整段泄漏。
            trimmed = _trimback_redact(m, replacement, counts)
            if trimmed is not None:
                return trimmed
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

    # D-9（Issue #63）：独立数字口令——须在其它规则之前执行，
    # 以便看到未经改写的原始数字（日期形态识别依赖原始串）
    result = _apply_digital_password_pass(result, counts)

    # D-8（Issue #64）：银行全称/简称整段脱敏 + 通用部门名整段保护
    # （保护占位符在流水线末尾统一还原）
    result = _apply_org_entity_pass(result, counts)

    # D-7（Issue #64）：L3 机构实体层 —— 白名单机构实体整段脱敏
    # 【时机】须在**通用机构名通道之前**——否则「营运管理部」会被兜底分支
    #   切成「营运 + 管理部」，产生「营运XXXX」混合残态；
    # 脱敏结果以哨兵包裹，流水线末尾统一还原为 ORG 占位符。
    result = _apply_org_entity_mask_pass(result, counts)

    # D-11 补强（Issue #63 批次四）：专名网点「非清单语境保留」
    # 【时机】须在**所有会切分机构名的通道之前**——组织名通道（营业部/
    #   部/中心）、地址通道（回溯式收拢）、姓名通道（专名误吞为 XXX）
    #   都会把机构名切碎，故先以哨兵整体占位，末尾统一还原。
    result = _apply_named_org_keep_pass(result, counts)

    # 第一阶段：姓名之前的规则（邮箱/日期/银行/支行/地名等）
    for pattern, replacement in pre_patterns:
        result = _counting_sub(pattern, result, replacement, counts)

    # D-12（Issue #63）：企业 / 机构全称整段脱敏
    # 【时机】须在**银行名规则之后**（避免与银行全称抢口径，如
    #   「福建海峡银行股份有限公司」应由银行通道处理为「XX银行」），
    #   且须在**地址通道之前**（否则行政区划前缀被地址通道先吃掉，
    #   企业全称将退化为「XX市XX」混合残态）。
    result = _apply_full_org_name_pass(result, counts)

    # R-⑮（v1.3.7，Issue #13-③）：无区号 7–8 位本地号码（上下文限定）
    # 须在地址通道之前执行——否则"电话：87517381"等可能被其它规则先行切分
    result = _apply_local_phone_pass(result, counts)

    # 第二阶段：回溯式地址脱敏通道（省/市/县/区/街道/路/楼盘/大厦/支行/门牌）
    # 必须在姓名规则之前执行，防止"金水/花园"等地址成分被姓名规则误吞
    result = _apply_address_pass(result, stats=counts)

    # D-11（Issue #63）：地名 / 网点名 / 职场名脱敏（词典驱动）
    # 【时机】须在**姓名规则之前**——否则「连江」会被姓名通道误判成人名
    #   （实测 '连江' → 'XXX'），既造成分类错误又与人工标准版不符；
    #   同时在**地址通道之后**——地址通道已按回溯口径处理了
    #   「地名 + 支行/分行/营业部」等网点名（漳平支行 → XX支行）。
    result = _apply_place_site_pass(result, counts)

    # D-8 派生（Issue #64）：机构简称脱敏（平安科技 → XX科技）
    # 须在**姓名规则之前**——否则简称会被姓名通道先行误拆。
    result = _apply_org_abbrev_pass(result, counts)

    # 第三阶段（0）：职务与称谓式人名通道（Issue #52）
    # 须在通用姓名规则**之前**执行——"李工/陈总/张经理"等组合形态若先被
    # 通用姓名规则切开，会同时产生漏脱与误伤（#52 §一 案例 #20）。
    result = _apply_title_name_pass(result, counts)

    # D-10（Issue #62）：正文叙述型人名「姓名 + 职务后缀」召回通道
    # 须在**姓名规则组之前**执行——否则"孙海刚行长"会先被通用姓名规则
    # 切成"孙XXX行长"，留下残尾且与标准版的"XXX行长"形态不一致。
    result = _apply_narrative_title_pass(result, counts)

    # N-C 区域内逐项姓名判定（Issue #50 §4.8）
    # 规则A 退出主链路后，由本通道承接其召回职责：
    # 先定位姓名集中区（T2 名单段 / T3 签名区），再在区域内逐项做形态判定。
    #
    # 【执行时机：必须在姓名规则组之前】
    # 区域检测依赖**原始名单结构**（同构 + 锚点词）。若等到姓名规则跑完再检测，
    # 名单里已脱敏的项变成占位符 XXX，同构性被破坏 → 区域检不出 → 漏网。
    # 实测（平安信用卡 PPT 金标）：规则层跑完后 T2 检出 0 个区域，
    # 而原始文本上可检出含「王松/吴学亮/叶新林/马娟」的名单段。
    result = _apply_nc_region_name_pass(result, counts)

    # 通用人名词典层（Issue #55）——须在**姓名规则组之前**执行。
    # 本层补齐「孤立 + 库外 + 短名」缺口（如「王明」「张明」「李雷」）。
    # 置于 D16 之前的原因：D16 的 2 字名层会把「韩梅梅」截成「韩XXX」，
    # 本层先整体脱敏可避免该类残缺输出。
    result = _apply_common_name_dict_pass(result, counts)

    # D-5（Issue #60 方案 2）：角色槽位内姓名强判别
    # 须在**姓名规则组之前**执行——否则「刘兆缋」会先被规则切成「刘兆」
    # 而留下残尾「缋」（部分脱敏残留）
    result = _apply_slot_strong_name_pass(result, counts)

    # D-5 补强（Issue #60）：确认真名清单的精确匹配通道
    # 须在**姓名规则组之前**执行——否则 3 字名会被 D16 左边界拦下、
    # 或被切成「刘兆」留下残尾「缋」。
    result = _apply_extra_name_pass(result, counts)

    # 第三阶段：姓名规则（R1：带常用词覆盖保护，"说明书→说XXX"类误伤在应用层过滤）
    _name_repl = get_replacement("NAME", "XXX")
    for pattern, replacement in name_patterns:
        result = _name_guard_sub_counted(pattern, result, replacement, counts)

    # D-4 补丁（Issue #61-B）：通用机构词保留后，紧邻的人名仍须脱敏
    # （须在姓名规则组之后执行——已被遮盖的姓名此时已是 XXX，不会被二次命中）
    result = _apply_org_generic_name_pass(result, counts)

    # D-6（Issue #60 方案 1）：GB2312 生僻名用字候选池兜底层
    # 【时机】须在**姓名规则组之后**——既有规则能整段脱敏的姓名（王倩茹
    #   「王倩」+温字尾等）已先被遮盖，本层只看**闸门放不出的残留**；
    #   若本层先行，会把三字名切成两字（制造 #57 口径的「残尾」形态）。
    result = _apply_rare_name_pass(result, counts)

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
        # 【幂等性守卫（Issue #54）】XX 若属于更长的占位符连写（XXX / XXXX），
        #   说明它**本身就是已脱敏姓名**的占位符，而不是地址通道截断姓名后
        #   留下的两字占位符——此时不得再合并，否则二次脱敏会把
        #   "地XXX和XXX持" 合并成 "地XXXXXX持"（越脱越少，且不可逆）。
        _in_long_placeholder = (
            (_idx > 0 and result[_idx - 1] == 'X')
            or (_idx + 2 < len(result) and result[_idx + 2] == 'X')
        )
        if _in_long_placeholder:
            _pos = _idx + 2
            continue
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

    # D-8（Issue #64）：还原通用部门名保护占位符（流水线末尾统一还原）
    result = _restore_org_entity_words(result)

    # D-7（Issue #64）：还原机构实体白名单脱敏哨兵
    result = _restore_org_entity_layer(result)

    # D-11 补强（Issue #63 批次四）：还原专名网点保护占位符
    result = _restore_named_org_keep(result)

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
# XML 感知脱敏（P0：整文件级兜底通道破坏 XML 结构）
#
# 背景
# ----
# PPT / Word / Excel 处理器的「整文件级兜底」通道会对 XML 原文整体跑正则。
# XML 原文里除文本节点外，还含大量标签与属性，其中属性名也可能命中脱敏规则：
#
#     <p:transition spd="slow" p14:dur="2000"/>
#              ↑ 属性名 "spd" 命中拉丁银行缩写 SPD（上海浦东发展银行）
#              → 被替换为 "[Bank]"，属性名变为 "[Bank]"="slow"
#
# 结果是 XML 结构非法，产物 PPTX/DOCX 无法被 Office 与 python-pptx/python-docx
# 打开——交付级致命缺陷（实测：平安信用卡 PPTX 12 个 slide XML 损坏）。
#
# 对策（保结构 + 保上下文）
# --------------------------
# 【不能简单按节点拆分】实测：把 XML 拆成「文本节点/属性值」逐段跑规则，
# 姓名召回从 442 处掉到 63 处——因为部分检测通道（N-C 名单区域检测、
# 上下文护栏）依赖**文档级上下文**，逐节点处理会失去跨节点视野而漏脱。
#
# 因此采用「哨兵保护」方案：
#   ① 先把 XML 中的**标签名**与**属性名**替换为不可被规则命中的哨兵串；
#   ② 对整份（已保护结构的）XML 跑一次完整脱敏 —— 上下文完整保留，
#      文本节点与属性值照常脱敏；
#   ③ 事后把哨兵还原为原始名称。
# 这样既不破坏 XML 结构，又不损失任何召回能力。
# ---------------------------------------------------------------------------

_XML_TAG_RE = re.compile(r"<[^>]*>", re.S)
_XML_ATTR_RE = re.compile(
    r"""(\s*)([^\s=/>]+)(\s*=\s*)("[^"]*"|'[^']*')"""
)
# 哨兵：含控制字符，任何脱敏规则都不会命中（规则面向可见文本）
_SENTINEL = "\x00WBX{}\x00"


def _protect_xml_structure(content: str) -> Tuple[str, Dict[str, str]]:
    """
    把 XML 中的标签名与属性名替换为哨兵，返回 (保护后内容, {哨兵: 原名})。
    属性值与文本节点原样保留。

    例外：`xmlns:*` 命名空间声明**整体保护**（连同其值）——
    命名空间 URI 是结构标识而非业务内容，若参与脱敏：
      ① URI 中的银行缩写（如 urn:abc）会被误判为银行名而破坏 URI 合法性；
      ② 破坏后 XML 无法解析。
    """
    mapping: Dict[str, str] = {}
    counter = [0]

    def _sentinel(original: str) -> str:
        key = _SENTINEL.format(counter[0])
        counter[0] += 1
        mapping[key] = original
        return key

    out: List[str] = []
    pos = 0
    for m in _XML_TAG_RE.finditer(content):
        out.append(content[pos:m.start()])
        tag = m.group(0)
        # 标签名：<name ...> 或 </name>
        tm = re.match(r"</?([^\s/>]+)", tag)
        if tm:
            name = tm.group(1)
            out.append(tag[:tm.start(1)] + _sentinel(name))
            rest = tag[tm.end(1):]
        else:
            rest = tag
        # 属性名（xmlns 声明整体保护）
        def _attr_sub(am: "re.Match") -> str:
            lead, attr_name, eq, quoted = am.group(1), am.group(2), am.group(3), am.group(4)
            if attr_name == "xmlns" or attr_name.startswith("xmlns:"):
                # 命名空间声明：名称与值整体保护
                return f"{lead}{_sentinel(am.group(0).strip())}"
            return f"{lead}{_sentinel(attr_name)}{eq}{quoted}"

        rest = _XML_ATTR_RE.sub(_attr_sub, rest)
        out.append(rest)
        pos = m.end()
    out.append(content[pos:])
    return "".join(out), mapping


def apply_redactions_xml_aware(content: str) -> Tuple[str, Dict[str, int]]:
    """
    XML 感知的整文件脱敏：
      - 标签名与属性名：保护（哨兵），不参与替换，保证 XML 结构合法
      - 文本节点与属性值：正常脱敏，保留文档级上下文（不拆节点）

    返回 (脱敏后的 XML, 分类计数)。
    """
    counts: Dict[str, int] = {}
    if not content or not isinstance(content, str):
        return content, counts

    # 快速判定：不含 '<' 时按纯文本处理
    if "<" not in content:
        return apply_redactions_counted(content)

    protected, mapping = _protect_xml_structure(content)
    redacted, counts = apply_redactions_counted(protected)
    for key, original in mapping.items():
        redacted = redacted.replace(key, original)
    return redacted, counts


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
    """重置规则缓存（config.json 变更后需调用）。

    【历史缺陷修复】此前本函数只清 `_PATTERNS`，而十余个**配置派生缓存**
    （保护词表、业务词表、全名集、名字池、称谓通道正则等）在配置切换后
    仍保留旧值——导致「切到假配置再切回真配置」后，真配置里的保护词
    （如「白名单」）再也加载不到，业务词被整段误伤。
    本函数现统一清空全部配置派生缓存，使配置切换真正生效。
    """
    global _PATTERNS
    global _FULL_NAME_SET_CACHE
    global _BIZ_GUARD_WORDS
    global _BIZ_GUARD_MAXLEN
    global _BIZ_WORDS_BY_FIRST
    global _COMMON_WORD_GUARDS
    global _COMMON_WORD_GUARDS_MAXLEN
    global _COMMON_WORD_GUARD_SUBSTRINGS
    global _BOUNDARY_PAIRS
    global _STANDALONE_EXEMPT_WORDS
    global _STANDALONE_NAME_CANDIDATES
    global _ENGLISH_TERM_WHITELIST
    global _TITLE_NAME_CACHE
    global _TITLE_SUFFIX_RE_CACHE
    global _TITLE_NICK_RE_CACHE
    global _TITLE_SINGLE_RE_CACHE
    global _ATTESTED_SURNAMES_CACHE
    global _SAFE_CUT_WORDS
    global _COMMON_NAME_DICT
    global _NAME_POOL_SET_CACHE
    global _ROLE_LABEL_PATTERNS
    global _ROLE_SUFFIX_SORTED
    global _ORG_GEN_NAME_CTX
    global _PLACE_RULES_CACHE
    global _FULL_ORG_RE_CACHE
    global _ORG_ABBREV_CACHE
    global _ORG_LAYER_COMBO_RE
    global _ORG_LAYER_COMBO_MAXLEN
    global _ORG_LAYER_COMBO_CFG_ID
    global _GB2312_CHARS_CACHE
    global _RARE_NAME_LAYER_CACHE

    _PATTERNS = None
    _FULL_NAME_SET_CACHE = None
    _BIZ_GUARD_WORDS = None
    _BIZ_GUARD_MAXLEN = 0
    _BIZ_WORDS_BY_FIRST = None
    _COMMON_WORD_GUARDS = None
    _COMMON_WORD_GUARDS_MAXLEN = 0
    _COMMON_WORD_GUARD_SUBSTRINGS = None
    _BOUNDARY_PAIRS = None
    _STANDALONE_EXEMPT_WORDS = None
    _STANDALONE_NAME_CANDIDATES = None
    _ENGLISH_TERM_WHITELIST = None
    _TITLE_NAME_CACHE = None
    _TITLE_SUFFIX_RE_CACHE = None
    _TITLE_NICK_RE_CACHE = None
    _TITLE_SINGLE_RE_CACHE = None
    _ATTESTED_SURNAMES_CACHE = None
    _SAFE_CUT_WORDS = set()
    _COMMON_NAME_DICT = None
    _NAME_POOL_CACHE.clear()
    _NAME_CHAR_FREQ_CACHE.clear()
    _NAME_POOL_SET_CACHE = None
    _ROLE_LABEL_PATTERNS = None
    _ROLE_SUFFIX_SORTED = None
    _ORG_GEN_NAME_CTX = None
    # 【批次四新增】D-11 地名/网点词典、D-12 企业全称正则、D-8 简称派生规则
    # 均为**配置派生缓存**，必须一并清空——否则「切到假配置再切回真配置」
    # 后，真配置里的地名/机构词再也加载不到（与本函数文档中的历史缺陷同类）。
    _PLACE_RULES_CACHE = None
    _FULL_ORG_RE_CACHE = None
    _ORG_ABBREV_CACHE = None
    # 【D-7 新增】机构实体层组合正则（通名 × 后缀）同样是配置派生缓存
    _ORG_LAYER_COMBO_RE = None
    _GB2312_CHARS_CACHE = {}
    _RARE_NAME_LAYER_CACHE = {}
    _ORG_LAYER_COMBO_MAXLEN = 0
    _ORG_LAYER_COMBO_CFG_ID = 0


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
    """统计文本中各类敏感信息的出现次数。

    【修复说明】
      原实现用「规则序号 → REDACTION_LABELS.values() 下标」映射类别标签，
      但生效规则数（40+）与标签表条目数（14）根本不对齐，且残留一段
      `for (key,), (pattern, _) in zip(...)` 的死代码——该解包对
      `[[k] for k in ...]` 与 `[[p] for p in ...]` 恒抛 ValueError，
      导致本函数**任何输入都直接崩溃**（下游 `redact_excel` /
      `redact_all` 若调用将整链失败）。

      现改为复用 `apply_redactions_counted`——它按**实际生效的替换动作**
      逐处计数并由 `_label_for_replacement` 归类，口径与真实脱敏结果一致。
    """
    if not text:
        return {}

    _, counts = apply_redactions_counted(text)
    return dict(counts)


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
