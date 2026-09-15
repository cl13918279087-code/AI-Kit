# -*- coding: utf-8 -*-
"""
R4 回归样例库（骨架版 · 2026-09-15）
沉淀自 Issue #2~#14 评审门禁的复现样例。运行：pytest tests/ -q
（无 pytest 环境时：python3 -c "import sys; sys.path.insert(0,'tests'); import test_r4_regression as t; t.test_true_names_fully_redacted(); t.test_over_redaction_fixed()"）
约定：每条样例标注来源 Issue；R4 各阶段合入前必须全绿。
注意：本文件随 R4 评审迭代扩充，成员可通过改进项清单/Issue 增补样例（脱敏后即可）。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from common_rules import apply_redactions  # noqa: E402

# ---------- 姓名：全遮回归（漏检红线） ----------
TRUE_NAMES = [
    ("陈贺生", "#13门禁"), ("谭菁岩", "#13门禁"), ("樊霖", "#13门禁"),
    ("方培培", "#13门禁"), ("孙海刚", "#e2e核实(v1.3.8)"),
]

# ---------- 误伤修复样例（过遮盖红线） ----------
FIX_CASES = [
    # (输入, 期望输出, 来源)
    ("李银行", "李银行", "#14 R-⑰"),
    ("王银行", "王银行", "#14 R-⑰"),
    ("关于郑州银行开展第一轮切换演练的通知", "关于XX银行开展第一轮切换演练的通知", "#14 R-⑱"),
    ("和郑州银行共同", "和XX银行共同", "#14 R-⑱"),
    ("向郑州银行报送", "向XX银行报送", "#14 R-⑱"),
    ("我行与郑州银行的合作", "我行与XX银行的合作", "#14 R-⑱"),
    ("郑州银行股份有限公司", "XX银行股份有限公司", "#14 R-⑱"),
    ("自郑州银行成立", "自XX银行成立", "#14 R-⑲"),
    ("和XX中心对接", "和XX中心对接", "#14波及面 R-⑱"),
    ("李XX学校", "李XX学校", "#14波及面 R-⑱"),
    ("黄XX镇", "XXX", "#恢复段不回归(黄日镇类)"),
    ("骨干成员", "骨干成员", "#RuleA二字词误伤"),
    ("时不我待", "时不我待", "#RuleA右边界放宽误伤"),
]


def test_true_names_fully_redacted():
    for name, src in TRUE_NAMES:
        assert apply_redactions(name) == "XXX", f"[{src}] {name} 未全遮（漏检）"


def test_over_redaction_fixed():
    for src, expect, origin in FIX_CASES:
        got = apply_redactions(src)
        assert got == expect, f"[{origin}] {src!r} -> {got!r}（期望 {expect!r}）"
