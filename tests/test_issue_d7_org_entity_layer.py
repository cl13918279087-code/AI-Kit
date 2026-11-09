# -*- coding: utf-8 -*-
"""
Issue #64 D-7：L3 机构实体层（org_stems × org_suffixes 组合式生成）。

【背景】
机构名通道此前只有两层语义：
  · `generic_words`（独立通用词）→ **保留**原文；
  · `composite_words`（前缀成词）→ **整段脱敏**。
「通名 + 后缀」组合（如「零售部」「营运管理部」）既不在两张表里，只能落到
通用机构名正则的兜底分支，于是同时产生两种错误：
  ① 通用部门名被整段脱敏（人工标准版中一律保留）→ 过度脱敏；
  ② 真实机构实体无法按业务需要整段脱敏 → 口径无法切换。

【D-7 方案（Issue #64 §四 方案 A）】
引入 L3 机构实体层：
  · `org_stems`（通名）× `org_suffixes`（后缀）→ 组合式生成候选机构实体；
  · 组合命中且在 `entity_whitelist`（业务侧白名单）内 → **整段脱敏**；
  · 组合命中但不在白名单 → **整段保留**（回退批次一通用词口径）；
  · `composite_words`（D-4 前缀成词层）同属「整段脱敏」，由本层一并接管。

【本文件的断言】对应 #64 §四 验收第 1 条：
  1. 白名单机构实体（`营运管理部`）→ **整段脱敏**，且无「XX+XXXX」混合态；
  2. 非白名单组合（`运营中心`）→ **保留**；
  3. `信息科技部` → **整段脱敏**（D-4 前缀成词层回归）；
  4. 机构实体层与通用词层**互斥**（同一实体只有一种口径，不叠加、不混合）。

运行：
    python3 -m pytest tests/test_issue_d7_org_entity_layer.py -v
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


# ---------------------------------------------------------------------------
# 0. 测试夹具：默认配置 / 注入白名单配置
# ---------------------------------------------------------------------------

def _read_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _reload(cfg=None, path=None):
    """切换配置并重建所有配置派生缓存。"""
    if path is not None:
        os.environ["AIKIT_CONFIG_PATH"] = path
    common_rules._CONFIG_CACHE = cfg
    common_rules.reset_patterns()


@pytest.fixture(scope="module")
def default_cfg():
    """仓库内真实配置（不受其它测试文件的 AIKIT_CONFIG_PATH 影响）。"""
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
    tmp_dir = os.path.join(REPO, ".tmpwork", "pytest_d7")
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, "config_d7.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    _reload(cfg, path)
    yield cfg
    os.environ.pop("AIKIT_CONFIG_PATH", None)
    common_rules._CONFIG_CACHE = None
    common_rules.reset_patterns()


def _redact(text):
    return common_rules.apply_redactions(text)


def _has_mixed_state(out, word):
    """是否出现「部分保留 + 部分遮盖」的混合残态（如 营运XXXX）。"""
    if word not in out:
        return False
    # 实体被完整保留 → 不是混合态
    return False


# ---------------------------------------------------------------------------
# 1. 配置落地
# ---------------------------------------------------------------------------

def test_d7_config_block_present(default_cfg):
    layer = (default_cfg.get("org_name_rules") or {}).get("org_entity_layer")
    assert layer, "缺少 org_name_rules.org_entity_layer 配置块"
    assert layer.get("enable") is True, "D-7 机构实体层未启用"
    assert layer.get("org_stems"), "缺少 org_stems（机构通名）"
    assert layer.get("org_suffixes"), "缺少 org_suffixes（机构后缀）"
    assert layer.get("entity_whitelist") is not None, "缺少 entity_whitelist"


def test_d7_suffixes_exclude_ambiguous(default_cfg):
    """「营业部」不在后缀表内——机构链口径由 D-11 负责，避免双通道打架。"""
    layer = default_cfg["org_name_rules"]["org_entity_layer"]
    assert "营业部" not in layer["org_suffixes"]


def test_d7_whitelist_excludes_generic_words(default_cfg):
    """配置层互斥：白名单实体不得同时出现在通用词表中。

    否则同一实体在两层里各自有一套口径，「保留 / 整段脱敏」将不可判定。
    """
    rules = default_cfg["org_name_rules"]
    generic = set(rules.get("generic_words") or [])
    wl = set(rules["org_entity_layer"].get("entity_whitelist") or [])
    assert not (generic & wl), "白名单与通用词表存在交集: %s" % (generic & wl)


# ---------------------------------------------------------------------------
# 2. 验收 1：白名单机构实体整段脱敏（无 XX+XXXX 混合态）
# ---------------------------------------------------------------------------

def test_whitelist_entity_fully_masked(whitelist_cfg):
    """白名单实体 → 整段脱敏为 ORG 占位符，绝不出现「营运XXXX」残态。"""
    out = _redact("营运管理部")
    assert "营运管理部" not in out, "白名单实体未被脱敏: %r" % out
    assert out == "XXXX", "白名单实体未整段脱敏: %r" % out


def test_whitelist_entity_in_sentence_no_mixed_state(whitelist_cfg):
    """句中场景同样整段脱敏，不产生「营运 + XXXX」混合态。"""
    out = _redact("关于营运管理部的通知")
    assert "营运" not in out, "出现前缀残留（混合态）: %r" % out
    assert "管理部" not in out, "出现后缀残留（混合态）: %r" % out
    assert "XXXX" in out, "未脱敏: %r" % out


def test_whitelist_entity_masked_with_adjacent_name(whitelist_cfg):
    """白名单实体相邻的姓名仍须脱敏——占位符不得打断姓名边界。"""
    out = _redact("林梅娥营运管理部钱媛")
    assert "林梅娥" not in out, "前置人名漏脱: %r" % out
    assert "钱媛" not in out, "后置人名漏脱: %r" % out
    assert "营运管理部" not in out, "白名单实体漏脱: %r" % out


# ---------------------------------------------------------------------------
# 3. 验收 2：非白名单组合 → 保留
# ---------------------------------------------------------------------------

def test_non_whitelist_combo_kept(whitelist_cfg):
    """`运营中心` 不在白名单 → 保留原文（回退批次一通用词口径）。"""
    out = _redact("运营中心")
    assert out == "运营中心", "非白名单组合被误脱敏: %r" % out


def test_non_whitelist_combo_kept_in_sentence(whitelist_cfg):
    out = _redact("请运营中心牵头推进")
    assert "运营中心" in out, "非白名单组合被误脱敏: %r" % out


def test_generic_dept_name_kept(default_cfg):
    """通用部门名（人工标准版一律保留）→ 保留，不得整段遮盖。

    「零售部 / 贸金部 / 特资部 / 金市部 / 公司部」在标准版中 0 遮蔽，
    此前我方整段遮盖属过度脱敏（Issue #64 §四 验收 2 的机构类来源）。
    """
    for word in ("零售部", "贸金部", "特资部", "金市部", "公司部",
                 "计划财务部", "各业务部"):
        out = _redact(word)
        assert out == word, "%s 被误脱敏: %r" % (word, out)


# ---------------------------------------------------------------------------
# 4. 验收 3：`信息科技部` 整段脱敏（回归）
# ---------------------------------------------------------------------------

def test_composite_word_fully_masked(default_cfg):
    """D-4 前缀成词层回归：信息科技部 / 金融科技部 整段脱敏。"""
    for word in ("信息科技部", "金融科技部"):
        out = _redact(word)
        assert out == "XXXX", "%s 未整段脱敏: %r" % (word, out)


def test_composite_word_in_sentence_no_mixed_state(default_cfg):
    out = _redact("信息科技部负责系统建设")
    assert "信息科技部" not in out, "信息科技部漏脱: %r" % out
    assert "科技部" not in out, "出现后缀残留（混合态）: %r" % out


# ---------------------------------------------------------------------------
# 5. 验收 4：机构实体层与通用词层互斥
# ---------------------------------------------------------------------------

def test_entity_layer_and_generic_layer_exclusive(default_cfg):
    """同一实体在两层中只能有一种口径：
    · 通用词表内的词 → 通用词层保留（实体层不得叠加脱敏）；
    · 白名单内的词 → 实体层整段脱敏（通用词层不得还原）。
    """
    rules = default_cfg["org_name_rules"]
    generic = list(rules.get("generic_words") or [])
    wl = list(rules["org_entity_layer"].get("entity_whitelist") or [])
    for word in generic:
        out = _redact(word)
        assert out == word, "通用词 %s 被实体层叠加脱敏: %r" % (word, out)
    for word in wl:
        out = _redact(word)
        assert out == "XXXX", "白名单实体 %s 未整段脱敏: %r" % (word, out)


def test_entity_layer_decision_is_context_free(whitelist_cfg):
    """实体层决策与上下文无关：单独出现 / 句中出现口径一致。"""
    for word in ("营运管理部", "运营中心", "信息科技部"):
        alone = _redact(word)
        in_ctx = _redact("关于%s的通知" % word)
        masked = (alone == "XXXX")
        assert masked == ("XXXX" in in_ctx and word not in in_ctx), \
            "%s 上下文口径不一致: %r vs %r" % (word, alone, in_ctx)


def test_mask_set_and_keep_set_disjoint(whitelist_cfg):
    """脱敏集合与保留集合不得相交（同一实体不得同时可脱敏又可保留）。"""
    mask_set, keep_set = common_rules._get_org_entity_layer_sets()
    assert not (mask_set & keep_set), "脱敏/保留集合存在交集: %s" % (mask_set & keep_set)
    assert "营运管理部" in mask_set
    assert "信息科技部" in mask_set
    assert "运营中心" in keep_set


def test_idempotent(whitelist_cfg):
    """幂等：二次脱敏不改变结果（哨兵还原不留残留）。"""
    once = _redact("关于营运管理部的通知")
    twice = _redact(once)
    assert once == twice, "非幂等: %r -> %r" % (once, twice)


def test_no_sentinel_leak(whitelist_cfg):
    """私有区哨兵不得泄漏到最终文本。"""
    out = _redact("林梅娥营运管理部钱媛")
    for cp in ("\ue002", "\ue003", "\ue004", "\ue005"):
        assert cp not in out, "哨兵泄漏: %r" % out


# ---------------------------------------------------------------------------
# 6. 验收 5：简称派生口径（D-8，随批次四落地）回归固化
# ---------------------------------------------------------------------------

def test_bank_abbreviation_masked(default_cfg):
    """`建行` 等银行简称 → 按全称口径整词脱敏（D-8 / bank_abbreviations）。"""
    out = _redact("建行接入三方测试环境")
    assert "建行" not in out, "银行简称漏脱: %r" % out


def test_standalone_abbreviation_masked(default_cfg):
    """`计财` 独立出现 → 整词脱敏；后接部门通名时保留（营运计财部）。"""
    assert _redact("计财") == "XXXX"
    assert _redact("营运计财部") == "营运计财部"


def test_derived_org_abbreviation_masked(default_cfg):
    """`平安 + 机构通名` → 简称脱敏、通名保留（与人工标准版一致）。"""
    out = _redact("平安科技数据平台部")
    assert "平安" not in out, "简称漏脱: %r" % out
    assert "科技" in out, "机构通名被误伤: %r" % out


def test_full_org_name_masked(default_cfg):
    """机构全称 → 前缀脱敏、通名保留（工商银行 → XX银行）。"""
    out = _redact("工商银行")
    assert "工商" not in out, "机构全称漏脱: %r" % out
