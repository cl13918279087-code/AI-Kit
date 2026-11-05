#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# llm_premask.py - 送 LLM 前的最小化预脱敏（命令行工具）
# ---------------------------------------------------------------------------
#
# 【用途】
#   Agent 直处理模式（SKILL.md 第五节-A）在把文档文本交给 LLM 之前，
#   先用本工具对文本做一次**等长掩码**：身份证 / 银行卡 / 手机 / 固话 / 邮箱
#   被替换为全角 X，LLM 因此看不到这些个人唯一标识的明文。
#
#   等长掩码保证 LLM 返回的字符偏移仍与原文一致，回写不会错位。
#
# 【用法】
#   # 从文件读，输出掩码后文本到 stdout
#   python3 scripts/llm_premask.py --file doc.txt
#
#   # 从 stdin 读
#   cat doc.txt | python3 scripts/llm_premask.py
#
#   # 输出到文件，并打印统计（不含任何原文）
#   python3 scripts/llm_premask.py --file doc.txt --out masked.txt --stats
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from entity_detector import pre_redact_for_llm  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="送 LLM 前的最小化预脱敏（等长掩码，不改变字符长度）"
    )
    parser.add_argument("--file", dest="file", help="输入文本文件；缺省则从 stdin 读")
    parser.add_argument("--out", dest="out", help="输出文件；缺省则写 stdout")
    parser.add_argument("--stats", action="store_true",
                        help="向 stderr 打印统计（仅类别与字符数，不含原文）")
    args = parser.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    masked, stats = pre_redact_for_llm(text)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(masked)
    else:
        sys.stdout.write(masked)

    if args.stats:
        print(json.dumps(stats, ensure_ascii=False), file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
