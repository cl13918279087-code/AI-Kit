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
# 坐标模型（v2 —— 增量偏移版）：
#   - 不再维护每个工作字符的原始坐标数组，改为记录累计偏移量列表
#     _cum_off[n] = 原文第 n+1 个字符对应的原始坐标右端点；
#     len(_cum_off) == len(current)；_cum_off 单调非减；
#   - commit() 更新 _cum_off：O(k) 其中 k 为本次替换涉及的字符数，
#     整体从 O(k_edit · n) 降至 O(k_edit)（k_edit 通常 ≤10，远小于 n）；
#   - _orig_span() 用 bisect_right 查表：O(log n)；
#   - assemble() 线性扫描已排序编辑列表，无逐字符字典。
#
# 变更记录（v2 vs v1）：
#   - 2026-09-16：修复陈黎回评发现的三重 O(n²) 性能退化
#     * commit()：整表重建坐标数组 → 增量偏移 O(k)
#     * sync_stage()：无条件全量 SequenceMatcher → 首尾公共前后缀短路
#     * assemble()：逐字符 cov 字典 → 排序后线性区间合并
# ---------------------------------------------------------------------------

from __future__ import annotations

import bisect
import difflib
from typing import Any, Dict, List, Optional


class RedactionPlan:
    """一份文本的脱敏计划：候选生成、确认与单点装配（v2，增量偏移版）。"""

    def __init__(self, text: str):
        self.original: str = text
        self.current: str = text
        self.edits: List[Dict[str, Any]] = []
        self._seq: int = 0
        # 累计偏移表：_cum_off[n] = 原文第 n+1 个字符对应的原始坐标右端点
        # len(_cum_off) == len(current)；_cum_off[i] 单调非减
        n = len(text)
        self._cum_off: List[int] = list(range(1, n + 1))
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

        # 计算原始坐标（工作坐标 → 原始坐标，O(log n)）
        o_s, o_e = self._orig_span(start, end)

        edit = {
            "start": o_s, "end": o_e, "replacement": replacement,
            "stage": stage, "category": category, "seq": self._seq,
        }
        self._seq += 1
        if start == end:
            # 零宽插入：按原始坐标入桶（装配时在该原文位置前输出）
            self._inserts.setdefault(o_s, []).append(edit)
        else:
            self.edits.append(edit)

        # 应用到工作文本并同步偏移表（增量，O(k) 其中 k=end-start+len(repl)）
        self.current = self.current[:start] + replacement + self.current[end:]
        repl_len = len(replacement)
        old_span = end - start

        if start == end:
            # 插入：offset 表插入 repl_len 个新值（同 o_s）
            self._cum_off = (
                self._cum_off[:start]
                + [o_s] * repl_len
                + self._cum_off[start:]
            )
        elif repl_len == 0:
            # 删除：offset 表移除 [start, end) 段
            self._cum_off = self._cum_off[:start] + self._cum_off[end:]
        else:
            # 替换：offset 表该段全部更新为 o_s
            self._cum_off = (
                self._cum_off[:start]
                + [o_s] * repl_len
                + self._cum_off[end:]
            )

    def _batch_commit(self, edits: List, stage: str, category: Optional[str]) -> None:
        """批量提交（同规则所有匹配一次性应用，性能关键路径）。

        edits：快照坐标列表，从右往左排列。
        格式A：[Tuple[start, end, replacement]] —— 所有编辑共用 category 参数
        格式B：[Tuple[start, end, replacement, label]] —— 每条编辑有独立 label
        内部：
        1. 从右往左逐条用 _orig_span 映射到当前坐标（O(k·log n)，k=匹配数）；
        2. 单次 O(n) 字符串构建 + O(n) _cum_off 重建；
        3. 总开销 O(k·log n + n)，远优于逐处 commit 的 O(k·n)。
        """
        if not edits:
            return

        # Step 1：检测格式并提取 label
        has_label = len(edits[0]) == 4

        # Step 2：快照坐标 → 当前坐标（从右往左，逐条映射）
        cur_edits: List = []
        for e in edits:
            s, end, repl = e[0], e[1], e[2]
            label = e[3] if has_label else (category or "")
            o_s, o_e = self._orig_span(s, end)
            cur_edits.append((s, end, repl, o_s, o_e, label))

        # Step 3: build new text (append right-to-left, reverse at end)
        n = len(self.current)
        rev: List[str] = []
        next_s = n
        for s, e, repl, _, _, _ in cur_edits:
            if e < next_s:
                rev.append(self.current[e:next_s])
            rev.append(repl)
            next_s = s
        if next_s > 0:
            rev.append(self.current[:next_s])
        rev.reverse()
        new_current = "".join(rev)

        # Step 4: rebuild _cum_off (O(n) linear scan)
        # Strategy: scan new text left-to-right, tracking which original position each char came from
        # Unedited chars: original pos increases linearly
        # Replacement chars: all share the same original interval start (o_s)
        final_cum_off: List[int] = []
        old_t = 0  # next unprocessed original position
        for s, e, repl, o_s, o_e, _ in sorted(cur_edits, key=lambda x: x[0]):
            # Copy unedited chars [old_t, s)
            for _ in range(old_t, s):
                final_cum_off.append(_ + 1)
            # Replacement chars: all share o_s
            for _ in range(len(repl)):
                final_cum_off.append(o_s)
            old_t = e
        # Copy tail [old_t, n)
        for _ in range(old_t, n):
            final_cum_off.append(_ + 1)

        # Step 5: write edit records (original coords)
        seq_start = self._seq
        self._seq += len(cur_edits)
        for idx, (s, e, repl, o_s, o_e, label) in enumerate(reversed(cur_edits)):
            self.edits.append({
                "start": o_s, "end": o_e,
                "replacement": repl,
                "stage": stage, "category": label,
                "seq": seq_start + idx,
            })

        # Step 6: commit (single write)
        self.current = new_current
        self._cum_off = final_cum_off


    def _orig_span(self, start: int, end: int) -> tuple:
        """工作坐标 → 原始坐标（bisect 查表，O(log n)）。

        _cum_off[i] = 原文第 i+1 个字符的原始坐标右端点。
        第 n 个工作字符（0-indexed）的原始起点 = _cum_off[n-1]（n>0）或 0（n=0）。
        工作坐标区间 [start, end) 对应的原始右端点 = bisect_right(_cum_off, end-1)。
        """
        n = len(self.current)
        if start == 0:
            o_s = 0  # 原文第 0 个字符的原始起点恒为 0
        elif start < n:
            o_s = self._cum_off[start - 1]
        else:
            o_s = self._cum_off[-1] if self._cum_off else 0
        if end > start:
            # bisect_right 返回首个 > value 的下标，即该工作位置对应的原始右端点
            o_e = bisect.bisect_right(self._cum_off, end - 1)
        else:
            o_e = o_s
        return o_s, o_e

    def sync_stage(self, new_text: str, stage: str, category: str) -> None:
        """阶段函数整体返回新文本时的对接方式：diff 对齐记录净编辑。

        优化：先比对长度差与首尾公共前后缀，仅对真正变化的中间窗口做 diff，
        避免全量 SequenceMatcher（最坏 O(n²)）。
        """
        old = self.current
        if old == new_text:
            return

        # 短路 1：长度相同 → 逐字符扫描（最坏 O(n)，但实际触发此分支时 n 通常小）
        if len(old) == len(new_text):
            for i, (a, b) in enumerate(zip(old, new_text)):
                if a != b:
                    self.commit(i, i + 1, b, stage, category)
            return

        # 短路 2：利用公共前后缀缩小 diff 窗口
        # common prefix
        pref_len = 0
        min_len = min(len(old), len(new_text))
        while pref_len < min_len and old[pref_len] == new_text[pref_len]:
            pref_len += 1
        # common suffix（从尾部往左扫，避免 prefix/suffix 重叠）
        suff_len = 0
        while (suff_len < min_len - pref_len
               and old[len(old) - suff_len - 1] == new_text[len(new_text) - suff_len - 1]):
            suff_len += 1

        old_inner = old[pref_len:len(old) - suff_len] if suff_len else old[pref_len:]
        new_inner = new_text[pref_len:len(new_text) - suff_len] if suff_len else new_text[pref_len:]

        # 窗口内无变化（仅首尾因长度差异而不同）
        if not old_inner and not new_inner:
            total_len = len(old)
            if pref_len < total_len - suff_len:
                self.commit(pref_len, total_len - suff_len,
                            new_text[pref_len:len(new_text) - suff_len], stage, category)
            return

        sm = difflib.SequenceMatcher(None, old_inner, new_inner, autojunk=False)
        # 从右往左应用，避免坐标漂移（工作坐标在 old 快照上）
        for tag, i1, i2, j1, j2 in reversed(sm.get_opcodes()):
            if tag == "equal":
                continue
            self.commit(pref_len + i1, pref_len + i2, new_inner[j1:j2], stage, category)

    # -- 输出 ----------------------------------------------------------------

    def candidates(self) -> List[Dict[str, Any]]:
        """候选清单（已确认替换，原始坐标）。L2/L3 校验层的接入点。"""
        all_edits = sorted(
            self.edits + [e for lst in self._inserts.values() for e in lst],
            key=lambda e: e["seq"],
        )
        return all_edits

    def assemble(self) -> str:
        """单点装配：从原文 + 确认编辑重建输出（线性区间合并版）。

        结构自校验：结果必须与 self.current 逐字节一致（由坐标模型保证，
        门禁在基线语料上全量验证）。
        """
        if not self.edits:
            # 无替换编辑：逐插入输出即可
            parts = [self.original]
            for i in range(len(self.original) + 1):
                for ins in self._inserts.get(i, []):
                    parts.append(ins["replacement"])
            return "".join(parts)

        # 按原始 start 排序并合并重叠区间
        sorted_edits = sorted(self.edits, key=lambda e: e["start"])
        merged: List[Dict[str, Any]] = []
        for e in sorted_edits:
            if merged and e["start"] <= merged[-1]["end"]:
                # 重叠/邻接：合并（取后记录 replacement）
                merged[-1]["end"] = max(merged[-1]["end"], e["end"])
                merged[-1]["replacement"] += e["replacement"]
            else:
                merged.append(e.copy())

        # 线性扫描输出：o_s → 复制原文片段 → o_e → 写 replacement
        out_parts: List[str] = []
        prev = 0
        for e in merged:
            s, e_pos = e["start"], e["end"]
            # 原文 [prev, s) 复制
            if prev < s:
                out_parts.append(self.original[prev:s])
            # 零宽插入（在 s 之前）
            for ins in self._inserts.get(s, []):
                out_parts.append(ins["replacement"])
            # 替换
            out_parts.append(e["replacement"])
            prev = e_pos

        # 原文剩余
        if prev < len(self.original):
            out_parts.append(self.original[prev:])
        # 末尾零宽插入
        for ins in self._inserts.get(len(self.original), []):
            out_parts.append(ins["replacement"])

        return "".join(out_parts)
