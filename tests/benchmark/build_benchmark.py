#!/usr/bin/env python3
"""
评测基准构建器（Issue #54 D2：评测基准入库做 CI 回归门禁）。

【为什么需要入库】
历史评测资产（119 份真实文档 + 824 条真名金标）曾寄居于工作区临时目录，
清理工作区时随之丢失，且从未进入版本库——这导致：
  1. 任何规则改动都**无法验证「零变化」**（无基线可比）；
  2. 评估脚本依赖临时路径，CI 与他人环境不可复现。

本脚本把评测资产**固化到仓库内**：
  tests/benchmark/docs.json      —— 语料文本（从 工作目录/ 的源文档抽取）
  tests/benchmark/gold_names.json —— 每份语料的真名金标（自动挖掘 + 人工校对）

【金标挖掘方法】
  主路径 —— **源文档 vs 标准版脱敏产物 diff**（Issue #54 提出的
          「全文 token diff + 偏移回溯」）：标准版把真名遮成 XXX，
          因此 diff 中「源侧是 2~4 字中文、目标侧是 XXX」的片段
          **就是被标准版判定为真名的原文**——这是零人工干预、
          与标准版口径完全一致的金标来源。
  辅路径 —— **角色锚点上下文 ∩ D16 精确全名库**（双证据取交集）：
          覆盖无标准版配对的语料；「组长：邹阳」入选，
          「成员：业务和技术骨干」因「骨干」不在 D16 而排除。

【重跑】
  python3 tests/benchmark/build_benchmark.py
输出：
  tests/benchmark/docs.json
  tests/benchmark/gold_names.json
"""
import difflib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

WORK = REPO / "工作目录"
OUT_DIR = REPO / "tests" / "benchmark"

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
    """按行抽取 xlsx 文本（同一行的单元格用 \\t 连接，保留行内并列结构）。"""
    chunks = []
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            shared = [re.sub(r"<[^>]+>", "", m) for m in
                      re.findall(r"<si>(.*?)</si>", sx, re.S)]
        for name in sorted(z.namelist()):
            if re.match(r"xl/worksheets/sheet\d+\.xml$", name):
                x = z.read(name).decode("utf-8", "ignore")
                for row in re.findall(r"<row\b[^>]*>(.*?)</row>", x, re.S):
                    cells = []
                    for m in re.finditer(r"<c\b([^>]*)>(.*?)</c>", row, re.S):
                        attrs, body = m.group(1), m.group(2)
                        v = re.search(r"<v>(.*?)</v>", body, re.S)
                        if not v:
                            # 内联字符串 <is><t>...</t></is>
                            t = re.search(r"<t[^>]*>(.*?)</t>", body, re.S)
                            if t:
                                cells.append(t.group(1))
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


EXTRACTORS = {".docx": _docx_text, ".pptx": _pptx_text, ".xlsx": _xlsx_text}

# ---------------------------------------------------------------- 金标挖掘

# 角色锚点：其后紧跟的 token 极可能是人名
ROLE_ANCHORS = (
    "主持人", "记录人", "与会人员", "参会人员", "出席人员", "与会人",
    "组长", "副组长", "常务副组长", "成员", "组员", "负责人", "联系人",
    "项目经理", "产品经理", "IT组长", "业务组长", "技术组长", "测试组长",
    "需求组长", "开发组长", "顾问", "专家", "骨干", "组长：",
)

# 顿号/逗号并列的人名块
_LIST_SEP = r"[、，,]"


def mine_gold(text: str, d16: set, name_list: bool = False) -> set:
    """上下文挖掘：角色锚点 ∩ D16（覆盖无标准版配对的语料）。"""
    found = set()

    # 纯人名清单——每行一个姓名，D16 命中即金标
    if name_list:
        for line in text.split("\n"):
            tok = line.strip()
            if 2 <= len(tok) <= 4 and tok in d16:
                found.add(tok)
        return found

    # 角色锚点后紧跟的 2~4 字 token
    for anchor in ROLE_ANCHORS:
        for m in re.finditer(re.escape(anchor) + r"\s*[:：]?\s*([一-龥]{2,4})", text):
            tok = m.group(1)
            if tok in d16:
                found.add(tok)

    # 顿号/逗号并列块（块内至少一项命中 D16 → 全块采信）
    for m in re.finditer(r"([一-龥]{2,4}(?:" + _LIST_SEP + r"[一-龥]{2,4}){1,})", text):
        items = [i for i in re.split(_LIST_SEP, m.group(1)) if i]
        if any(i in d16 for i in items):
            found.update(i for i in items if i in d16)

    # 角色锚点后的并列块
    for m in re.finditer(
        re.escape("：") + r"\s*([一-龥]{2,4}(?:" + _LIST_SEP + r"[一-龥]{2,4}){1,})", text
    ):
        items = [i for i in re.split(_LIST_SEP, m.group(1)) if i]
        if any(i in d16 for i in items):
            found.update(i for i in items if i in d16)

    # 表格中每行独立单元格形态的人名（xlsx 抽取后单元格以 \t 分隔）
    for line in text.split("\n"):
        if "\t" not in line:
            continue
        for cell in line.split("\t"):
            tok = cell.strip()
            if 2 <= len(tok) <= 4 and tok in d16:
                found.add(tok)

    return found


# ---------------------------------------------------------------- diff 反推

# 标准版用 XXX / XX 遮盖真名；源侧被替换成占位符的中文 token 即真名
_MASK_RE = re.compile(r"^X{2,4}$")
_CJK_TOKEN_RE = re.compile(r"^[一-龥]{2,4}$")


def gold_from_diff(src_text: str, std_text: str) -> set:
    """从「源文档 vs 标准版」diff 中反推真名金标。

    判据：diff 的 replace/delete 段中，源侧是 2~4 字纯中文、目标侧是
    X 占位符（XXX/XX）→ 该源侧 token 被标准版判为真名 → 入金标。

    这个口径**完全对齐标准版**，因此「本实现漏掉了标准版能脱掉的人名」
    会被基准直接抓到——这正是 #54 要求的 CI 门禁能力。
    """
    found = set()
    sm = difflib.SequenceMatcher(None, src_text, std_text, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        src_seg = src_text[i1:i2].strip()
        std_seg = std_text[j1:j2].strip()
        if not src_seg or not std_seg:
            continue
        if not _CJK_TOKEN_RE.match(src_seg):
            continue
        # 目标侧：全为 X 占位（可能带少量标点残留）
        if not _MASK_RE.match(re.sub(r"[^\s]", "", std_seg)):
            continue
        found.add(src_seg)
    return found


def _find_std_pair(src: Path) -> Path | None:
    """为源文档找对应的「标准版脱敏产物」。

    标准版文件名有两个变形：
      1. 前缀把银行名换成 XX（`平安信用卡…` → `XX信用卡…`、`郑州银行…` → `XX银行…`）
      2. 日期被替换成 YYYYMMDD（`…v0.4-20201019` → `…v0.4-YYYYMMDD`）
    因此匹配时剥离「银行名前缀 + 非中文部分 + 脱敏标准版后缀」，
    比较剩余的**中文尾部主干**。
    """
    def _core(name: str) -> str:
        s = name
        s = re.sub(r"脱敏标准版$|脱敏版$", "", s)
        s = re.sub(r"[0-9A-Za-z_\-]+", "", s)     # 版本号/日期/XX
        s = s.replace("+", "")
        s = re.sub(r"[。，,、]", "", s)
        return s

    def _tail(core: str, n: int = 6) -> str:
        """取中文主干尾部 n 字（银行名前缀长度不定，尾部才是稳定特征）。"""
        return core[-n:] if len(core) > n else core

    src_core = _core(src.stem)
    src_tail = _tail(src_core)
    best = None
    best_score = None
    for cand in sorted(WORK.iterdir()):
        if cand == src or cand.suffix.lower() not in EXTRACTORS:
            continue
        c = cand.stem
        if "脱敏标准版" not in c and not c.endswith("脱敏版"):
            continue
        c_core = _core(c)
        if not c_core or not src_core:
            continue
        c_tail = _tail(c_core)
        # 中文主干尾部一致 → 视为同一文档
        if src_tail == c_tail:
            score = abs(len(c_core) - len(src_core))
            if best_score is None or score < best_score:
                best, best_score = cand, score
    return best


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg = json.load(open(SCRIPTS / "config.json", encoding="utf-8"))
    d16 = set(cfg.get("full_name_list", []))
    d16.discard("姓名")

    # 源文档清单（排除脱敏产物/中间产物）
    sources = []
    for p in sorted(WORK.iterdir()):
        if p.suffix.lower() not in EXTRACTORS:
            continue
        name = p.name
        if "脱敏" in name or "_iter" in name or name.startswith("test_"):
            continue
        sources.append(p)

    docs, gold = {}, {}
    for p in sources:
        try:
            text = EXTRACTORS[p.suffix.lower()](p)
        except (KeyError, zipfile.BadZipFile, OSError) as e:
            print(f"  跳过 {p.name}: {e}")
            continue
        if not text.strip():
            continue
        key = p.name
        docs[key] = text

        # 主路径：源 vs 标准版 diff 反推
        names = set()
        std = _find_std_pair(p)
        if std is not None:
            try:
                std_text = EXTRACTORS[std.suffix.lower()](std)
                names = gold_from_diff(text, std_text)
            except (KeyError, zipfile.BadZipFile, OSError):
                pass

        # 辅路径：角色锚点上下文 ∩ D16
        is_name_list = text.lstrip().startswith("姓名")
        names |= mine_gold(text, d16, name_list=is_name_list)

        if names:
            gold[key] = sorted(names)
        src_tag = f"diff←{std.name}" if std is not None else "上下文"
        print(f"  {p.name}: {len(text)} 字, 金标 {len(names)} 个 [{src_tag}]")

    json.dump(docs, open(OUT_DIR / "docs.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(gold, open(OUT_DIR / "gold_names.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    total = sum(len(v) for v in gold.values())
    print(f"\n语料 {len(docs)} 份，真名金标 {total} 条 → {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
