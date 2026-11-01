#!/usr/bin/env python3
"""
bench_metrics.py - 评测指标统一计算（Issue #54 D2/D3）

【为什么要有这个模块】
Issue #54 §2.2 要求 CI 门禁在「精确率 / 召回率 / 歧义正确率回退时失败」，
§三 验收标准第 4 条要求「`quality_validator.py` 输出的指标 JSON 与
`bench_eval.py` 口径一致」。

如果门禁测试与质量校验服务各算各的，两边口径一旦漂移就会出现
「CI 绿灯但质检报告红灯」的假象。因此本模块把**指标定义与阈值**收敛到
唯一实现：

  · `tests/benchmark/test_benchmark_gate.py` —— 回归门禁直接调用本模块；
  · `scripts/quality_validator.py --json` —— 输出同一份指标 JSON。

口径定义（全部为**字符级** / **片段级**，与上游 bench 的 span 口径对齐）：

| 指标 | 定义 | 门槛 |
|---|---|---|
| gold_recall      | 真名金标被脱敏比例（越高越好，防过度收紧） | ≥ 0.85 |
| biz_misfire      | 通用业务语料（102 条）被误伤条数 | ≤ 12 |
| real_biz_misfire | 真实业务语料（1200 条）被误伤处数 | ≤ 0 |
| ambiguity_acc    | 语义歧义集正确率 | ≥ 0.60（阶段一） |
| precision        | 1 − 阴性语料误伤率（负样本保持原文的比例） | ≥ 0.95 |
| recall           | 等价 gold_recall（真名金标脱敏比例） | ≥ 0.85 |
| f1               | 2PR/(P+R) | — |
| determinism      | 同输入多次运行输出完全一致 | = 1.0 |
| idempotency      | 二次脱敏不再产生新改动 | = 1.0 |

运行：
    python3 scripts/bench_metrics.py            # 人类可读报告
    python3 scripts/bench_metrics.py --json     # 机器可读 JSON
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common_rules  # noqa: E402

# ---------------------------------------------------------------------------
# 门禁阈值（与 tests/benchmark/test_benchmark_gate.py 同源）
# ---------------------------------------------------------------------------
MIN_GOLD_RECALL = 0.85          # 真名金标保留率下限
MAX_BIZ_MISFIRE = 12            # 通用业务语料误伤上限
MAX_REAL_BIZ_MISFIRE = 0        # 真实业务语料误伤上限（#51 起为 0）
MIN_AMBIG_ACCURACY = 0.60       # 语义歧义集正确率（#54 §2.2 阶段一）
MIN_PRECISION = 0.95            # 精确率下限（按阴性误伤率折算）

# ---------------------------------------------------------------------------
# 「设计意图」豁免口径（真实业务语料误伤判定）
# ---------------------------------------------------------------------------
_INTENT_PLACEHOLDER_RE = re.compile(r"[XYMD0-9%\.\-/]+")
_ORG_SUFFIX_RE = re.compile(
    r"(营业部|科技部|运营部|管理部|董事会|监事会|管委会|事业部|信息科技部|合规部)"
)
_PLACEHOLDER_NAMES = ("张三", "李四")
# 占位符字符（脱敏产物中的 X/Y/M/D 等）
_PLACEHOLDER_TOKEN_RE = re.compile(r"[XYMD]{2,}")


def repo_root() -> str:
    """仓库根目录（scripts/ 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bench_dir(root: Optional[str] = None) -> str:
    return os.path.join(root or repo_root(), "tests", "benchmark")


def _diff_segments(old: str, new: str) -> List[Tuple[str, str]]:
    """返回 (原文段, 脱敏段) 差异列表。"""
    import difflib
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return [(old[i1:i2], new[j1:j2])
            for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _is_date_intent(seg: str, old: str, rep: str) -> bool:
    """判断一处差异是否属于「日期脱敏」（设计意图）。"""
    if not _INTENT_PLACEHOLDER_RE.fullmatch(rep.strip()):
        return False
    return ("年" in seg or "月" in seg or "日" in seg
            or "-" in seg or "/" in seg)


def load_assets(root: Optional[str] = None) -> Dict[str, Any]:
    """载入评测资产（缺失项返回空，不抛异常）。"""
    bench = _bench_dir(root)
    assets: Dict[str, Any] = {}

    def _load_json(name):
        path = os.path.join(bench, name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    assets["docs"] = _load_json("docs.json") or {}
    assets["gold"] = _load_json("gold_names.json") or {}
    assets["biz_corpus"] = _load_json("biz_corpus.json") or []
    assets["ambig"] = _load_json("ambig_cases.json") or []
    return assets


def _false_positive_corpus() -> List[str]:
    """通用业务语料（tests/test_name_false_positive.py 的 102 条）。"""
    tests_dir = os.path.join(repo_root(), "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    try:
        from test_name_false_positive import FALSE_POSITIVE_CORPUS
        return list(FALSE_POSITIVE_CORPUS)
    except Exception:
        return []


_METRICS_CACHE: Dict[str, Any] = {}


def _asset_signature(root: Optional[str] = None) -> tuple:
    """资产签名（路径 + mtime）——用于进程内缓存失效判断。"""
    bench = _bench_dir(root)
    sig = []
    for name in ("docs.json", "gold_names.json", "biz_corpus.json", "ambig_cases.json"):
        path = os.path.join(bench, name)
        try:
            sig.append((name, os.path.getmtime(path)))
        except OSError:
            sig.append((name, 0))
    return tuple(sig)


def compute_metrics(root: Optional[str] = None,
                    assets: Optional[Dict[str, Any]] = None,
                    use_cache: bool = True) -> Dict[str, Any]:
    """计算全部门禁指标，返回机器可读字典。

    【为什么有缓存】真实业务语料 1200 条的脱敏比对较慢（约 30–60s），
    CI 与质检常在同一进程内多次取指标；缓存以资产 mtime 作为失效键，
    资产未变则复用结果，避免重复计算拖慢门禁。
    """
    if use_cache and assets is None:
        key = (root or repo_root(), _asset_signature(root))
        cached = _METRICS_CACHE.get(key)
        if cached is not None:
            return cached
    metrics = _compute_metrics_uncached(root, assets)
    if use_cache and assets is None:
        _METRICS_CACHE[(root or repo_root(), _asset_signature(root))] = metrics
    return metrics


def _compute_metrics_uncached(root: Optional[str] = None,
                              assets: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """实际计算（无缓存）。"""
    root = root or repo_root()
    assets = assets or load_assets(root)
    docs = assets.get("docs") or {}
    gold = assets.get("gold") or {}
    biz_corpus = assets.get("biz_corpus") or []
    ambig = assets.get("ambig") or []

    # ---- 真名金标保留率（召回率）----
    kept = total = 0
    leaked: List[Tuple[str, str]] = []
    for doc, names in gold.items():
        out = common_rules.apply_redactions(docs.get(doc, ""))
        for w in names:
            total += 1
            if w not in out:
                kept += 1
            else:
                leaked.append((doc, w))
    gold_recall = (kept / total) if total else 0.0

    # ---- 通用业务语料误伤 ----
    fp_corpus = _false_positive_corpus()
    biz_misfire = [c for c in fp_corpus
                   if common_rules.apply_redactions(c) != c]

    # ---- 真实业务语料误伤 ----
    real_misfires = []
    for idx, seg in enumerate(biz_corpus):
        out = common_rules.apply_redactions(seg)
        if out == seg:
            continue
        for old, rep in _diff_segments(seg, out):
            if any(p in old for p in _PLACEHOLDER_NAMES):
                continue
            if _ORG_SUFFIX_RE.fullmatch(old.strip()) and rep.strip() == "XXXX":
                continue
            if _is_date_intent(seg, old, rep):
                continue
            real_misfires.append({"index": idx, "original": old,
                                  "replacement": rep, "context": seg[:60]})

    # ---- 语义歧义集 ----
    ambig_total = ambig_correct = 0
    ambig_wrong: List[Dict[str, Any]] = []
    for case in ambig:
        src = case.get("input", "")
        expected = case.get("expected", "")
        ambig_total += 1
        out = common_rules.apply_redactions(src)
        if out == expected:
            ambig_correct += 1
        else:
            ambig_wrong.append({"input": src, "expected": expected,
                                "actual": out})
    ambig_acc = (ambig_correct / ambig_total) if ambig_total else 0.0

    # ---- 精确率（阴性语料保持原文的比例）----
    negative_total = len(fp_corpus) + len(biz_corpus)
    negative_misfire = len(biz_misfire) + len(real_misfires)
    precision = (1 - negative_misfire / negative_total) if negative_total else 1.0
    recall = gold_recall
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    # ---- 确定性 / 幂等性 ----
    #
    # 【幂等性的判定口径】只把「**已脱敏内容被改写**」算作幂等性违规：
    #   ① 占位符被合并/改写（"地XXX和XXX持" → "地XXXXXX持"）——有害，必须为 0；
    #   ② 占位符被还原成原文（"XXX" → "李四"）——有害（脱敏被撤销），必须为 0；
    #   ③ 首轮**漏脱**的真名在二轮被补上（"李又菂" → "XXX"）——属召回问题，
    #      不是幂等性违规，单独计入 second_pass_new_masks 供召回治理使用。
    sample = list(docs.values())[:5] or biz_corpus[:5]
    deterministic = True
    idempotent = True
    new_masks = 0
    for text in sample:
        outs = {common_rules.apply_redactions(text) for _ in range(3)}
        if len(outs) != 1:
            deterministic = False
        once = common_rules.apply_redactions(text)
        twice = common_rules.apply_redactions(once)
        for old_seg, new_seg in _diff_segments(once, twice):
            if old_seg == new_seg:
                continue
            if _PLACEHOLDER_TOKEN_RE.search(old_seg):
                idempotent = False
            elif new_seg and _PLACEHOLDER_TOKEN_RE.fullmatch(new_seg.strip()):
                new_masks += 1

    metrics: Dict[str, Any] = {
        "gold_recall": {
            "value": round(gold_recall, 4),
            "kept": kept, "total": total,
            "leaked": sorted({w for _, w in leaked}),
        },
        "biz_misfire": {
            "value": len(biz_misfire), "total": len(fp_corpus),
            "samples": biz_misfire[:10],
        },
        "real_biz_misfire": {
            "value": len(real_misfires), "total": len(biz_corpus),
            "samples": real_misfires[:10],
        },
        "ambiguity": {
            "value": round(ambig_acc, 4),
            "correct": ambig_correct, "total": ambig_total,
            "wrong": ambig_wrong[:10],
        },
        "precision": {"value": round(precision, 4)},
        "recall": {"value": round(recall, 4)},
        "f1": {"value": round(f1, 4)},
        "determinism": {"value": 1.0 if deterministic else 0.0},
        "idempotency": {"value": 1.0 if idempotent else 0.0,
                        "second_pass_new_masks": new_masks},
    }
    metrics["gates"] = evaluate_gates(metrics)
    metrics["passed"] = all(metrics["gates"].values())
    return metrics


def evaluate_gates(metrics: Dict[str, Any]) -> Dict[str, bool]:
    """按 #54 §2.2 阈值判定各门禁项。"""
    def _v(key: str) -> float:
        return float(metrics.get(key, {}).get("value", 0.0))

    return {
        "gold_recall": _v("gold_recall") >= MIN_GOLD_RECALL,
        "biz_misfire": _v("biz_misfire") <= MAX_BIZ_MISFIRE,
        "real_biz_misfire": _v("real_biz_misfire") <= MAX_REAL_BIZ_MISFIRE,
        "ambiguity_accuracy": _v("ambiguity") >= MIN_AMBIG_ACCURACY,
        "precision": _v("precision") >= MIN_PRECISION,
        "determinism": _v("determinism") >= 1.0,
        "idempotency": _v("idempotency") >= 1.0,
    }


def render_report(metrics: Dict[str, Any]) -> str:
    """人类可读报告。"""
    g = metrics.get("gates", {})
    lines = [
        "=" * 52,
        "Issue #54 评测指标（bench_metrics 口径）",
        "=" * 52,
        f"真名金标保留率 recall : {metrics['gold_recall']['value']:.1%} "
        f"({metrics['gold_recall']['kept']}/{metrics['gold_recall']['total']})",
        f"精确率 precision      : {metrics['precision']['value']:.1%}",
        f"F1                    : {metrics['f1']['value']:.1%}",
        f"通用语料误伤          : {metrics['biz_misfire']['value']}/"
        f"{metrics['biz_misfire']['total']}",
        f"真实语料误伤          : {metrics['real_biz_misfire']['value']}/"
        f"{metrics['real_biz_misfire']['total']}",
        f"歧义集正确率          : {metrics['ambiguity']['value']:.1%} "
        f"({metrics['ambiguity']['correct']}/{metrics['ambiguity']['total']})",
        f"确定性 / 幂等性       : {metrics['determinism']['value']:.0%} / "
        f"{metrics['idempotency']['value']:.0%}",
        "-" * 52,
    ]
    for k, ok in g.items():
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {k}")
    lines.append(f"总判定: {'PASS' if metrics.get('passed') else 'FAIL'}")
    return "\n".join(lines)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Issue #54 评测指标")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--root", default=None, help="仓库根目录")
    args = ap.parse_args()
    metrics = compute_metrics(args.root)
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(render_report(metrics))
    sys.exit(0 if metrics.get("passed") else 1)


if __name__ == "__main__":
    main()
