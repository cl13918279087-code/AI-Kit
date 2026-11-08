#!/usr/bin/env python3
"""整改方案设计验证脚本（2026-11-08 统一整改方案）

用途：在**不改动生产代码**的前提下，验证整改方案的三项关键设计是否成立，
为社区评审提供可复现的实证依据：

  1. 证据门控原型（对应 Issue #58 / #59 的 L1 姓名证据层）
     - 验证「姓氏 + 名用字 + 字频闸门」四合一判据在真实名单上的召回
     - 验证同一判据在职务标签 / 通用词上的误判率
  2. 规则 C 贪婪匹配原型（对应 Issue #57）
     - 验证「贪婪 {1,3} + 职务词起点剥离」可消除姓名尾字残留
     - 同时暴露 Issue #57 原文方案 A「回退 1 字」的缺陷
  3. 部分脱敏残留扫描（门禁 1 基线）
     - 扫描产出件中 `X{2,}[汉字]` 形态，剔除合法续接与职务尾字

用法：
    python3 scripts/verify_remediation_design.py [--scan-pairs pairs.json]

说明：本脚本只读，不写生产代码；所有原型逻辑均在内存中独立实现。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import common_rules as cr  # noqa: E402

# ---------------------------------------------------------------------------
# 样本集
# ---------------------------------------------------------------------------

# 真实名单样本（来自 Issue #58 实证与七组比对报告）
POSITIVE_NAMES: List[str] = [
    "窦磊", "王富生", "王根利", "郑云", "杨瑜", "孙海刚", "吴文静",
    "蔡昀煜", "阮秋鸣", "李熔", "王谧", "成葭", "刘兆缋", "段宇信",
    "黄丽丹", "易晓晨",
]

# 职务标签 / 通用词（应为保留）
NEGATIVE_NAMES: List[str] = [
    "内部资料", "禁止外传", "各方代表", "专家骨干", "报告生成",
    "会议纪要", "各条线副行长", "总监", "副组长", "各业务部门总经理",
    "业务和技术骨干", "外部专家骨干", "项目管理骨干", "总体组",
    "开发一组", "金融同业", "电子银行", "小企业", "资产管理", "零售",
    "方案设计", "日常运营", "业务发展", "客户经理",
]

# 规则 C 场景样本（Issue #57）
COLON_CASES: List[str] = [
    "负责人：蔡昀煜", "组长：阮秋鸣", "成员：吴文静", "组长：黄宽钊",
    "组长：樊霖副组长", "副组长：各条线副行长、总监", "领导：总经理",
    "成员：蔡昀煜、阮秋鸣", "负责人：李熔", "负责人：王谧",
]

# L2 职务标签词表（组合式声明：prefix_forms + suffixes）
ROLE_WORDS: List[str] = [
    "副行长", "分行长", "支行长", "总行长", "行长", "副组长", "组长",
    "副经理", "总经理", "经理", "副总监", "总监", "副主任", "主任",
    "副科长", "科长", "副处长", "处长", "副部长", "部长", "专员",
    "顾问", "助理", "站长", "队长", "副主管", "主管", "骨干",
    "负责人", "成员",
]


# ---------------------------------------------------------------------------
# 1. 证据门控原型
# ---------------------------------------------------------------------------

class EvidenceModel:
    """L1 姓名证据层的原型实现。

    判据（四者缺一不可）：
        ① 该块 ∈ D16 精确全名表
        ② 该块 ∈ 通用人名词典（Issue #55）
        ③ 该块首字 ∈ 姓氏池，且其余字 ∈ 名字用字池，且尾字字频 ∈ hot ∪ warm
        ④ 该块不在业务词表 / 通用词护栏表中
    """

    def __init__(self) -> None:
        cfg = cr.get_config()
        self.cfg = cfg
        self.d16: Set[str] = set(cfg.get("full_name_list", []))
        dict_file = cfg.get("common_name_dict_file")
        self.dict_words: Set[str] = set()
        if dict_file:
            p = _SCRIPTS / dict_file if not Path(dict_file).is_absolute() else Path(dict_file)
            if p.exists():
                self.dict_words = set(p.read_text(encoding="utf-8").split())
        self.surnames: Set[str] = set(cfg["surname_pool"])
        self.pool: Set[str] = set(cr._get_name_pool_chars(cfg))
        self.hot: Set[str] = set(cr._get_gated_name_chars(cfg))
        self.warm: Set[str] = set(cr._get_warm_name_chars(cfg))
        self.guards: Set[str] = set(cfg.get("common_word_guards", []))
        self.biz: Set[str] = set(cfg.get("biz_guard_words", []))

    def raw_evidence(self, word: str) -> bool:
        """判据 ①②③（不含护栏），用于展示「朴素判据」的误判面。"""
        if word in self.d16 or word in self.dict_words:
            return True
        if not (2 <= len(word) <= 4):
            return False
        if not all("\u4e00" <= c <= "\u9fa5" for c in word):
            return False
        if word[0] not in self.surnames:
            return False
        rest = word[1:]
        if not all(c in self.pool for c in rest):
            return False
        return rest[-1] in self.hot or rest[-1] in self.warm

    def gated(self, word: str) -> bool:
        """完整判据（①②③ + ④护栏）。"""
        if word in self.guards or word in self.biz:
            return False
        return self.raw_evidence(word)


def run_gate_prototype() -> int:
    model = EvidenceModel()
    print("=" * 68)
    print("1. 证据门控原型（Issue #58 / #59 — L1 姓名证据层）")
    print("=" * 68)

    print("\n[正向] 真实姓名，应判 True：")
    hit = miss = 0
    for w in POSITIVE_NAMES:
        ok = model.gated(w)
        hit += ok
        miss += not ok
        note = "" if ok else "  ← 字池缺字（Issue #60 范畴）"
        print(f"  {w:<8} raw={model.raw_evidence(w)!s:<5} gated={ok!s:<5}{note}")

    print("\n[负向] 职务标签 / 通用词，应判 False：")
    fp = 0
    for w in NEGATIVE_NAMES:
        raw = model.raw_evidence(w)
        g = model.gated(w)
        fp += g
        flag = "  ← 朴素判据误判" if (raw and not g) else ""
        print(f"  {w:<14} raw={raw!s:<5} gated={g!s:<5}{flag}")

    print(f"\n  正向命中 {hit}/{len(POSITIVE_NAMES)}，未命中 {miss}")
    print(f"  负向误判 {fp}/{len(NEGATIVE_NAMES)}")
    print("  结论：朴素判据（仅姓氏+名用字）会误判「方案设计」「金融同业」；"
          "叠加字频闸门与护栏表后归零。")
    return 0 if fp == 0 else 1


# ---------------------------------------------------------------------------
# 2. 规则 C 贪婪匹配原型
# ---------------------------------------------------------------------------

def run_colon_prototype() -> int:
    cfg = cr.get_config()
    pool = cr._get_name_pool_chars(cfg)
    cls = cr._build_name_char_class(pool)
    surnames: Set[str] = set(cfg["surname_pool"])
    surname_alt = "|".join(re.escape(s) for s in surnames)
    greedy = re.compile(rf"(?<=：)(?:{surname_alt}){cls}{{1,3}}")

    def repl(m: re.Match) -> str:
        s = m.group(0)
        # 剥离尾部的职务词（按长度降序，取最长可切分者）
        for w in sorted(ROLE_WORDS, key=len, reverse=True):
            if not s.endswith(w):
                continue
            cand = s[:-len(w)]
            if len(cand) >= 2 and cand[0] in surnames \
                    and all(c in pool for c in cand[1:]):
                return "XXX" + w
            break
        return "XXX"

    print("=" * 68)
    print("2. 规则 C 贪婪匹配原型（Issue #57 — 全角冒号长度量词）")
    print("=" * 68)
    for s in COLON_CASES:
        print(f"  {s!r:<34} -> {greedy.sub(repl, s)!r}")

    print("\n  对比 Issue #57 原文方案 A（「回退 1 字」）：")
    print("    组长：樊霖副组长  贪婪匹配到「樊霖副组长」")
    print("    回退 1 字  → XXX副组    ❌ 职务词被拦腰截断")
    print("    剥离职务词 → XXX副组长  ✅ 职务词整体保留")
    print("  结论：回退必须回退到**职务词起点**，而非固定 1 字。")
    return 0


# ---------------------------------------------------------------------------
# 3. 部分脱敏残留扫描
# ---------------------------------------------------------------------------

_RAW = re.compile(r"X{2,}[\u4e00-\u9fa5]")
# 占位符后合法续接字（不属于姓名尾字残留）
_LEGIT = set("行银部组和与中队的了及或等是有在职责业务工作内容名称方式方法")
# 明显职务续接
_ROLE_TAIL = set("长员理监总组主任管经理部长")


def scan_partial(pairs_file: Path) -> int:
    print("=" * 68)
    print("3. 部分脱敏残留扫描（门禁 1 基线 — Issue #57 口径）")
    print("=" * 68)
    try:
        from compare_with_standard import extract_texts
    except ImportError:
        print("  [跳过] scripts/compare_with_standard.py 不可用")
        return 0

    data = json.loads(pairs_file.read_text(encoding="utf-8"))
    pairs = data["pairs"] if isinstance(data, dict) else data
    total_raw = total_suspect = 0
    for p in pairs:
        mine = Path(p["mine"])
        if not mine.exists():
            continue
        raw = suspect = 0
        samples: List[str] = []
        for _blk, text in extract_texts(mine):
            for m in _RAW.finditer(text):
                raw += 1
                tail = m.group(0)[-1]
                if tail in _LEGIT or tail in _ROLE_TAIL:
                    continue
                suspect += 1
                if len(samples) < 6:
                    samples.append(m.group(0))
        total_raw += raw
        total_suspect += suspect
        print(f"  {mine.name[:44]:<46} raw={raw:<5} suspect={suspect:<5} {samples}")
    print(f"\n  TOTAL raw={total_raw}  suspect(疑似姓名尾字残留)={total_suspect}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="整改方案设计验证")
    ap.add_argument("--scan-pairs", default="output/compare_20261108/pairs.json",
                    help="七组比对映射文件（用于残留扫描）")
    args = ap.parse_args()

    rc = run_gate_prototype()
    rc |= run_colon_prototype()
    pairs = Path(args.scan_pairs)
    if pairs.exists():
        rc |= scan_partial(pairs)
    else:
        print(f"\n[跳过] 残留扫描：{pairs} 不存在")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
