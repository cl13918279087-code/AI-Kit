# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# candidate_engine.py - R4-a 候选化引擎（major v2.0.0 · 分支评审参照实现）
#
# 对应评审方案：docs/R4姓名规则重构评审方案.md（分支 feature/r4-name-refactor）
#
# 架构转变：从"正则直接替换"转为"候选生成 → 逐层校验 → 确认替换"。
# R4-a 仅交付候选化骨架：所有替换以 RedactionPlan 记录为候选（原始文本坐标），
# 终文由 assemble() 从原文单点装配；L2 分词边界 / L3 上下文评分两层
# 在 R4-b/c 接入 candidates() 与 commit() 之间。
#
# 行为约束（R4-a 硬门禁）：与 v1.3.8 输出逐字节一致。
#   - tests/test_r4_regression.py：Issue #2~#14 样例 18 条
#   - 工作目录五份基线语料：逐文本单元 diff = 0
#
# 坐标模型：
#   - 工作文本每个字符持有原始坐标区间 [o_start, o_end)；
#   - 一次替换产出的所有字符共享同一原始区间（原子块）；
#   - 后续编辑覆盖原子块时（链式后处理，如 post_fixes/恢复段），其原始
#     坐标取所覆盖原子区间的并集；装配时"后记录编辑胜出"——与现行
#     链式顺序语义一致；
#   - assemble() 从原文 + 编辑清单重建，必须与工作文本逐字节一致，
#     作为候选记录完整性的结构自校验。
# ---------------------------------------------------------------------------

from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional


class RedactionPlan:
    """一份文本的脱敏计划：候选生成、确认与单点装配。"""

    def __init__(self, text: str):
        self.original: str = text
        self.current: str = text
        self.edits: List[Dict[str, Any]] = []
        self._seq: int = 0
        # 每个工作字符的原始区间（左右端点）；替换字符共享原子块区间
        self._o_start: List[int] = list(range(len(text)))
        self._o_end: List[int] = list(range(1, len(text) + 1))
        # 零宽插入编辑：原始位置 -> [编辑]（保序）
        self._inserts: Dict[int, List[Dict[str, Any]]] = {}

    # -- 候选确认 -----------------------------------------------------------

    def commit(self, start: int, end: int, replacement: str,
               stage: str, category: str) -> None:
        """在当前工作文本坐标 [start, end) 处确认一处替换。

        replacement 为空串表示删除；start == end 表示插入。
        """
        n = len(self.current)
        if not (0 <= start <= end <= n):
            return  # 非法坐标防御（正常流程不可达）
        if start == end and not replacement:
            return
        o_s, o_e = self._orig_span(start, end)
        edit = {
            "start": o_s, "end": o_e, "replacement": replacement,
            "stage": stage, "category": category, "seq": self._seq,
        }
        self._seq += 1
        if start == end:
            # 零宽插入：按原始坐标 o_s 入桶（装配时在该原文位置前输出）
            self._inserts.setdefault(o_s, []).append(edit)
        else:
            self.edits.append(edit)
        # 应用到工作文本并同步坐标表
        self.current = self.current[:start] + replacement + self.current[end:]
        if start == end:
            L = len(replacement)
            self._o_start = self._o_start[:start] + [o_s] * L + self._o_start[start:]
            self._o_end = self._o_end[:start] + [o_e] * L + self._o_end[start:]
        elif not replacement:
            self._o_start = self._o_start[:start] + self._o_start[end:]
            self._o_end = self._o_end[:start] + self._o_end[end:]
        else:
            L = len(replacement)
            self._o_start = self._o_start[:start] + [o_s] * L + self._o_start[end:]
            self._o_end = self._o_end[:start] + [o_e] * L + self._o_end[end:]

    def _orig_span(self, start: int, end: int) -> tuple:
        """工作坐标 → 原始坐标（所覆盖原子区间的并集）。"""
        if start < len(self.current):
            o_s = self._o_start[start]
        else:
            o_s = self._o_end[-1] if self.current else 0
        if end > start:
            o_e = self._o_end[end - 1]
        else:
            o_e = o_s
        return o_s, o_e

    def sync_stage(self, new_text: str, stage: str, category: str) -> None:
        """阶段函数整体返回新文本时的对接方式：diff 对齐记录净编辑。

        用于地址/电话/恢复段等内部含复杂扫描逻辑的通道——R4-a 不重写
        其内部实现（保持行为零变化），仅在其出入口记录编辑清单。
        """
        old = self.current
        if old == new_text:
            return
        sm = difflib.SequenceMatcher(None, old, new_text, autojunk=False)
        # 从右往左应用，避免坐标漂移
        for tag, i1, i2, j1, j2 in reversed(sm.get_opcodes()):
            if tag == "equal":
                continue
            self.commit(i1, i2, new_text[j1:j2], stage, category)

    # -- 输出 ----------------------------------------------------------------

    def candidates(self) -> List[Dict[str, Any]]:
        """候选清单（已确认替换，原始坐标）。L2/L3 校验层的接入点。"""
        all_edits = sorted(
            self.edits + [e for lst in self._inserts.values() for e in lst],
            key=lambda e: e["seq"],
        )
        return all_edits

    def assemble(self) -> str:
        """单点装配：从原文 + 确认编辑重建输出。

        结构自校验：结果必须与 self.current 逐字节一致（由坐标模型保证，
        门禁在基线语料上全量验证）。
        """
        cov: Dict[int, Dict[str, Any]] = {}
        for e in self.edits:
            for p in range(e["start"], e["end"]):
                cov[p] = e  # 后记录编辑胜出（链式语义）
        out: List[str] = []
        i, n = 0, len(self.original)
        while i < n:
            e = cov.get(i)
            if e is None:
                for ins in self._inserts.get(i, []):
                    out.append(ins["replacement"])
                out.append(self.original[i])
                i += 1
                continue
            j = i
            while j < n and cov.get(j) is e:
                j += 1
            out.append(e["replacement"])
            i = j
        # 原文末尾的插入编辑
        for ins in self._inserts.get(n, []):
            out.append(ins["replacement"])
        return "".join(out)
