#!/usr/bin/env python3
"""
decision_mining.py - 裁决记录挖掘 → 规则候选（Issue #54 D1 闭环末端）

【闭环位置】Issue #54 §2.1 的裁决闭环最后一步：

    规则引擎 → 候选清单(span+置信度+通道) → 灰区导出 → Agent/LLM 裁决
      → 回灌二轮定稿 → **高频裁决自动沉淀为规则候选（保护表 / 名单）**

本脚本实现最后一步：把散落在 decisions*.json 里的裁决记录聚合起来，
按频次产出两类候选：

  · `guard_candidates` —— 判为 **skip**（不该脱敏）的高频实体
    → 建议并入 config.json 的 `common_word_guards`（整词保护表）
  · `name_candidates`  —— 判为 **redact**（确实该脱敏）的高频实体
    → 建议并入 config.json 的 `full_name_list`（精确全名表）

【纪律】本脚本**只产出候选，不自动改配置**——保护表 / 全名表入库必须经过
人工评审（与 Issue #51 §4.3 的保护表闭环一致）。脚本会主动做两项互斥检查：

  ① 候选若已在 D16 全名库或评测金标中 → 标为 `conflict`，**不得**进保护表
     （否则会造成真名漏脱）；
  ② 候选若本身是占位人名（张三/李四）→ 不产出（占位名必须脱敏）。

用法：
    python3 scripts/decision_mining.py --decisions a.json b.json --min-freq 3
    python3 scripts/decision_mining.py --decisions ./decisions_dir --out cand.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common_rules  # noqa: E402


def _read_records(path: str) -> List[Dict[str, Any]]:
    """读取一个裁决文件（列表格式或 {"pending": [...]} 格式）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        for key in ("pending", "decisions", "items"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def _collect_files(paths: Iterable[str]) -> List[str]:
    """把路径参数展开为文件列表（支持目录递归）。"""
    out: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for fn in files:
                    if fn.endswith(".json") and ("decision" in fn or "gray" in fn):
                        out.append(os.path.join(root, fn))
        elif os.path.isfile(p):
            out.append(p)
    return sorted(out)


def mine_decisions(paths: Iterable[str], min_freq: int = 3,
                   repo_root: Optional[str] = None) -> Dict[str, Any]:
    """挖掘裁决记录，产出保护表 / 名单候选。"""
    files = _collect_files(paths)
    freq_skip: Counter = Counter()
    freq_redact: Counter = Counter()
    channels: Dict[str, Counter] = defaultdict(Counter)
    categories: Dict[str, Counter] = defaultdict(Counter)

    for path in files:
        for rec in _read_records(path):
            text = (rec.get("text") or "").strip()
            decision = (rec.get("decision") or "").strip().lower()
            if not text or decision not in ("skip", "redact"):
                continue
            if decision == "skip":
                freq_skip[text] += 1
            else:
                freq_redact[text] += 1
            ch = rec.get("channel")
            if ch:
                channels[text][ch] += 1
            cat = rec.get("category")
            if cat:
                categories[text][cat] += 1

    # 真名库 / 金标（互斥检查用）
    try:
        cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "config.json"), encoding="utf-8"))
    except Exception:
        cfg = {}
    full_names = set(cfg.get("full_name_list") or [])
    placeholders = set(cfg.get("placeholder_names") or [])
    gold: set = set()
    gold_path = os.path.join(repo_root or os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tests", "benchmark", "gold_names.json")
    try:
        with open(gold_path, encoding="utf-8") as f:
            for v in json.load(f).values():
                gold |= set(v)
    except Exception:
        pass

    guard_candidates: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    for text, n in freq_skip.most_common():
        if n < min_freq:
            continue
        item = {
            "text": text,
            "freq": n,
            "channels": dict(channels.get(text, {})),
            "categories": dict(categories.get(text, {})),
        }
        if text in placeholders:
            item["skipped_reason"] = "占位人名（必须脱敏），不入保护表"
            conflicts.append(item)
            continue
        if text in full_names or text in gold:
            item["skipped_reason"] = "命中真名库/金标，入保护表会造成真名漏脱"
            conflicts.append(item)
            continue
        guard_candidates.append(item)

    name_candidates = [
        {"text": t, "freq": n,
         "channels": dict(channels.get(t, {})),
         "categories": dict(categories.get(t, {}))}
        for t, n in freq_redact.most_common() if n >= min_freq
    ]

    # 同实体既 skip 又 redact —— 裁决不一致，需人工复核
    inconsistent = [
        {"text": t, "skip": freq_skip[t], "redact": freq_redact[t]}
        for t in set(freq_skip) & set(freq_redact)
        if freq_skip[t] >= min_freq or freq_redact[t] >= min_freq
    ]

    return {
        "scanned_files": files,
        "min_freq": min_freq,
        "guard_candidates": guard_candidates,
        "name_candidates": name_candidates,
        "conflicts": conflicts,
        "inconsistent": inconsistent,
        "suggestion": {
            "common_word_guards": [c["text"] for c in guard_candidates],
            "full_name_list": [c["text"] for c in name_candidates],
        },
    }


def render_report(result: Dict[str, Any]) -> str:
    lines = [
        "=" * 52,
        "Issue #54 D1 · 裁决记录挖掘（规则候选）",
        "=" * 52,
        f"扫描文件: {len(result['scanned_files'])} 个",
        f"最小频次: {result['min_freq']}",
        "",
        f"保护表候选（common_word_guards）: {len(result['guard_candidates'])} 条",
    ]
    for c in result["guard_candidates"][:20]:
        lines.append(f"  · {c['text']}  (×{c['freq']})")
    lines.append("")
    lines.append(f"名单候选（full_name_list）: {len(result['name_candidates'])} 条")
    for c in result["name_candidates"][:20]:
        lines.append(f"  · {c['text']}  (×{c['freq']})")
    if result["conflicts"]:
        lines.append("")
        lines.append(f"⚠ 互斥冲突（禁止入库）: {len(result['conflicts'])} 条")
        for c in result["conflicts"][:10]:
            lines.append(f"  · {c['text']} — {c.get('skipped_reason', '')}")
    if result["inconsistent"]:
        lines.append("")
        lines.append(f"⚠ 裁决不一致（需人工复核）: {len(result['inconsistent'])} 条")
        for c in result["inconsistent"][:10]:
            lines.append(f"  · {c['text']} — skip×{c['skip']} / redact×{c['redact']}")
    lines.extend(["", "（候选仅供人工评审后并入 config.json，本脚本不自动改配置）", ""])
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="裁决记录挖掘（Issue #54 D1）")
    ap.add_argument("--decisions", nargs="+", required=True,
                    help="裁决文件或目录（decisions*.json / gray*.json）")
    ap.add_argument("--min-freq", type=int, default=3, help="候选最小频次")
    ap.add_argument("--out", default=None, help="候选 JSON 输出路径")
    args = ap.parse_args()

    result = mine_decisions(args.decisions, min_freq=args.min_freq)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[输出] 候选已写入: {args.out}")
    print(render_report(result))


if __name__ == "__main__":
    main()
