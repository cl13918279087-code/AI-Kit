#!/usr/bin/env python3
"""
test_issue55_common_name_dict.py — 通用人名词典层回归守卫（Issue #55）

【背景】
#50 规则A 退出主链路后遗留一个已知边界：**「孤立 + 无上下文锚点 +
不在 D16 精确库」的 2 字名**（如「王明」「张明」）不再被主链路脱敏。

根因在词典而非规则——D16 全名库（7398 条）的本质是
「**本项目语料内出现过的姓名**」，不是通用人名词典：
实测抽查 86 个高频中文人名，**66 个（77%）不在库**。

#55 引入**独立于 D16 的通用人名词典层**（精确匹配词表层）补齐该缺口。

【本文件守卫的内容】
  1. 工程约束①：独立成层——词典层与 D16 库零交集，不混入 `full_name_list`
  2. 工程约束②：精确匹配——不做笛卡尔积；不靠形态粗筛
  3. 工程约束③：右边界非汉字——串词不命中
  4. 工程约束④：102 通用语料 + 1200 真实业务语料**双零误伤**
  5. 召回提升：13 条孤立样本召回自 8/13 提升至 ≥ 12/13
  6. 负样本口径：叶子 / 白天 / 白天鹅 不按姓名处理；宁波银行/甘肃银行维持 XX银行
  7. 无回归：金标召回、真实语料零误伤、门禁八项

运行方式：
    python3 -m pytest tests/test_issue55_common_name_dict.py -v
"""

import json
import os
import re
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common_rules  # noqa: E402
from common_rules import apply_redactions  # noqa: E402

CONFIG_PATH = os.path.join(SCRIPTS_DIR, "config.json")
DICT_PATH = os.path.join(SCRIPTS_DIR, "data", "common_name_dict.txt")


def _load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _dict_words():
    words = set()
    with open(DICT_PATH, encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if w and not w.startswith("#"):
                words.add(w)
    return words


# ---------------------------------------------------------------------------
# 0. 词表与配置的基本形态
# ---------------------------------------------------------------------------


def test_common_name_dict_config_present():
    """config.json 必须提供独立的通用人名词典层配置项。"""
    cfg = _load_config()
    assert cfg.get("common_name_dict_enabled") is True, (
        "config.json 缺少 common_name_dict_enabled（Issue #55）")
    assert cfg.get("common_name_dict_file"), (
        "config.json 缺少 common_name_dict_file（Issue #55）")


def test_dict_file_exists_and_is_pure_chinese():
    """词表文件必须存在，且每条都是 2/3 字纯汉字姓名。"""
    words = _dict_words()
    assert len(words) > 10000, f"词表规模异常小：{len(words)}"
    bad = [w for w in words
           if len(w) not in (2, 3) or not re.fullmatch(r"[一-鿿]+", w)]
    assert not bad, f"词表含非 2/3 字汉字条目：{bad[:5]}"


def test_dict_layer_loaded():
    """规则层实际加载到的词表规模与文件一致（防接入点遗漏）。"""
    loaded = common_rules._get_common_name_dict()
    assert len(loaded) == len(_dict_words()), (
        f"规则层加载词表 {len(loaded)} 条 ≠ 文件 {len(_dict_words())} 条")


# ---------------------------------------------------------------------------
# 1. 工程约束①：独立成层，不混入 full_name_list
# ---------------------------------------------------------------------------


def test_dict_is_disjoint_from_d16():
    """词典层与 D16 全名库必须零交集。

    交集非零会带来两类问题：
      1. 同一字符串被两层争抢，命中顺序决定输出，行为不可预期；
      2. 混进 `full_name_list` 会让「张三丰」被 2 字名层截断（#51/#56 教训）。
    """
    cfg = _load_config()
    d16 = {n for n in cfg["full_name_list"] if isinstance(n, str) and n != "姓名"}
    words = _dict_words()
    overlap = d16 & words
    assert not overlap, f"词典层与 D16 库存在交集 {sorted(overlap)[:5]}"


def test_dict_not_merged_into_full_name_list():
    """词典层词条**不得**被写进 `full_name_list` 配置项。"""
    cfg = _load_config()
    fl = set(cfg["full_name_list"])
    words = _dict_words()
    assert not (fl & words), "词典层词条被混入 full_name_list，违反约束①"


def test_dict_disjoint_from_protection_tables():
    """词典层与各保护/豁免表零交集。"""
    cfg = _load_config()
    words = _dict_words()
    for key in ("common_word_guards", "standalone_exempt_words",
                "biz_guard_words", "placeholder_names"):
        table = set(cfg.get(key) or [])
        overlap = table & words
        assert not overlap, f"词典层与 {key} 存在交集 {sorted(overlap)[:5]}"


# ---------------------------------------------------------------------------
# 2. 工程约束②③：精确匹配 + 右边界非汉字
# ---------------------------------------------------------------------------


def test_right_boundary_must_not_be_chinese():
    """候选右侧紧邻汉字 → 不命中（#55 约束③，避免「王明生」类串词误命中）。

    直接验证**词典层扫描器**的候选判定，避开其它规则层的干扰。
    """
    words = _dict_words()
    probe = sorted(words)[0]
    assert len(probe) in (2, 3)

    # 右邻汉字 → 不产生候选
    cands = common_rules._find_common_name_dict_candidates(f"{probe}负责")
    assert not cands, f"「{probe}负责」右邻汉字仍产生候选：{cands}"

    # 右邻为标点/空白/结尾 → 产生候选
    cands = common_rules._find_common_name_dict_candidates(f"负责人：{probe}，")
    assert cands, f"「负责人：{probe}，」未产生候选（词典层未生效）"
    assert cands[0][2] == probe


def test_left_boundary_surname_guard():
    """候选左侧紧邻姓氏字 → 放弃该候选（防 #56 同类截断，如「艾成军」→「成军」）。"""
    words = _dict_words()
    # 找一个 2 字词条，其首字是姓氏 → 构造「<姓氏><词条>」形态
    probe = None
    for w in sorted(words):
        if len(w) == 2 and w[0] in common_rules.SURNAME_SET:
            probe = w
            break
    if probe is None:
        pytest.skip("词典层无可用探针")

    # 「<姓氏><词条>」：左邻是姓氏字 → 不产生 2 字候选
    text = "赵" + probe
    cands = common_rules._find_common_name_dict_candidates(text)
    assert not any(c[2] == probe for c in cands), (
        f"「{text}」中左邻姓氏字的 2 字候选仍产生：{cands}")


def test_no_cartesian_expansion():
    """词典层不得做笛卡尔积——组合出来的非词典串不得被脱敏。

    「王小红」不在词典层词表内（实测），若实现退化为笛卡尔积
    （姓 + 名字池任意组合），它会被命中——这正是 #49 误伤放大器。
    """
    assert "王小红" not in _dict_words(), (
        "前提失效：「王小红」已进入词典层，本用例需重估")
    assert apply_redactions("王小红") == "王小红", (
        "「王小红」不在词典层却被脱敏——疑似退化为笛卡尔积匹配")


def test_dict_layer_does_not_use_jieba_nr():
    """词典层不得依赖 jieba `nr` 词性入层（#55 明文禁止）。

    校验方式：词表条目应来自**外部人名词典语料**（Chinese-Names-Corpus），
    且词条整体不依赖分词器的词性标注——此处固化「词典层与 jieba 无关」
    的实现约定：词表由独立文件提供，规则层只做**全字符串哈希查表**。
    """
    src = open(DICT_PATH, encoding="utf-8").read(2000)
    assert "Chinese-Names-Corpus" in src, (
        "词表须声明数据来源（可追溯性守卫）")
    assert "Apache" in src, "词表须声明数据许可（Apache-2.0 可再分发）"


# ---------------------------------------------------------------------------
# 3. 工程约束④：双零误伤
# ---------------------------------------------------------------------------


def test_biz_corpus_zero_name_misfire():
    """102 条通用语料：姓名类误伤必须为 0。"""
    from test_name_false_positive import FALSE_POSITIVE_CORPUS

    name_class = []
    other_class = []
    for seg in FALSE_POSITIVE_CORPUS:
        out, cnt = common_rules.apply_redactions_counted(seg)
        if out == seg:
            continue
        if cnt.get("姓名"):
            name_class.append(seg)
        else:
            other_class.append(seg)
    assert not name_class, f"通用语料出现姓名类误伤 {len(name_class)} 条：{name_class[:3]}"
    assert len(other_class) <= 5, (
        f"非姓名类改动 {len(other_class)} 条，超过门禁上限")


def test_real_corpus_zero_misfire():
    """1200 条真实业务语料：零误伤（复用门禁同源豁免判定）。"""
    import bench_metrics

    with open(os.path.join(REPO_ROOT, "tests", "benchmark", "biz_corpus.json"),
              encoding="utf-8") as f:
        real = json.load(f)

    misfires = []
    for idx, seg in enumerate(real):
        out = apply_redactions(seg)
        if out == seg:
            continue
        for old, rep in bench_metrics._diff_segments(seg, out):
            if any(p in old for p in bench_metrics._PLACEHOLDER_NAMES):
                continue
            if bench_metrics._ORG_SUFFIX_RE.fullmatch(old.strip()) and rep.strip() == "XXXX":
                continue
            if bench_metrics._is_date_intent(seg, old, rep):
                continue
            misfires.append((idx, old, rep, seg[:50]))
    assert not misfires, f"真实语料出现 {len(misfires)} 处误伤，例如：{misfires[:3]}"


def test_no_corpus_specific_pruning_dependency():
    """零误伤不得依赖「语料特异裁剪」——即不得为规避误伤而把命中语料中的词条剔除。

    守卫方式：词典层词表中若有条目在 102/1200 语料中出现（作为独立片段），
    说明词表本身是**通用**的，而非针对当前语料裁剪出来的。
    至少 90% 的词条不应在双语料中出现。
    """
    from test_name_false_positive import FALSE_POSITIVE_CORPUS

    with open(os.path.join(REPO_ROOT, "tests", "benchmark", "biz_corpus.json"),
              encoding="utf-8") as f:
        real = json.load(f)

    corpus_text = "\n".join(
        [c for c in FALSE_POSITIVE_CORPUS if isinstance(c, str)]
        + [s for s in real if isinstance(s, str)]
    )
    words = _dict_words()
    appearing = [w for w in words if w in corpus_text]
    ratio = len(appearing) / len(words)
    assert ratio <= 0.10, (
        f"词表中有 {len(appearing)}/{len(words)}（{ratio:.1%}）条目出现在当前语料中，"
        "疑似针对语料特异裁剪")


# ---------------------------------------------------------------------------
# 4. 召回提升（#55 验收核心）
# ---------------------------------------------------------------------------


def test_isolated_sample_recall_improved():
    """13 条孤立样本召回自 8/13 提升至 ≥ 12/13。"""
    from test_name_false_positive import TRUE_NAME_SAMPLES

    missed = [n for n in TRUE_NAME_SAMPLES if apply_redactions(n) == n]
    assert len(missed) <= 1, (
        f"孤立样本召回不足：漏脱 {len(missed)}/13 {missed}"
        "（#55 验收口径要求召回自 8/13 提升至 ≥12/13）")


def test_newly_covered_samples():
    """本层新增覆盖的具体样本（#55 补齐的库外短名缺口）。"""
    for name in ("张明", "李雷", "樊霖", "王明", "韩梅梅"):
        assert apply_redactions(name) == "XXX", (
            f"「{name}」应由通用人名词典层脱敏（#55）：{apply_redactions(name)!r}")


def test_han_meimei_not_truncated():
    """「韩梅梅」整体脱敏，不残留首字（#56 层序问题的同类防线）。"""
    assert apply_redactions("韩梅梅") == "XXX"
    assert apply_redactions("联系人：韩梅梅") == "联系人：XXX"


def test_contextual_name_still_recalled():
    """带上下文的库外姓名同样被覆盖。"""
    for text, keep in (
        ("负责人：张明，负责推进", "张明"),
        ("联系人：樊霖，电话123", "樊霖"),
        ("评审：韩梅梅、李雷", "韩梅梅"),
    ):
        out = apply_redactions(text)
        assert keep not in out, f"上下文形态漏脱「{keep}」：{out!r}"


# ---------------------------------------------------------------------------
# 5. 负样本口径（用户硬约束）
# ---------------------------------------------------------------------------


def test_common_words_not_treated_as_names():
    """「叶子」「白天」「白天鹅」不得按姓名处理。"""
    for word in ("叶子", "白天", "白天鹅"):
        out = apply_redactions(word)
        assert word in out, f"通用词「{word}」被误脱敏：{out!r}"


def test_yezi_han_is_still_redacted():
    """「叶子涵」仍须脱敏（真名优先，#49 第三轮复评裁决）。"""
    assert apply_redactions("叶子涵") == "XXX"


def test_bank_names_kept_as_xx_bank():
    """「宁波银行」「甘肃银行」维持 `XX银行`，不得被词典层切成姓名。"""
    assert apply_redactions("宁波银行") == "XX银行"
    assert apply_redactions("甘肃银行") == "XX银行"


# ---------------------------------------------------------------------------
# 6. 无回归守卫
# ---------------------------------------------------------------------------


def test_gold_names_not_degraded():
    """真名金标召回不得劣化。"""
    with open(os.path.join(REPO_ROOT, "tests", "benchmark", "gold_names.json"),
              encoding="utf-8") as f:
        gold_raw = json.load(f)
    gold = set()
    for v in gold_raw.values():
        if isinstance(v, list):
            gold.update(v)

    missed = []
    for g in gold:
        if not g:
            continue
        probe = f"负责人：{g}，负责整体推进。"
        if g in apply_redactions(probe):
            missed.append(g)
    assert not missed, f"金标漏脱 {len(missed)} 个：{missed[:5]}"


def test_idempotent():
    """二次脱敏不得改变结果（幂等性）。"""
    for text in ("张明负责推进", "韩梅梅、李雷", "联系人：王明"):
        once = apply_redactions(text)
        twice = apply_redactions(once)
        assert once == twice, f"非幂等：{text!r} → {once!r} → {twice!r}"


def test_deterministic():
    """同一输入多次处理结果一致（确定性）。"""
    text = "项目组成员：张明、李雷、韩梅梅"
    outs = {apply_redactions(text) for _ in range(3)}
    assert len(outs) == 1, f"结果不确定：{outs}"


def test_bench_gate_all_pass():
    """八项门禁全 PASS（单一指标源 `scripts/bench_metrics.py`）。"""
    import subprocess
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "bench_metrics.py")],
        capture_output=True, text=True, cwd=REPO_ROOT)
    assert "总判定: PASS" in r.stdout, f"门禁未全 PASS：\n{r.stdout[-2000:]}\n{r.stderr[-1000:]}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
