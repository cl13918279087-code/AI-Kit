#!/usr/bin/env python3
"""
test_issue57_colon_name_length.py - Issue #57 回归守卫（规则 C 全角冒号后的姓名长度）

【缺陷现象】
规则 C（全角冒号「：」后的姓名捕获）使用**非贪婪**量词 `{1,3}`，
在「负责人：蔡昀煜」这类三字名上只吃掉「蔡昀」，输出残缺：

    "负责人：蔡昀煜" → "负责人：蔡XX"   ← 尾字「煜」泄漏

【修复（D-1）】
  ① 量词恢复贪婪 `{1,3}`，三字名整段命中；
  ② 贪婪后若命中片段内含**职务词**，则从职务词**起点**切开：
     姓名部分整段替换，职务词整体保留。

    "负责人：蔡昀煜"   → "负责人：XXX"
    "组长：樊霖副组长" → "组长：XXX副组长"    ← 不得出现 "XXX副组"
    "组长：李经理"     → "组长：XXX"          ← 姓 + 职务整体遮盖，不留"经理"

【本文件守卫的内容】
  1. 26 个实测实名 × 4 种上下文，无一出现尾字残留；
  2. 职务词不被拦腰截断（不得出现 "XXX副组" 一类残缺态）；
  3. 全量 D16 三字名回归扫描：残留 0；
  4. 零劣化：通用语料姓名类误伤维持 0、金标召回不退化。

运行方式：
    python -m pytest tests/test_issue57_colon_name_length.py -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common_rules  # noqa: E402
from common_rules import apply_redactions  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, "scripts", "config.json")


# 实测收集的三字实名样本（脱敏差异比对报告中的残留样本）
REAL_NAMES = [
    "蔡昀煜", "阮秋鸣", "吴文静", "黄宽钊", "段宇信", "吴晋秋", "易晓晨",
    "董圣瑜", "陈绍飞", "费黎花", "宗树峰", "欧晓烨", "李义申", "张锦宇",
    "黄丽丹", "廖腾华", "孙海刚", "毛月珍", "朱丽琼", "李杰", "樊霖",
    "窦磊", "王富生", "王根利", "郑云", "杨瑜",
]

CONTEXTS = [
    "负责人：{}",
    "负责人：{}负责",
    "由{}负责",
    "负责人:{}",
]


# ---------------------------------------------------------------------------
# 1. 三字名整段命中（无尾字残留）
# ---------------------------------------------------------------------------

def test_real_names_no_residual():
    """26 个实名 × 4 种上下文：不得出现尾字残留。"""
    failures = []
    for name in REAL_NAMES:
        for ctx in CONTEXTS:
            src = ctx.format(name)
            out = apply_redactions(src)
            # 姓名被脱敏后，原名的任何一个字都不应残留在输出里
            for ch in name:
                if ch in out:
                    failures.append(f"{src!r} → {out!r}（残留 {ch!r}）")
                    break
    assert not failures, "以下输入仍有姓名残留：\n  " + "\n  ".join(failures)


def test_colon_name_fully_masked():
    """全角冒号后姓名整段替换为占位符。"""
    assert apply_redactions("负责人：蔡昀煜") == "负责人：XXX"
    assert apply_redactions("成员：蔡昀煜、阮秋鸣") == "成员：XXX、XXX"


# ---------------------------------------------------------------------------
# 2. 职务词起点剥离（不得拦腰截断）
# ---------------------------------------------------------------------------

def test_role_word_kept_whole():
    """姓名 + 职务后缀：职务词整体保留，不得截成 "XXX副组"。"""
    out = apply_redactions("组长：樊霖副组长")
    assert out == "组长：XXX副组长", out
    assert "副组" not in out.replace("副组长", ""), "职务词被拦腰截断"


def test_surname_plus_title_fully_masked():
    """姓 + 职务（姓氏即个人信息）：整体遮盖，不留职务尾字泄漏姓氏。"""
    out = apply_redactions("组长：李经理")
    assert out == "组长：XXX", out
    assert "李" not in out


def test_no_truncated_role_form():
    """全量扫描：不得出现被截断的职务词形态。"""
    broken = ("副组", "副经", "总经", "营业", "信息科", "科主", "组经",
              "副主", "副总", "营销", "团队")
    failures = []
    for name in REAL_NAMES:
        for role in ("副组长", "副经理", "总经理", "科员", "业务员", "组员"):
            src = f"负责人：{name}{role}"
            out = apply_redactions(src)
            for b in broken:
                if b in out and (b + role[len(b):]) not in out:
                    failures.append(f"{src!r} → {out!r}")
    assert not failures, "出现被截断的职务词：\n  " + "\n  ".join(failures)


# ---------------------------------------------------------------------------
# 3. 全量 D16 三字名回归扫描
# ---------------------------------------------------------------------------

def _d16_three_char_names():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    out = []
    for n in cfg.get("full_name_list") or []:
        if isinstance(n, str) and len(n) == 3 and "姓名" != n:
            out.append(n)
    return out


def _rule_c_pattern():
    """取出规则 C（全角冒号后的姓名捕获）这一条正则。"""
    cands = [p for p, _ in common_rules._get_patterns()
             if p.pattern.startswith("(?<=：)(?:")]
    assert len(cands) == 1, f"规则 C 正则定位失败（命中 {len(cands)} 条）"
    return cands[0]


def test_d16_three_char_names_rule_c_full_match():
    """D16 库内三字名：规则 C 必须**整名命中**（贪婪量词生效的直证）。

    这是 D-1 的规则级断言——不受其他通道（地名/机构名）优先级影响，
    因此可以覆盖全量 5000+ 三字名。
    """
    names = _d16_three_char_names()
    assert len(names) > 100, f"D16 三字名样本异常（{len(names)}）"
    rule_c = _rule_c_pattern()
    failures = []
    for n in names:
        m = rule_c.search(f"负责人：{n}")
        if not m or m.group(0) != n:
            failures.append(f"{n!r} → {m.group(0) if m else None!r}")
    assert not failures, f"{len(failures)} 个三字名未被规则 C 整名命中：{failures[:5]}"


# 姓名与地名/机构名天然冲突的样本（前置通道优先级导致，非 D-1 范围）：
#   玉林 / 长春 / 海南 / 长江 / 桂林 —— 城市名；
#   镇 / 路 / 科 / 部 —— 机构与行政区划后缀。
# 整改前（基线）同样如此，属独立的「姓名 ↔ 实体」优先级议题。
_PLACE_OR_ORG_CHARS = set("镇路科室部")
_PLACE_WORDS = ("玉林", "长春", "海南", "长江", "桂林", "深圳", "北京",
                "上海", "广州", "南京", "杭州", "武汉", "成都", "西安")


def test_d16_three_char_names_no_residual_end_to_end():
    """端到端扫描：非实体冲突样本的冒号上下文残留 0。"""
    names = _d16_three_char_names()
    conflicts = []
    checked = 0
    failures = []
    for n in names:
        if (set(n) & _PLACE_OR_ORG_CHARS) or any(w in n for w in _PLACE_WORDS):
            conflicts.append(n)
            continue
        checked += 1
        out = apply_redactions(f"负责人：{n}")
        if out != "负责人：XXX":
            failures.append(f"负责人：{n} → {out!r}")
    assert checked > 5000, f"有效样本过少（{checked}）"
    assert not failures, f"{len(failures)} 个三字名仍有残留，例如：{failures[:5]}"


# ---------------------------------------------------------------------------
# 4. 零劣化
# ---------------------------------------------------------------------------

def test_biz_corpus_no_name_misfire():
    """通用业务语料：姓名类误伤维持 0。"""
    from test_name_false_positive import FALSE_POSITIVE_CORPUS

    name_class = []
    for seg in FALSE_POSITIVE_CORPUS:
        out, cnt = common_rules.apply_redactions_counted(seg)
        if out == seg:
            continue
        if cnt.get("姓名"):
            name_class.append(seg)
    assert not name_class, f"通用语料出现姓名类误伤 {len(name_class)} 条：{name_class[:3]}"


def test_gold_names_still_recalled():
    """真名金标召回不得劣化。"""
    bench = os.path.join(REPO_ROOT, "tests", "benchmark", "gold_names.json")
    with open(bench, encoding="utf-8") as f:
        gold_raw = json.load(f)
    gold = set()
    for v in gold_raw.values():
        if isinstance(v, list):
            gold.update(v)

    missed = []
    for g in gold:
        if not g:
            continue
        if g in apply_redactions(f"负责人：{g}，负责整体推进。"):
            missed.append(g)
    assert not missed, f"金标漏脱 {len(missed)} 个：{missed[:5]}"


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
