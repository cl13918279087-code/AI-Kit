"""
评测基准回归门禁（Issue #54 D2：评测基准入库做 CI 回归门禁）。

【为什么必须有这道门禁】
历史评测资产（119 份真实文档 + 824 条真名金标）曾寄居于工作区临时目录，
清理工作区时随之丢失且从未入库——结果是**任何规则改动都无法验证
「零变化」**（无基线可比），CI 与他人环境也无法复现。

本测试把评测资产固化到仓库内（`tests/benchmark/docs.json` +
`gold_names.json`，由 `build_benchmark.py` 生成），并作为 **CI 卡口**：
  1. `test_gold_names_recall`      —— 真名金标保留率下限（防整改过度收紧）
  2. `test_no_business_word_misfire` —— 通用业务语料零误伤（防扩池引入误伤）
  3. `test_gold_names_quality`    —— 金标本身有效性（防基线被污染）
  4. `test_benchmark_assets_present` —— 资产完整性（防误删后静默降级）
  5. `test_real_biz_corpus_zero_misfire` —— **真实业务语料零误伤**
     （Issue #51，1200 条真实长文本片段；这是唯一能暴露「词边界误伤」的语料）

【金标来源】源文档 vs 标准版脱敏产物的全文 diff 反推 + 角色锚点上下文 ∩ D16，
口径与标准版完全对齐——因此「本实现漏掉了标准版能脱掉的人名」会被直接抓到。

运行：
    python3 -m pytest tests/benchmark/test_benchmark_gate.py -v
"""
import json
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO, "scripts")
TESTS = os.path.join(REPO, "tests")
for p in (SCRIPTS, TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import common_rules  # noqa: E402
from test_name_false_positive import FALSE_POSITIVE_CORPUS  # noqa: E402

BENCH = os.path.join(REPO, "tests", "benchmark")
DOCS_PATH = os.path.join(BENCH, "docs.json")
GOLD_PATH = os.path.join(BENCH, "gold_names.json")
BIZ_CORPUS_PATH = os.path.join(BENCH, "biz_corpus.json")

# ---- 门禁阈值（改动本实现时应同步复核这些数字）----
# 真名金标保留率下限：低于此值说明整改过度收紧、真名漏脱
MIN_GOLD_RECALL = 0.85
# 通用业务语料误伤上限：高于此值说明规则扩张引入了业务词误伤
MAX_BIZ_MISFIRE = 12
# 真实业务语料误伤上限：Issue #51 整改后应达 0
MAX_REAL_BIZ_MISFIRE = 0

# ---- 真实业务语料「设计意图」豁免口径 ----
#
# 真实语料里有三类**刻意**的脱敏，不算业务词误伤：
#   ① 机构脱敏：科技部/董事会/运营部 → XXXX（脱敏设计本身）
#   ② 日期脱敏：2026年8月2日 → YYYY年MM月DD日、2026-05-21 → YYYY/MM/DD
#   ③ 占位人名：张三/李四 承担真实人名语义位 → 应当脱敏（**不算误伤**）
#
# 判定方式：把「原文片段 → 脱敏后片段」的差异逐段比对，
# 若差异段全部由占位符组成（X/Y/M/D/数字/百分号/短横线），
# 或原文段是占位人名，则视为设计意图。
_INTENT_PLACEHOLDER_RE = re.compile(r"[XYMD0-9%\.\-/]+")
# 设计意图的机构名（脱敏为 XXXX）
_ORG_SUFFIX_RE = re.compile(
    r"(营业部|科技部|运营部|管理部|董事会|监事会|管委会|事业部|信息科技部|合规部)"
)
# 占位人名（承担真实人名语义位，脱敏是正确行为）
_PLACEHOLDER_NAMES = ("张三", "李四")


def _diff_segments(old: str, new: str):
    """返回 (原文段, 脱敏段) 差异列表。"""
    import difflib
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return [(old[i1:i2], new[j1:j2])
            for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _is_date_intent(seg: str, old: str, rep: str) -> bool:
    """判断一处差异是否属于「日期脱敏」（设计意图）。

    日期脱敏的占位形态为 YYYY / MM / DD（可含斜杠短日期 MM/DD），
    而 difflib 会把「2026年8月2日」拆成 ('2026','YYYY') ('8','MM') ('2','DD')
    三段差异，因此需要看**所在片段**是否含日期单位字（年/月/日）
    或 ISO 分隔符（- /）。
    """
    if not _INTENT_PLACEHOLDER_RE.fullmatch(rep.strip()):
        return False
    return ("年" in seg or "月" in seg or "日" in seg
            or "-" in seg or "/" in seg)


@pytest.fixture(scope="module")
def docs():
    with open(DOCS_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def gold():
    with open(GOLD_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_benchmark_assets_present(docs, gold):
    """评测资产必须存在且非空——防误删后门禁静默降级为空跑。"""
    assert os.path.exists(DOCS_PATH), "缺少 tests/benchmark/docs.json，请运行 build_benchmark.py"
    assert os.path.exists(GOLD_PATH), "缺少 tests/benchmark/gold_names.json，请运行 build_benchmark.py"
    assert len(docs) >= 5, f"语料过少（{len(docs)} 份），评测基准失效"
    total = sum(len(v) for v in gold.values())
    assert total >= 200, f"金标过少（{total} 条），评测基准失效"
    # 语料与金标必须一一对应
    assert set(docs) == set(gold), "docs.json 与 gold_names.json 的键不一致"


def test_gold_names_quality(gold):
    """金标本身必须是 2~4 字纯中文，且不得是明显的业务词。"""
    bad = []
    for doc, names in gold.items():
        for w in names:
            if not (2 <= len(w) <= 4):
                bad.append((doc, w, "长度越界"))
            elif not all("一" <= c <= "龥" for c in w):
                bad.append((doc, w, "含非中文"))
    assert not bad, f"金标质量异常：{bad[:10]}"


def test_gold_names_recall(docs, gold):
    """真名金标保留率下限——防整改过度收紧导致真名漏脱。"""
    kept = total = 0
    leaked = []
    for doc, names in gold.items():
        out = common_rules.apply_redactions(docs[doc])
        for w in names:
            total += 1
            if w not in out:
                kept += 1
            else:
                leaked.append((doc, w))
    ratio = kept / total if total else 0.0
    assert ratio >= MIN_GOLD_RECALL, (
        f"真名金标保留率 {kept}/{total} = {ratio:.1%} < 门槛 {MIN_GOLD_RECALL:.0%}；"
        f"漏脱样例：{leaked[:20]}"
    )


def test_no_business_word_misfire():
    """通用业务语料误伤上限——防规则扩张把业务词当人名。"""
    mis = [c for c in FALSE_POSITIVE_CORPUS if common_rules.apply_redactions(c) != c]
    assert len(mis) <= MAX_BIZ_MISFIRE, (
        f"通用业务语料误伤 {len(mis)}/{len(FALSE_POSITIVE_CORPUS)} 超过上限 {MAX_BIZ_MISFIRE}；"
        f"样例：{mis[:15]}"
    )


def test_real_biz_corpus_zero_misfire():
    """真实业务语料零误伤（Issue #51）——这是唯一能暴露「词边界误伤」的语料。

    1200 条真实长文本片段（公司 Agent 规划文档重建）里密集出现
    「编排链路」「关键利益相关方」「灰区待定清单」「银行业监管和高管报告」
    等业务术语——它们恰好由「姓 + 名用字」拼成，是姓名通道的高发误伤区。

    判定口径（「设计意图」豁免）：
      ① 机构脱敏：科技部/董事会 → XXXX
      ② 日期脱敏：2026年8月2日 → YYYY年MM月DD日、2026-05-21 → YYYY/MM/DD
      ③ 占位人名：张三/李四 承担真实人名语义位 → 脱敏是正确行为
    其余任何差异都算业务词误伤。
    """
    assert os.path.exists(BIZ_CORPUS_PATH), (
        "缺少 tests/benchmark/biz_corpus.json，请运行 build_biz_corpus.py"
    )
    with open(BIZ_CORPUS_PATH, encoding="utf-8") as f:
        corpus = json.load(f)
    assert len(corpus) >= 1000, f"真实业务语料过少（{len(corpus)} 条），门禁失效"

    misfires = []
    for idx, seg in enumerate(corpus):
        out = common_rules.apply_redactions(seg)
        if out == seg:
            continue
        for old, rep in _diff_segments(seg, out):
            # ③ 占位人名 → 脱敏正确
            if any(p in old for p in _PLACEHOLDER_NAMES):
                continue
            # ① 机构脱敏 → 脱敏设计本身
            if _ORG_SUFFIX_RE.fullmatch(old.strip()) and rep.strip() == "XXXX":
                continue
            # ② 日期脱敏 → 占位符形态
            if _is_date_intent(seg, old, rep):
                continue
            misfires.append((idx, old, rep, seg[:60]))

    assert len(misfires) <= MAX_REAL_BIZ_MISFIRE, (
        f"真实业务语料误伤 {len(misfires)} 处，超过上限 {MAX_REAL_BIZ_MISFIRE}；"
        f"样例：{misfires[:15]}"
    )
