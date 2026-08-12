#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# pipeline.py - 统一入口 / 批量并行处理 / 断点续传
# doc-redact-project / v1.0.0
#
# 用法：
#   python3 pipeline.py <输入文件/文件夹> [选项]
#   python3 pipeline.py ./docs -t all -o ./output --workers 4
#   python3 pipeline.py ./docs -t word,excel,pdf --resume
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import os
import json
import time
import logging
import hashlib
import traceback
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from tqdm import tqdm

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# 进度状态文件（断点续传）
# ---------------------------------------------------------------------------

class ResumeState:
    """断点续传状态管理"""

    def __init__(self, state_file: str):
        self.state_file = Path(state_file)
        self.state: Dict[str, Any] = {}
        if self.state_file.exists():
            try:
                self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                self.state = {}

    def is_done(self, file_path: str) -> bool:
        return self.state.get("done", {}).get(file_path, False)

    def mark_done(self, file_path: str, result: Dict) -> None:
        self.state.setdefault("done", {})[file_path] = True
        self.state.setdefault("results", {})[file_path] = result
        self._save()

    def mark_failed(self, file_path: str, error: str) -> None:
        self.state.setdefault("failed", {})[file_path] = error
        self._save()

    def get_summary(self) -> Dict[str, Any]:
        done = self.state.get("done", {})
        failed = self.state.get("failed", {})
        return {
            "total": len(done) + len(failed),
            "done": len(done),
            "failed": len(failed),
            "results": self.state.get("results", {}),
        }

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 任务结果
# ---------------------------------------------------------------------------

@dataclass
class RedactResult:
    input_path: str
    output_path: str
    status: str            # success / failed / skipped
    error: str = ""
    duration_ms: int = 0
    entities_count: int = 0
    file_size: int = 0
    manifest_path: str = ""


# ---------------------------------------------------------------------------
# 文件类型路由
# ---------------------------------------------------------------------------

FILE_TYPE_MAP = {
    ".docx": "word",
    ".doc": "word",
    ".xlsx": "excel",
    ".xls": "excel",
    ".pptx": "ppt",
    ".ppt": "ppt",
    ".pdf": "pdf",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".bmp": "image",
    ".gif": "image",
    ".tiff": "image",
    ".webp": "image",
}

SUPPORTED_TYPES = set(FILE_TYPE_MAP.keys())


def detect_file_type(path: str) -> Optional[str]:
    ext = Path(path).suffix.lower()
    return FILE_TYPE_MAP.get(ext)


# ---------------------------------------------------------------------------
# 单文件处理（子进程入口）
# ---------------------------------------------------------------------------

def process_single_file(args: Tuple[str, str, str, bool]) -> RedactResult:
    """
    处理单个文件（在子进程中运行）
    args: (input_path, output_dir, file_type, overwrite)
    """
    input_path, output_dir, file_type, overwrite = args
    start = time.time()
    input_p = Path(input_path)

    try:
        file_size = input_p.stat().st_size

        # 构建输出路径
        stem = input_p.stem
        ext = input_p.suffix.lower()
        output_path = Path(output_dir) / f"{stem}_脱敏{ext}"

        if output_path.exists() and not overwrite:
            return RedactResult(
                input_path=input_path,
                output_path=str(output_path),
                status="skipped",
                error="输出文件已存在（使用 --overwrite 覆盖）",
                duration_ms=int((time.time() - start) * 1000),
                file_size=file_size,
            )

        # 路由到对应处理器
        if file_type == "word":
            result = _process_word(input_path, str(output_path))
        elif file_type == "excel":
            result = _process_excel(input_path, str(output_path))
        elif file_type == "ppt":
            result = _process_ppt(input_path, str(output_path))
        elif file_type == "pdf":
            result = _process_pdf(input_path, str(output_path))
        elif file_type == "image":
            result = _process_image(input_path, str(output_path))
        else:
            return RedactResult(
                input_path=input_path,
                output_path=str(output_path),
                status="failed",
                error=f"不支持的文件类型: {file_type}",
                duration_ms=int((time.time() - start) * 1000),
                file_size=file_size,
            )

        manifest_path = str(output_path) + "_manifest.json"
        return RedactResult(
            input_path=input_path,
            output_path=str(output_path),
            status="success",
            duration_ms=int((time.time() - start) * 1000),
            entities_count=result.get("entities_count", 0),
            file_size=file_size,
            manifest_path=manifest_path,
        )

    except Exception as e:
        return RedactResult(
            input_path=input_path,
            output_path="",
            status="failed",
            error=f"{type(e).__name__}: {str(e)}",
            duration_ms=int((time.time() - start) * 1000),
            file_size=input_p.stat().st_size if input_p.exists() else 0,
        )


def _process_word(input_path: str, output_path: str) -> Dict[str, Any]:
    from scripts.redact_word import redact_word
    manifest = redact_word(input_path, output_path)
    return {"entities_count": sum(manifest.values())}


def _process_excel(input_path: str, output_path: str) -> Dict[str, Any]:
    from scripts.redact_excel import redact_excel
    manifest = redact_excel(input_path, output_path)
    return {"entities_count": sum(manifest.values())}


def _process_ppt(input_path: str, output_path: str) -> Dict[str, Any]:
    from scripts.redact_ppt import redact_ppt
    manifest = redact_ppt(input_path, output_path)
    return {"entities_count": sum(manifest.values())}


def _process_pdf(input_path: str, output_path: str) -> Dict[str, Any]:
    from scripts.redact_pdf import redact_pdf
    manifest = redact_pdf(input_path, output_path)
    return {"entities_count": sum(manifest.values())}


def _process_image(input_path: str, output_path: str) -> Dict[str, Any]:
    from scripts.redact_image import redact_image
    manifest = redact_image(input_path, output_path)
    return {"entities_count": sum(manifest.values())}


# ---------------------------------------------------------------------------
# 批量处理主函数
# ---------------------------------------------------------------------------

def batch_process(
    input_paths: List[str],
    output_dir: str,
    file_types: Optional[List[str]] = None,
    max_workers: int = 5,
    overwrite: bool = False,
    resume_state: Optional[ResumeState] = None,
) -> List[RedactResult]:
    """
    批量并行处理文件
    """
    # 过滤文件类型
    tasks: List[Tuple[str, str, str, bool]] = []
    for p in input_paths:
        ftype = detect_file_type(p)
        if ftype is None:
            logger.debug(f"跳过不支持的文件: {p}")
            continue
        if file_types and ftype not in file_types:
            continue
        if resume_state and resume_state.is_done(p):
            logger.info(f"[跳过-已完成] {Path(p).name}")
            continue
        tasks.append((p, output_dir, ftype, overwrite))

    if not tasks:
        logger.warning("没有找到需要处理的文件")
        return []

    logger.info(f"共 {len(tasks)} 个文件待处理，并行度: {max_workers}")
    results: List[RedactResult] = []

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_single_file, t): t[0] for t in tasks}

        with tqdm(total=len(tasks), desc="脱敏进度", unit="文件",
                  bar_format="{l_bar}{bar}| {n}/{total} [{elapsed}<{remaining}]") as pbar:
            for future in as_completed(futures):
                input_path = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = RedactResult(
                        input_path=input_path,
                        output_path="",
                        status="failed",
                        error=f"子进程异常: {str(e)}",
                    )

                results.append(result)

                if resume_state:
                    if result.status == "success":
                        resume_state.mark_done(input_path, asdict(result))
                    elif result.status == "failed":
                        resume_state.mark_failed(input_path, result.error)

                # 更新进度条描述
                done = sum(1 for r in results if r.status == "success")
                fail = sum(1 for r in results if r.status == "failed")
                pbar.set_postfix_str(f"✅{done} ❌{fail}", refresh=True)
                pbar.update(1)

    return results


# ---------------------------------------------------------------------------
# Markdown 报告生成
# ---------------------------------------------------------------------------

def generate_markdown_report(
    results: List[RedactResult],
    output_dir: str,
    total_input_chars: int = 0,
) -> str:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    success = [r for r in results if r.status == "success"]
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in results if r.status == "skipped"]

    total_duration = sum(r.duration_ms for r in results)
    total_entities = sum(r.entities_count for r in success)
    total_size = sum(r.file_size for r in results)

    # 按文件类型分组统计
    type_stats: Dict[str, Dict[str, int]] = {}
    for r in success:
        ftype = detect_file_type(r.input_path) or "unknown"
        if ftype not in type_stats:
            type_stats[ftype] = {"count": 0, "entities": 0, "size": 0}
        type_stats[ftype]["count"] += 1
        type_stats[ftype]["entities"] += r.entities_count
        type_stats[ftype]["size"] += r.file_size

    lines = [
        f"# 📋 文档脱敏批量处理报告",
        f"",
        f"**生成时间**: {timestamp}",
        f"**处理文件数**: {len(results)}",
        f"**成功**: {len(success)} | **失败**: {len(failed)} | **跳过**: {len(skipped)}",
        f"",
        f"---",
        f"",
        f"## 📊 总体统计",
        f"",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 处理文件数 | {len(results)} |",
        f"| ✅ 成功 | {len(success)} |",
        f"| ❌ 失败 | {len(failed)} |",
        f"| ⏭️ 跳过 | {len(skipped)} |",
        f"| 总耗时 | {total_duration/1000:.1f} 秒 |",
        f"| 检测敏感项总数 | {total_entities} |",
        f"| 输入文件总大小 | {_format_size(total_size)} |",
        f"",
        f"## 📁 按文件类型统计",
        f"",
        f"| 文件类型 | 处理数 | 敏感项 | 原始大小 |",
        f"|----------|--------|--------|---------|",
    ]

    for ftype, stats in sorted(type_stats.items()):
        lines.append(
            f"| {ftype.upper()} | {stats['count']} | "
            f"{stats['entities']} | {_format_size(stats['size'])} |"
        )

    if success:
        lines.extend([
            f"",
            f"## ✅ 成功详情",
            f"",
            f"| # | 文件名 | 敏感项 | 耗时 | 输出路径 |",
            f"|---|--------|--------|------|---------|",
        ])
        for i, r in enumerate(success, 1):
            lines.append(
                f"| {i} | `{Path(r.input_path).name}` | "
                f"{r.entities_count} | {r.duration_ms/1000:.1f}s | "
                f"`{Path(r.output_path).name}` |"
            )

    if failed:
        lines.extend([
            f"",
            f"## ❌ 失败详情",
            f"",
            f"| # | 文件名 | 错误原因 |",
            f"|---|--------|---------|",
        ])
        for i, r in enumerate(failed, 1):
            lines.append(
                f"| {i} | `{Path(r.input_path).name}` | "
                f"`{r.error[:60]}{'...' if len(r.error)>60 else ''}` |"
            )

    if skipped:
        lines.extend([
            f"",
            f"## ⏭️ 跳过详情",
            f"",
        ])
        for r in skipped:
            lines.append(f"- `{Path(r.input_path).name}` — {r.error}")

    lines.extend([
        f"",
        f"---",
        f"",
        f"*本报告由 doc-redact-project v1.0.0 自动生成*",
    ])

    report_path = Path(output_dir) / f"脱敏报告_{time.strftime('%Y%m%d_%H%M%S')}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"报告已生成: {report_path}")
    return str(report_path)


def _format_size(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="doc-redact-project - 金融文档智能脱敏工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 pipeline.py ./docs/report.docx -o ./output
  python3 pipeline.py ./docs -t all -o ./output --workers 4
  python3 pipeline.py ./docs -t word,excel,pdf --resume
  python3 pipeline.py ./docs -t image --overwrite
        """,
    )

    parser.add_argument("input", help="输入文件或文件夹路径")
    parser.add_argument("-o", "--output", default="./output", help="输出目录（默认: ./output）")
    parser.add_argument("-t", "--types",
                        help="文件类型，逗号分隔，如: word,excel,pdf,image,all（默认: all）")
    parser.add_argument("--workers", type=int, default=5, help="并行处理数量（默认: 5）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出文件")
    parser.add_argument("--resume", action="store_true", help="启用断点续传")
    parser.add_argument("--resume-state", default=".redact_state.json", help="断点续传状态文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示详细日志")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 解析文件类型
    file_types: Optional[List[str]] = None
    if args.types:
        if "all" in args.types:
            file_types = None
        else:
            file_types = [t.strip().lower() for t in args.types.split(",")]

    # 收集输入文件
    input_path = Path(args.input)
    input_files: List[str] = []

    if input_path.is_file():
        input_files = [str(input_path)]
    elif input_path.is_dir():
        for f in input_path.rglob("*"):
            if f.is_file() and f.suffix.lower() in SUPPORTED_TYPES:
                input_files.append(str(f))
    else:
        logger.error(f"输入路径不存在: {args.input}")
        sys.exit(1)

    logger.info(f"输入路径: {args.input}")
    logger.info(f"找到 {len(input_files)} 个文件")

    # 断点续传
    resume_state = None
    if args.resume:
        resume_state = ResumeState(args.resume_state)
        summary = resume_state.get_summary()
        logger.info(f"断点续传状态: 已完成 {summary['done']}/{summary['total']}")

    # 创建输出目录
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 执行批量处理
    start_time = time.time()
    results = batch_process(
        input_files,
        str(output_dir),
        file_types=file_types,
        max_workers=args.workers,
        overwrite=args.overwrite,
        resume_state=resume_state,
    )

    # 生成报告
    report_path = generate_markdown_report(results, str(output_dir))

    # 打印摘要
    success = [r for r in results if r.status == "success"]
    failed = [r for r in results if r.status == "failed"]
    total_time = time.time() - start_time

    print("\n" + "=" * 60)
    print(f"  脱敏完成  —  ✅{len(success)}  ❌{len(failed)}  ⏱️{total_time:.1f}s")
    print(f"  报告: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
