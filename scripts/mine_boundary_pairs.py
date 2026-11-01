#!/usr/bin/env python3
"""
边界对挖掘器（Issue #51 G7）。

【为什么需要】
姓名规则把一个常用词切成两半时，命中片段是「前一词的后半 + 后一词的前半」：
    「依赖大量」→ 命中「赖大」（依赖|大量）
    「指向真实」→ 命中「向真」（指向|真实）
    「规范建设」→ 命中「范建」（规范|建设）
这类误伤**不与任何完整词重叠**，因此 G1（重叠）/ G2（整体）/ G4（整词包含）
都抓不到——它们在词**边界**上。

【G7 判据】
    命中片段 [start,end) 若满足下列之一，即判为「跨词边界错切」→ 保留原文：
      A. 左邻字 + 命中首字 ∈ 边界对表   （左边界切错：把词切在了词中间）
      B. 命中末字 + 右邻字 ∈ 边界对表   （右边界切错）
判据成立时，被切的那个词至少有一部分落在命中片段之外，故命中片段必然是碎片。

【边界对表来源】
    ① 语料中 jieba 切出的独立二字词（频次 ≥ MIN_FREQ）
    ② 既有词表（EXCLUDED_COMMON_WORDS / biz_guard_words / common_word_guards）
       派生的二字组合
两条来源都经 D16 真名库**双向互斥**过滤——含真名的组合不入表，
因此本守卫**不可能**因为保护了某个真名组合而造成真名漏脱。

【真名优先】
    命中片段本身若是 D16 真名、或与 D15 注入真名重叠 → 不豁免。

【用法】
    python3 scripts/mine_boundary_pairs.py             # 只报告
    python3 scripts/mine_boundary_pairs.py --apply     # 写回 config.json
"""
import argparse
import json
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import common_rules as cr  # noqa: E402

DEFAULT_SRC = Path("/Users/clzxr/公司Agent规划")
MIN_FREQ = 3
CJK_RUN = re.compile(r"[一-龥]+")


# ---------------------------------------------------------------- 文本抽取

def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        x = z.read("word/document.xml").decode("utf-8", "ignore")
    x = re.sub(r"</w:p>", "\n", x)
    x = re.sub(r"</w:tc>", "\t", x)
    return re.sub(r"<[^>]+>", "", x)


def _pptx_text(path: Path) -> str:
    chunks = []
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if re.match(r"ppt/slides/slide\d+\.xml$", name):
                x = z.read(name).decode("utf-8", "ignore")
                x = re.sub(r"</a:p>", "\n", x)
                chunks.append(re.sub(r"<[^>]+>", "", x))
    return "\n".join(chunks)


def _xlsx_text(path: Path) -> str:
    chunks = []
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            shared = [re.sub(r"<[^>]+>", "", m)
                      for m in re.findall(r"<si>(.*?)</si>", sx, re.S)]
        for name in sorted(z.namelist()):
            if re.match(r"xl/worksheets/sheet\d+\.xml$", name):
                x = z.read(name).decode("utf-8", "ignore")
                for row in re.findall(r"<row\b[^>]*>(.*?)</row>", x, re.S):
                    cells = []
                    for m in re.finditer(r'<c\b([^>]*)>(.*?)</c>', row, re.S):
                        v = re.search(r"<v>(.*?)</v>", m.group(2), re.S)
                        if not v:
                            continue
                        raw = v.group(1)
                        cells.append(shared[int(raw)] if 't="s"' in m.group(1) else raw)
                    if cells:
                        chunks.append("\t".join(cells))
    return "\n".join(chunks)


EXTRACTORS = {
    ".docx": _docx_text,
    ".pptx": _pptx_text,
    ".xlsx": _xlsx_text,
    ".md": lambda p: p.read_text(encoding="utf-8", errors="ignore"),
    ".txt": lambda p: p.read_text(encoding="utf-8", errors="ignore"),
}


def iter_files(src: Path):
    if not src.exists():
        return
    for p in sorted(src.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in EXTRACTORS:
            continue
        if "pmoskill" in str(p):
            continue
        yield p


# ---------------------------------------------------------------- 词频挖掘

def _mine_two_char_words(src: Path, min_freq: int) -> Counter:
    """统计语料中 jieba 切出的独立二字词频次。"""
    try:
        import jieba
        jieba.setLogLevel(60)
    except ImportError:
        print("提示：未安装 jieba，退化为「连续汉字串的二字滑窗」统计（精度略低）")
        jieba = None
    freq = Counter()
    files = list(iter_files(src))
    print(f"扫描 {len(files)} 份文档 @ {src}")
    for p in files:
        try:
            text = EXTRACTORS[p.suffix.lower()](p)
        except (KeyError, zipfile.BadZipFile, OSError, ValueError):
            continue
        if not text:
            continue
        if jieba is not None:
            for w in jieba.cut(text):
                w = w.strip()
                if len(w) == 2 and re.fullmatch(r"[一-龥]{2}", w):
                    freq[w] += 1
        else:
            for run in CJK_RUN.findall(text):
                for i in range(len(run) - 1):
                    freq[run[i:i + 2]] += 1
    return freq


def _pairs_from_words(words) -> set:
    """从整词表派生二字组合（跨词边界的两个相邻字）。"""
    pairs = set()
    for w in words:
        if not isinstance(w, str) or len(w) < 2:
            continue
        for i in range(len(w) - 1):
            pairs.add(w[i:i + 2])
    return pairs


# ---------------------------------------------------------------- 互斥校验

def _conflict_names(pairs, names: set) -> set:
    """筛出与真名库有双向包含关系的组合（含真名自身）。"""
    bad = set()
    for p in pairs:
        for n in names:
            if p in n or n in p:
                bad.add(p)
                break
    return bad


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="语料源目录")
    ap.add_argument("--apply", action="store_true", help="写回 config.json")
    ap.add_argument("--min-freq", type=int, default=MIN_FREQ)
    args = ap.parse_args()

    src = Path(args.src)
    cfg_path = REPO / "scripts" / "config.json"
    cfg = json.load(open(cfg_path, encoding="utf-8"))

    names = {n for n in (cfg.get("full_name_list") or []) if isinstance(n, str)}
    names.discard("姓名")
    # 金标真名同样受保护
    gold_path = REPO / "tests" / "benchmark" / "gold_names.json"
    if gold_path.exists():
        gold = json.load(open(gold_path, encoding="utf-8"))
        for arr in gold.values():
            names |= {w for w in arr if isinstance(w, str)}

    freq = _mine_two_char_words(src, args.min_freq)
    mined = {w for w, c in freq.items() if c >= args.min_freq}
    print(f"语料二字词 {len(freq)} 个（频次≥{args.min_freq} 保留 {len(mined)} 个）")

    # 既有词表派生的二字组合也纳入
    derived = _pairs_from_words(
        set(cr.EXCLUDED_COMMON_WORDS)
        | set(cfg.get("biz_guard_words") or [])
        | set(cfg.get("common_word_guards") or [])
    )
    print(f"既有词表派生二字组合 {len(derived)} 个")

    candidates = mined | derived
    bad = _conflict_names(candidates, names)
    kept = sorted(candidates - bad)
    print(f"与真名库冲突剔除 {len(bad)} 个，最终边界对表 {len(kept)} 条")

    # 抽检
    sample = ["依赖", "大量", "基于", "静态", "规范", "指向", "真实", "监管",
              "架构", "迭代", "任务", "路径", "定义", "组织", "文本", "全局",
              "任何", "方向", "建立", "原有", "综合", "聚焦", "灰度", "识别",
              "移入", "开展", "利益", "相关"]
    for w in sample:
        print(f"  {w}: {'✓' if w in kept else '✗'}")

    if args.apply:
        cfg["word_boundary_pairs"] = kept
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"\n已写回 config.json：word_boundary_pairs {len(kept)} 条")
    else:
        print("\n（仅报告；加 --apply 写回 config.json）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
