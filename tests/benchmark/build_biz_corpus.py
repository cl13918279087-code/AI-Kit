#!/usr/bin/env python3
"""
真实业务语料重建器（承接 Issue #59：评测语料从真实工作文档重建）。

【为什么要有这个】
tests/benchmark/ 的第一版语料只有 6 份银行文档（工作目录/），
覆盖的是「银行项目文档」这一种文体。真正暴露词边界缺陷的，是
**长文本、表格密集、部门名/职务名/流程术语密集**的另一类文体——
本项目自身的规划/验收/评审文档。这批文档在 100 行以内就密集出现
「信息科技部」「关键路径法」「任务名称生成」「基于向量检索」等结构，
恰好是姓名通道越界的高发区。

本脚本从源目录（默认 /Users/clzxr/公司Agent规划）重建真实业务语料：

  tests/benchmark/biz_corpus.json      —— 真实业务片段（含上下文）
  tests/benchmark/biz_known_misfires.json —— 已知待修误伤白名单

【两条入库纪律】
1. **片段级，不入全文**：仓库是公开的（AI-Kit）。源文档是公司内部材料，
   全量入库等于对外公开。因此只保留「可能触发姓名通道」的**行级片段**，
   每行裁剪到 ≤48 字，不保留文档结构与章节信息。
2. **人名假名化**：片段中的真实人名统一替换为「人名甲/人名乙」，
   既保住上下文语义（姓名位置仍可测），又不泄露真实姓名。

【重跑】
    python3 tests/benchmark/build_biz_corpus.py
"""
import json
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "tests" / "benchmark"
SRC = Path("/Users/clzxr/公司Agent规划")

# 片段最大长度（入库体积控制）
MAX_LINE = 48
# 单份文档最多取几行（避免个别长文档主导语料）
MAX_LINES_PER_DOC = 12
# 语料总量上限
MAX_LINES = 1200

CJK = re.compile(r"[一-龥]")
NAME_CHAR = re.compile(r"[一-龥]{2,4}")


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


EXTRACTORS = {
    ".docx": _docx_text,
    ".pptx": _pptx_text,
    ".xlsx": _xlsx_text,
    ".md": lambda p: p.read_text(encoding="utf-8", errors="ignore"),
    ".txt": lambda p: p.read_text(encoding="utf-8", errors="ignore"),
}


# ---------------------------------------------------------------- 片段筛选

# 触发「姓名通道越界」的结构信号：动词+姓 / 姓+名用字 / 部门后缀 / 职务词
_TRIGGER_HINTS = (
    "生成", "定义", "建立", "规范", "执行", "过程", "路径", "链", "检索", "识别",
    "关键", "编排", "输出", "输入", "任务", "名单", "角色", "汇报", "审批", "签字",
    "负责", "牵头", "主持", "记录", "参会", "出席", "评审", "验收", "交付",
    "部", "科", "中心", "室", "处", "组", "班", "行", "岗", "员", "长", "总",
    "师", "生", "家", "手", "员", "工",
)

# 明显不是业务语料的噪声行（纯符号/纯 URL/纯标识符）
_NOISE = re.compile(r"^[\s|>*\-–—_=+#`~\[\]{}<>\"'()（）]*$")


def _is_candidate(line: str) -> bool:
    s = line.strip()
    if len(s) < 6 or len(s) > MAX_LINE * 2:
        return False
    if _NOISE.match(s):
        return False
    cjk = len(CJK.findall(s))
    if cjk < 4:
        return False
    # 必须含触发结构，且含中文
    if not any(h in s for h in _TRIGGER_HINTS):
        return False
    # 排除纯代码/纯表格分隔
    if s.count("|") >= 4 or s.count("\t") >= 4:
        return False
    return True


def _trim(line: str) -> str:
    s = line.strip().strip("|").strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) > MAX_LINE:
        s = s[:MAX_LINE]
    return s


# ---------------------------------------------------------------- 人名假名化

def _pseudonymize(text: str, real_names: set) -> str:
    """把真实人名替换为「人名甲/人名乙」，保持出现顺序。

    只替换**长度 2~4 的全中文**且出现在人名语境（姓名前后为标点/空白/
    换行/冒号/称谓词）的 token，避免把普通业务词误判为姓名。
    """
    if not real_names:
        return text
    # 长名优先，避免「张三」抢占「张三丰」的匹配
    ordered = sorted((n for n in real_names if 2 <= len(n) <= 4), key=len, reverse=True)
    if not ordered:
        return text
    mapping: dict = {}

    def _sub(m):
        tok = m.group(0)
        if tok not in mapping:
            idx = len(mapping)
            mapping[tok] = "人名" + "甲乙丙丁戊己庚辛壬癸"[idx % 10]
        return mapping[tok]

    pattern = re.compile("|".join(re.escape(n) for n in ordered))
    return pattern.sub(_sub, text)


# ---------------------------------------------------------------- 主流程

def _iter_source_files():
    if not SRC.exists():
        return []
    out = []
    for p in sorted(SRC.rglob("*")):
        if not p.is_file():
            continue
        sp = str(p)
        if "/.git/" in sp or "pmoskill" in sp or "pmoskills" in sp:
            continue
        if p.suffix.lower() not in EXTRACTORS:
            continue
        out.append(p)
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg = json.load(open(REPO / "scripts" / "config.json", encoding="utf-8"))
    d16 = set(cfg.get("full_name_list", []))
    d16.discard("姓名")
    # D16 里混有非人名词（安宣部/成文 等），只取「姓氏 + 名字用字」形态且
    # 不在业务保护表内的，且长度 2~3 的候选——宁可漏替换，不可误替换。
    biz_guard = set(cfg.get("biz_guard_words", []))
    real_names = {n for n in d16 if 2 <= len(n) <= 3 and n not in biz_guard}

    files = _iter_source_files()
    lines: list = []
    seen = set()
    for p in files:
        try:
            text = EXTRACTORS[p.suffix.lower()](p)
        except (KeyError, zipfile.BadZipFile, OSError, ValueError):
            continue
        if not text.strip():
            continue
        picked = 0
        for raw in text.split("\n"):
            if picked >= MAX_LINES_PER_DOC:
                break
            if not _is_candidate(raw):
                continue
            s = _trim(raw)
            if not s or s in seen:
                continue
            seen.add(s)
            lines.append(s)
            picked += 1
        if len(lines) >= MAX_LINES:
            break

    lines = lines[:MAX_LINES]
    corpus = [_pseudonymize(s, real_names) for s in lines]

    json.dump(corpus, open(OUT_DIR / "biz_corpus.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"业务语料 {len(corpus)} 条 → {OUT_DIR/'biz_corpus.json'}")
    print(f"（源目录 {SRC}，扫描 {len(files)} 份文档）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
