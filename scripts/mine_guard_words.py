#!/usr/bin/env python3
"""
整词保护表挖掘器（Issue #51 G4）。

【为什么需要】
姓名规则以「姓氏 + 名字池字」的闭集形态切词，在真实业务长文本里会命中
大量**本身是完整术语、只是恰好由姓+名用字拼成**的词：
    「编排链」「织架构」「范定义」「焦三大」「务名」「程中」「于向」
手工往保护表里枚举属打地鼠——真实语料的新术语会持续冒出来。
本脚本把它变成**可重复的挖掘动作**：跑一遍真实语料，产出候选词表，
人工/规则复核后写回 config.json 的 `common_word_guards`。

【挖掘口径】
1. 跑一遍规则，对**每一处误伤**回溯其**真实上下文**，从上下文里切出
   候选整词（2~6 字中文），而不是只记被替换掉的那个碎片。
2. 候选词必须**满足全部**才入表：
   a. 在语料中出现频次 ≥ MIN_FREQ（默认 3）——偶发词不值得保护；
   b. 与 D16 全名库**双向互斥**：既不是任何真名，也不被任何真名包含，
      且不包含任何真名——否则会挡住真名脱敏；
   c. 不在 `biz_guard_words` 里（避免与既有表语义重复）；
   d. 候选词本身**确实会被当前规则误伤**（否则是无效词条）。
3. 输出候选清单 + 建议入表词表，供人工确认后回写 config.json。

【用法】
    python3 scripts/mine_guard_words.py                 # 只报告
    python3 scripts/mine_guard_words.py --apply        # 写回 config.json
    python3 scripts/mine_guard_words.py --src <目录>    # 指定语料目录
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
MIN_LEN = 2
MAX_LEN = 6
# 候选词不得以这些字符结尾/开头（姓氏字位置常被误切）
_CJK = re.compile(r"[一-龥]")
# 整词判定用的字符类（_CJK 是字符类集合，需 match 而非 fullmatch）
_CJK_RE = re.compile(r"[一-龥]+")

# jieba 词典词集（用于「候选词必须是真实存在的词」这一硬约束）
_JIEBA_DICT = None


def _load_jieba_dict():
    """载入 jieba 词典词集；jieba 不可用时返回 None（降级放行）。"""
    global _JIEBA_DICT
    if _JIEBA_DICT is not None:
        return _JIEBA_DICT
    try:
        import jieba
        jieba.initialize()
        _JIEBA_DICT = set(jieba.dt.FREQ.keys())
    except Exception:
        _JIEBA_DICT = None
    return _JIEBA_DICT

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
                    for m in re.finditer(r"<c\b([^>]*)>(.*?)</c>", row, re.S):
                        attrs, body = m.group(1), m.group(2)
                        v = re.search(r"<v>(.*?)</v>", body, re.S)
                        if not v:
                            continue
                        raw = v.group(1)
                        if 't="s"' in attrs:
                            try:
                                cells.append(shared[int(raw)])
                            except (ValueError, IndexError):
                                pass
                        else:
                            cells.append(raw)
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
        sp = str(p)
        if "/.git/" in sp or "pmoskill" in sp:
            continue
        yield p


# ---------------------------------------------------------------- 误伤定位


def _diff_fragments(src: str, out: str):
    """返回 [(源侧片段, 源侧区间)]，仅限被改动的非空片段。"""
    import difflib
    a = src.split("\n")
    b = out.split("\n")
    if len(a) != len(b):
        return []
    frags = []
    for la, lb in zip(a, b):
        if la == lb:
            continue
        sm = difflib.SequenceMatcher(None, la, lb, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            seg = la[i1:i2]
            if seg.strip():
                frags.append((seg, i1, i2))
    return frags


def _is_complete_term(word: str) -> bool:
    """判断候选词是否是**完整术语**（两端都不悬挂单字）。

    jieba 把「编排链路」切为 编排|链路——两端都是双字词，说明它是一个
    由两个完整词构成的复合术语；而「包括董事会报」切为 包括|董事会|报，
    尾部的「报」是挂在「董事会」后面的单字残片，说明它只是切错的碎片。

    判据：jieba 分词后的**首末两个 token 长度均 ≥ 2**。
    jieba 不可用时放行（由其他条件兜底）。
    """
    try:
        import jieba
    except Exception:
        return True
    try:
        toks = [t for t in jieba.cut(word, cut_all=False) if t and _CJK_RE.match(t)]
    except Exception:
        return True
    if not toks:
        # 整词被 jieba 视为一个不可分单元 → 视为完整术语
        return True
    if len(toks) == 1:
        return len(toks[0]) >= MIN_LEN
    return len(toks[0]) >= 2 and len(toks[-1]) >= 2


def _candidate_words(line: str, i1: int, i2: int):
    """从误伤位置切出候选整词（2~6 字中文）。

    【口径：jieba 独立词优先】
    早期版本用「向两侧滑窗扩 0~3 字」枚举候选，产出大量**跨词碎片**
    （如「关键路径上里」「何建立有效的」「包括董事会报」）——
    这些根本不是词，入表只会让保护表越来越大、命中率越来越低。

    现在的口径：
      ① 优先取 jieba 切出的**独立词**（该词完整覆盖误伤区间）；
      ② 若误伤区间跨越了 jieba 的词边界，则取**包含该区间的最短连续词组**
         （相邻 2 个 jieba 词的拼接），而不是任意滑窗；
      ③ jieba 不可用时降级为「误伤区间本身 ± 1 字」的有限枚举。
    """
    out = set()
    toks = None
    try:
        import jieba
        toks = [(t.start, t.end, t.word) for t in jieba.tokenize(line)
                if _CJK.fullmatch(t.word)]
    except Exception:
        toks = None

    if toks:
        # ① 独立词完整覆盖误伤区间
        for s, e, w in toks:
            if s <= i1 and i2 <= e and MIN_LEN <= len(w) <= MAX_LEN:
                out.add(w)
        # ② 跨词边界：取覆盖该区间的最短连续词组（相邻 2 词拼接）
        for idx, (s1, e1, w1) in enumerate(toks):
            if e1 < i1:
                continue
            for idx2 in range(idx, min(idx + 3, len(toks))):
                s2, e2, w2 = toks[idx2]
                if e2 < i2:
                    continue
                if s2 <= i1 and i2 <= e2:
                    group = line[s2:e2]
                    if MIN_LEN <= len(group) <= MAX_LEN:
                        out.add(group)
                    break
        if out:
            return out

    # ③ 降级：有限枚举（仅 ±1 字，避免碎片泛滥）
    for left in range(0, 2):
        for right in range(0, 2):
            s = i1 - left
            e = i2 + right
            if s < 0 or e > len(line):
                continue
            w = line[s:e]
            if not (MIN_LEN <= len(w) <= MAX_LEN):
                continue
            if not _CJK.fullmatch(w):
                continue
            if not (s <= i1 and i2 <= e):
                continue
            out.add(w)
    return out


# ---------------------------------------------------------------- 互斥校验


def _conflicts_with_names(word: str, d16: set) -> bool:
    """与 D16 真名库是否双向冲突（有包含关系即冲突）。"""
    for n in d16:
        if word in n or n in word:
            return True
    return False


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
    d16 = set(cfg.get("full_name_list", []))
    d16.discard("姓名")
    biz_guard = set(cfg.get("biz_guard_words", []))
    existing = set(cfg.get("common_word_guards", []))

    # 语料词频
    freq = Counter()
    misfire_ctx = []          # [(文件, 行, 区间)]
    files = list(iter_files(src))
    print(f"扫描 {len(files)} 份文档 @ {src}")

    for p in files:
        try:
            text = EXTRACTORS[p.suffix.lower()](p)
        except (KeyError, zipfile.BadZipFile, OSError, ValueError):
            continue
        if not text.strip():
            continue
        for w in _CJK.findall(text):
            pass
        for m in re.finditer(r"[一-龥]{%d,%d}" % (MIN_LEN, MAX_LEN), text):
            freq[m.group(0)] += 1
        out = cr.apply_redactions(text)
        if out == text:
            continue
        for seg, i1, i2 in _diff_fragments(text, out):
            misfire_ctx.append((p.name, seg))

    print(f"误伤片段 {len(misfire_ctx)} 处")

    # 从误伤上下文汇总候选
    cand = Counter()
    for name, seg in misfire_ctx:
        cand[seg.strip()] += 1

    # 候选词表 = jieba 独立词 ∩ 高频 ∩ 非真名冲突 ∩ 非已有保护词 ∩ 非占位名
    # 【为什么用 jieba 独立词而不是 n-gram 频次】
    #   n-gram 频次会把「包括董事会报」「不替代架构师」这类**跨词碎片**
    #   也算成高频词——它们不是词，入表只会让保护表虚胖、命中率归零。
    #   jieba 独立词口径保证入表的每个条目都是**真实存在的词**。
    placeholder_names = set(cfg.get("placeholder_names") or [])
    _load_jieba_dict()
    proposals = {}
    for w, n in freq.items():
        if n < args.min_freq:
            continue
        if w in biz_guard or w in existing:
            continue
        if w in placeholder_names:
            continue
        if _conflicts_with_names(w, d16):
            continue
        # 必须**两端都不悬挂单字**才算一个完整术语。
        #   「编排链路」→ jieba 切为 编排|链路，两端都是双字词 → 合格
        #   「包括董事会报」→ 切为 包括|董事会|报，尾部「报」是悬挂单字 → 不合格
        #   「不替代架构师」→ 切为 不|替代|架构师，首部「不」是悬挂单字 → 不合格
        if not _is_complete_term(w):
            continue
        # 该词是否会被规则误伤（含作为子串被切成姓名）
        if not _is_misfired(w):
            continue
        proposals[w] = n

    # 按「误伤覆盖度」排序：能挡住多少处误伤
    coverage = Counter()
    for w in proposals:
        for name, seg in misfire_ctx:
            if w in seg or seg in w:
                coverage[w] += 1
    ranked = sorted(proposals.items(), key=lambda kv: (-coverage[kv[0]], -kv[1]))

    print(f"候选保护词 {len(ranked)} 个（频次≥{args.min_freq}，与真名库互斥）\n")
    for w, n in ranked[:120]:
        print(f"  覆盖{coverage[w]:>3}处  频次{n:>4}  {w}")

    if args.apply:
        # 只取「覆盖度 ≥2」的高价值词，避免保护表膨胀
        picked = [w for w, _ in ranked if coverage[w] >= 2]
        new_list = sorted(existing | set(picked))
        cfg["common_word_guards"] = new_list
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"\n已写回 config.json：common_word_guards {len(new_list)} 条"
              f"（新增 {len(picked)} 条）")
    else:
        print("\n（仅报告；加 --apply 写回 config.json）")
    return 0


def _is_misfired(word: str) -> bool:
    """该词单独放入上下文后是否仍被规则误伤。"""
    probe = f"本{word}内容"
    return cr.apply_redactions(probe) != probe


if __name__ == "__main__":
    sys.exit(main())
