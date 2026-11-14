# -*- coding: utf-8 -*-
"""
Issue #64：D-8 机构简称派生 + D-7 机构实体层 + D-14 通用词守卫判定域。

【背景】
#64 §四 的五条验收中，两条此前只有「机制存在」而缺少可复跑的断言固化：
  · 验收 1：白名单机构实体（营运管理部）整段脱敏、通用词层与实体层互斥；
  · 验收 5：简称派生清单接入后，平安 / 工商 / 建行 / 计财 按全称口径脱敏。

本轮（批次十）另落地 **D-14**：姓名规则的**词级守卫判定域**由「整段命中」
改为「实际被遮盖的那一段」（即含锚点/职务词的规则取 `name` 分组）。
整改前「负责人 + 管理」这类结构会把通用词「管理」整段吞掉
（G6 尖刀组 UAT3 规划中 8 处 over），整改后通用词保留、锚点语义不变。

【本文件的断言】
  1. 验收 1：白名单机构实体整段脱敏、无混合态、与通用词层互斥；
  2. 验收 5：平安 / 工商 / 建行 / 计财 的简称口径；
  3. D-14：锚点后的通用词不再被误吞，且**真名仍被脱敏**（不回退）；
  4. 通用部门名（短信中心 / 运营中心）保留原文；
  5. 保护表与真名库**双向互斥**（新增词不得引入真名漏脱）。

运行：
    python3 -m pytest tests/test_issue64_d8_alias_derive.py -v
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import common_rules  # noqa: E402

CONFIG_PATH = os.path.join(SCRIPTS, "config.json")


def _read_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _reload(cfg=None, path=None):
    if path is not None:
        os.environ["AIKIT_CONFIG_PATH"] = path
    common_rules._CONFIG_CACHE = cfg
    common_rules.reset_patterns()


def _redact(text):
    return common_rules.apply_redactions(text)


@pytest.fixture(scope="module")
def default_cfg():
    old = os.environ.pop("AIKIT_CONFIG_PATH", None)
    _reload(None, None)
    yield _read_config()
    if old is None:
        os.environ.pop("AIKIT_CONFIG_PATH", None)
    else:
        os.environ["AIKIT_CONFIG_PATH"] = old
    common_rules._CONFIG_CACHE = None
    common_rules.reset_patterns()


@pytest.fixture
def whitelist_cfg():
    """注入「营运管理部 / 行政部 / 信息科技部」白名单的配置。"""
    cfg = _read_config()
    layer = cfg["org_name_rules"]["org_entity_layer"]
    layer["entity_whitelist"] = ["营运管理部", "行政部", "信息科技部"]
    tmp_dir = os.path.join(REPO, ".tmpwork", "pytest_i64")
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, "config_i64.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    _reload(cfg, path)
    yield cfg
    os.environ.pop("AIKIT_CONFIG_PATH", None)
    common_rules._CONFIG_CACHE = None
    common_rules.reset_patterns()


# ---------------------------------------------------------------------------
# 1. 验收 1：白名单机构实体整段脱敏 + 与通用词层互斥
# ---------------------------------------------------------------------------

def test_whitelist_entity_masked_whole(whitelist_cfg):
    out = _redact("关于营运管理部的通知")
    assert "营运管理部" not in out, "白名单机构实体漏脱: %r" % out
    assert "营运" not in out and "管理部" not in out, "出现混合残态: %r" % out


def test_entity_layer_exclusive_with_generic_layer(default_cfg):
    """同一实体在两层中只能有一种口径。"""
    rules = default_cfg["org_name_rules"]
    generic = list(rules.get("generic_words") or [])
    wl = list(rules["org_entity_layer"].get("entity_whitelist") or [])
    assert not (set(generic) & set(wl)), "通用词表与白名单存在交集"
    for word in generic:
        assert _redact(word) == word, "通用词 %s 被实体层叠加脱敏" % word
    for word in wl:
        assert _redact(word) == "XXXX", "白名单实体 %s 未整段脱敏" % word


def test_generic_dept_name_kept(default_cfg):
    """通用部门名（人工标准版一律保留）→ 保留原文。

    「短信中心」本轮由机构通道整段遮盖（G3 over 来源之一），
    现按 D-4 通用词口径归入保留集合。
    """
    for word in ("短信中心", "运营中心", "科技中心", "管理部", "营运部"):
        assert _redact(word) == word, "%s 被误脱敏: %r" % (word, _redact(word))


# ---------------------------------------------------------------------------
# 2. 验收 5：简称派生清单接入后的口径
# ---------------------------------------------------------------------------

def test_bank_abbreviation_masked(default_cfg):
    """`建行` → 整词脱敏（银行简称派生）。"""
    out = _redact("建行接入三方测试环境")
    assert "建行" not in out, "银行简称漏脱: %r" % out


def test_bank_fullname_prefix_masked(default_cfg):
    """`工商银行` → 前缀脱敏、通名保留（全称口径）。"""
    out = _redact("工商银行")
    assert "工商" not in out, "机构全称漏脱: %r" % out
    assert "银行" in out, "机构通名被误伤: %r" % out


def test_org_alias_derived_masked(default_cfg):
    """`平安 + 机构通名` → 简称脱敏、通名保留。"""
    out = _redact("平安科技数据平台部")
    assert "平安" not in out, "简称漏脱: %r" % out
    assert "科技" in out, "机构通名被误伤: %r" % out


def test_standalone_abbreviation_masked(default_cfg):
    """`计财` 独立出现 → 整词脱敏；后接部门通名时保留。"""
    assert _redact("计财") == "XXXX"
    assert _redact("营运计财部") == "营运计财部"


def test_alias_lists_present(default_cfg):
    """简称派生清单必须已接入配置（验收 5 的前提）。"""
    rules = default_cfg["org_name_rules"]
    assert rules.get("bank_abbreviations"), "缺少 bank_abbreviations"
    assert rules.get("abbreviation_derivations"), "缺少 abbreviation_derivations"
    assert rules.get("standalone_abbreviations"), "缺少 standalone_abbreviations"
    assert "建行" in rules["bank_abbreviations"]
    assert "计财" in rules["standalone_abbreviations"]
    assert "平安" in rules["abbreviation_derivations"]


# ---------------------------------------------------------------------------
# 3. D-14：词级守卫判定域 = 实际被遮盖的那一段
# ---------------------------------------------------------------------------

def test_anchor_common_word_not_masked(default_cfg):
    """锚点后的通用词不再被整段吞掉（G6 的 8 处 over 来源）。"""
    for text, word in (
        ("各条线负责人管理跟踪", "管理"),
        ("测试结果分别由各条线负责人管理跟踪，全组统一汇总。", "管理"),
    ):
        out = _redact(text)
        assert word in out, "通用词 %s 被误吞: %r" % (word, out)
        assert "XXX" not in out, "整段被误吞: %r" % out


def test_anchor_real_name_still_masked(default_cfg):
    """D-14 不得回退真名召回：锚点后的真名仍须脱敏。"""
    out = _redact("各条线负责人林梅娥跟踪")
    assert "林梅娥" not in out, "锚点后真名漏脱: %r" % out
    assert "负责人" in out, "锚点词被误伤: %r" % out


def test_guard_words_not_masked(default_cfg):
    """新增入表的通用词在典型语境中不被误吞（原文保留）。"""
    cases = [
        ("海银运[2021]253号", "海银运"),
        ("再按照指令系统分发的指令，选择", "的指令"),
        ("超柜适用", "超柜"),
        ("批量任务、混合业务测试", "批量任务"),
    ]
    for text, word in cases:
        out = _redact(text)
        assert word in out, "通用词 %s 被误吞: %r" % (word, out)


def test_no_sentinel_leak(default_cfg):
    """私有区哨兵不得泄漏到最终文本。"""
    out = _redact("林梅娥营运管理部钱媛")
    for cp in ("\ue002", "\ue003", "\ue004", "\ue005"):
        assert cp not in out, "哨兵泄漏: %r" % out


def test_idempotent(default_cfg):
    once = _redact("测试结果分别由各条线负责人管理跟踪，全组统一汇总。")
    twice = _redact(once)
    assert once == twice, "非幂等: %r -> %r" % (once, twice)


# ---------------------------------------------------------------------------
# 4. 真名互斥纪律：保护表不得引入真名漏脱
# ---------------------------------------------------------------------------

def test_guard_words_disjoint_from_name_dict(default_cfg):
    """保护表与通用人名词典层零交集（#55 纪律）。"""
    cfg = default_cfg
    dict_file = cfg.get("common_name_dict_file")
    words = set()
    if dict_file:
        path = dict_file if os.path.isabs(dict_file) else os.path.join(SCRIPTS, dict_file)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        words.add(line)
    if not words:
        pytest.skip("通用人名词典文件不可用")
    overlap = set(cfg.get("common_word_guards") or []) & words
    assert not overlap, "保护表与词典层存在交集: %s" % sorted(overlap)[:5]


def test_guard_words_disjoint_from_full_name_list(default_cfg):
    """保护表与 D16 全名库双向互斥（防真名漏脱）。"""
    d16 = common_rules.get_full_name_set()
    for word in (default_cfg.get("common_word_guards") or []):
        for name in d16:
            assert not (name in word or word in name), \
                "保护词 %s 与真名 %s 存在包含关系" % (word, name)
