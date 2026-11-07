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
import re
import json
import time
import logging
import hashlib
import traceback
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
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
# 状态口径（Issue P0-1：产出方与消费方状态值不一致）
#
# 历史缺陷：process_single_file 产出终态 "complete"，而全部消费方
# （断点续传 mark_done / mark_failed、进度条统计、Markdown 报告统计）
# 一律判定 "success"。导致成功文件**既不计成功、也不算失败**——
# 批量报告输出「处理 1 / 成功 0 / 失败 0 / 跳过 0」，审计台账三项恒零；
# 断点续传 mark_done 永不触发，--resume 形同虚设。
#
# 口径决策：
#   1. 终态成功统一取 "complete"，与 redaction_engine.RedactionStatus.COMPLETE
#      以及本文件 RedactResult 的既有文档注释保持一致，不引入第二套词汇；
#   2. 消费方一律经 is_success_status / is_failed_status / is_skipped_status
#      判定，不再散落字符串字面量比较；
#   3. "success" 作为**历史别名**保留兼容——外部调用方自行构造
#      RedactResult(status="success") 时仍按成功处理，避免破坏既有集成。
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_DETECTING = "detecting"
STATUS_APPLYING = "applying"
STATUS_WRITING = "writing"
STATUS_COMPLETE = "complete"      # 终态：成功
STATUS_FAILED = "failed"          # 终态：失败
STATUS_SKIPPED = "skipped"        # 终态：跳过（输出已存在且未 --overwrite）

#: 终态成功的历史别名（兼容旧调用方构造的 status 值）
_SUCCESS_ALIASES = frozenset({STATUS_COMPLETE, "success"})

#: 全部终态
TERMINAL_STATUSES = frozenset({STATUS_COMPLETE, STATUS_FAILED, STATUS_SKIPPED, "success"})


def _status_value(status: Any) -> str:
    """把状态（字符串 / RedactionStatus 枚举 / 任意带 .value 的对象）统一为小写字符串。"""
    value = getattr(status, "value", status)
    return str(value).strip().lower()


def is_success_status(status: Any) -> bool:
    """是否成功终态（"complete"，兼容历史别名 "success"）。"""
    return _status_value(status) in _SUCCESS_ALIASES


def is_failed_status(status: Any) -> bool:
    """是否失败终态。"""
    return _status_value(status) == STATUS_FAILED


def is_skipped_status(status: Any) -> bool:
    """是否跳过终态。"""
    return _status_value(status) == STATUS_SKIPPED


# ---------------------------------------------------------------------------
# 任务结果
# ---------------------------------------------------------------------------

@dataclass
class RedactResult:
    input_path: str
    output_path: str
    status: str            # pending/detecting/applying/writing/complete/failed/skipped
    error: str = ""
    duration_ms: int = 0
    entities_count: int = 0
    file_size: int = 0
    manifest_path: str = ""
    # 分层阶段明细（从 redaction_engine.py 的 StageResult 复用）
    stages: List[Dict[str, Any]] = field(default_factory=list)   # [(stage, status, duration_ms, ...), ...]
    # 降级事件（架构评审 P0-3）：产物已生成，但保真度下降/内容可能失真
    degradations: List[Dict[str, Any]] = field(default_factory=list)

    # -- 终态判定（统一口径，勿再直接比较字符串字面量）--------------------

    @property
    def is_success(self) -> bool:
        return is_success_status(self.status)

    @property
    def is_failed(self) -> bool:
        return is_failed_status(self.status)

    @property
    def is_skipped(self) -> bool:
        return is_skipped_status(self.status)

    @property
    def is_degraded(self) -> bool:
        """本文件是否发生过降级（降级 ≠ 失败，但须人工复核）。"""
        return bool(self.degradations)


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

    分层状态流转：pending → detecting → applying → writing → complete/failed/skipped
    """
    input_path, output_dir, file_type, overwrite = args
    start = time.time()
    input_p = Path(input_path)
    stages: List[Dict[str, Any]] = []

    try:
        file_size = input_p.stat().st_size

        # 构建输出路径（文件名同样脱敏：银行名称/地址等 → XX，日期戳 → YYYYMMDD）
        # R-⑬（v1.3.7，Issue #13-①）：改用 common_rules 公共实现，与单文件 CLI 统一
        stem = input_p.stem
        ext = input_p.suffix.lower()
        try:
            from common_rules import redact_filename_stem as _redact_name
            stem = _redact_name(stem)
        except Exception:
            pass  # 文件名脱敏失败时保留原名，不影响正文脱敏
        output_path = Path(output_dir) / f"{stem}_脱敏{ext}"

        if output_path.exists() and not overwrite:
            return RedactResult(
                input_path=input_path,
                output_path=str(output_path),
                status=STATUS_SKIPPED,
                error="输出文件已存在（使用 --overwrite 覆盖）",
                duration_ms=int((time.time() - start) * 1000),
                file_size=file_size,
                stages=stages,
            )

        # Stage 1: DETECTING（文本提取 + 实体检测）
        stage_detect_start = time.time()
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
                status=STATUS_FAILED,
                error=f"不支持的文件类型: {file_type}",
                duration_ms=int((time.time() - start) * 1000),
                file_size=file_size,
                stages=stages,
            )

        entities_count = result.get("entities_count", 0)
        degradations = result.get("degradations", []) or []
        stages.append({
            "stage": "detecting",
            "status": STATUS_COMPLETE,
            "duration_ms": int((time.time() - stage_detect_start) * 1000),
            "entities_count": entities_count,
        })

        # Stage 2: APPLYING（脱敏应用 — 已由各 _process_* 函数内部完成）
        stages.append({
            "stage": "applying",
            "status": STATUS_COMPLETE,
            "duration_ms": 0,
            "entities_count": entities_count,
        })

        # Stage 3: WRITING（文件写出 + manifest 归档）
        stage_write_start = time.time()
        manifest_path = _write_manifest(
            str(output_path), counts=result.get("counts") or {},
            degradations=degradations, source_path=input_path)
        stages.append({
            "stage": "writing",
            "status": STATUS_COMPLETE,
            "duration_ms": int((time.time() - stage_write_start) * 1000),
        })

        return RedactResult(
            input_path=input_path,
            output_path=str(output_path),
            status=STATUS_COMPLETE,
            duration_ms=int((time.time() - start) * 1000),
            entities_count=entities_count,
            file_size=file_size,
            manifest_path=manifest_path,
            stages=stages,
            degradations=degradations,
        )

    except Exception as e:
        stages.append({
            "stage": "failed",
            "status": STATUS_FAILED,
            "error": f"{type(e).__name__}: {str(e)}",
            "duration_ms": int((time.time() - start) * 1000),
        })
        return RedactResult(
            input_path=input_path,
            output_path="",
            status=STATUS_FAILED,
            error=f"{type(e).__name__}: {str(e)}",
            duration_ms=int((time.time() - start) * 1000),
            file_size=input_p.stat().st_size if input_p.exists() else 0,
            stages=stages,
        )


def _write_manifest(output_path: str, counts: dict, degradations: list,
                    source_path: str = "") -> str:
    """写出脱敏 manifest（JSON + Markdown），返回 manifest.json 路径。

    【审计链 P0-4 补强】
      此前 `RedactResult.manifest_path` 只**声明**了一个路径，
      实际从不落盘——下游按该字段取审计留痕会拿到「文件不存在」。
      本函数让声明与事实一致：**要么真实落盘，要么返回空串**。

    与 `redact_all.write_redaction_manifest` 同口径：
      · 只记计数、类别、决策、LLM 审计状态与降级元数据；
      · **不含任何文档原文**。
    """
    try:
        from manifest import RedactionManifest
        import llm_egress
    except Exception:
        return ""

    try:
        out = Path(output_path)
        m = RedactionManifest(filename=out.name)
        stats = {k: v for k, v in (counts or {}).items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool)}
        m.set_stats(**stats)
        m.stats["mode"] = "pipeline"
        if source_path:
            m.stats["source_file"] = str(source_path)
        m.add_degradations(degradations or [])
        try:
            rt = llm_egress.get_runtime()
            m.llm_enabled = rt["enabled"]
            m.llm_chars_sent = rt["chars_sent"]
            m.llm_pre_redacted = rt["pre_redacted"]
            m.llm_masked_chars = rt["masked_chars"]
            m.llm_masked_spans = rt["masked_spans"]
            m.llm_egress_policy = rt["egress_policy"]
        except Exception:
            pass
        json_path = str(out) + "_manifest.json"
        m.save_json(json_path)
        m.save_markdown(str(out) + "_manifest.md")
        return json_path
    except Exception as e:
        print(f"[提示] manifest 归档失败（{e}）", file=sys.stderr)
        return ""


def _process_word(input_path: str, output_path: str) -> Dict[str, Any]:
    from redact_word import redact_word
    import degradation
    degradation.clear_degradations()
    manifest = redact_word(input_path, output_path)
    counts = {k: v for k, v in (manifest or {}).items() if isinstance(v, int)}
    return {"entities_count": sum(counts.values()),
            "counts": counts,
            "degradations": degradation.get_degradations()}


def _process_excel(input_path: str, output_path: str) -> Dict[str, Any]:
    from redact_excel import redact_excel
    import degradation
    degradation.clear_degradations()
    manifest = redact_excel(input_path, output_path)
    counts = {k: v for k, v in (manifest or {}).items() if isinstance(v, int)}
    return {"entities_count": sum(counts.values()),
            "counts": counts,
            "degradations": degradation.get_degradations()}


def _process_ppt(input_path: str, output_path: str) -> Dict[str, Any]:
    from redact_ppt import redact_ppt
    import degradation
    degradation.clear_degradations()
    manifest = redact_ppt(input_path, output_path)
    counts = {k: v for k, v in (manifest or {}).items() if isinstance(v, int)}
    return {"entities_count": sum(counts.values()),
            "counts": counts,
            "degradations": degradation.get_degradations()}


def _process_pdf(input_path: str, output_path: str) -> Dict[str, Any]:
    from redact_pdf import redact_pdf
    import degradation
    degradation.clear_degradations()
    manifest = redact_pdf(input_path, output_path)
    counts = {k: v for k, v in (manifest or {}).items() if isinstance(v, int)}
    return {"entities_count": sum(counts.values()),
            "counts": counts,
            "degradations": degradation.get_degradations()}


def _process_image(input_path: str, output_path: str) -> Dict[str, Any]:
    from redact_image import redact_image
    import degradation
    degradation.clear_degradations()
    manifest = redact_image(input_path, output_path)
    counts = {k: v for k, v in (manifest or {}).items() if isinstance(v, int)}
    return {"entities_count": sum(counts.values()),
            "counts": counts,
            "degradations": degradation.get_degradations()}


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

    终态口径
        返回的每条 RedactResult.status ∈ {complete, failed, skipped}，
        判定一律用 pipeline.is_success_status / is_failed_status / is_skipped_status，
        勿直接比较字符串字面量（"success" 仅作历史别名兼容）。

    台账完整性
        返回列表长度 == 输入文件数：类型不支持 / 不在 --types 范围 /
        断点续传已完成的文件也各占一条（分别为 failed / skipped），
        保证「处理文件数 == 成功 + 失败 + 跳过」恒等式成立。

    调用注意
        子进程以 ProcessPoolExecutor 启动（macOS/Windows 为 spawn，Linux 为 fork），
        **调用方须以 `if __name__ == "__main__":` 保护入口**，否则 spawn 重新导入
        主模块时会递归启动子进程。
    """
    # 输出目录兜底创建（Issue P0-2：CLI 层 main() 已建目录，但 API 层直接调用
    # batch_process 时不建，导致每个文件都 FileNotFoundError）
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"创建输出目录失败: {output_dir} — {e}")

    # 过滤文件类型
    tasks: List[Tuple[str, str, str, bool]] = []
    # 台账完整性：被过滤掉的文件（类型不支持 / 不在 --types 范围 / 断点续传已完成）
    # 也必须进结果列表，否则报告中「处理文件数」小于实际输入，审计对不上账
    results: List[RedactResult] = []
    for p in input_paths:
        ftype = detect_file_type(p)
        if ftype is None:
            logger.debug(f"跳过不支持的文件: {p}")
            results.append(RedactResult(
                input_path=p,
                output_path="",
                status=STATUS_FAILED,
                error=f"不支持的文件类型: {Path(p).suffix or '(无扩展名)'}",
            ))
            continue
        if file_types and ftype not in file_types:
            results.append(RedactResult(
                input_path=p,
                output_path="",
                status=STATUS_SKIPPED,
                error=f"不在指定类型范围内（--types {','.join(file_types)}），已跳过",
            ))
            continue
        if resume_state and resume_state.is_done(p):
            logger.info(f"[跳过-已完成] {Path(p).name}")
            results.append(RedactResult(
                input_path=p,
                output_path="",
                status=STATUS_SKIPPED,
                error="断点续传：此前已完成，跳过",
            ))
            continue
        tasks.append((p, output_dir, ftype, overwrite))

    if not tasks:
        logger.warning("没有需要处理的文件（全部为跳过或失败）")
        return results

    logger.info(f"共 {len(tasks)} 个文件待处理，并行度: {max_workers}")

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
                        status=STATUS_FAILED,
                        error=f"子进程异常: {str(e)}",
                    )

                results.append(result)

                if resume_state:
                    if is_success_status(result.status):
                        resume_state.mark_done(input_path, asdict(result))
                    elif is_failed_status(result.status):
                        resume_state.mark_failed(input_path, result.error)

                # 更新进度条描述
                done = sum(1 for r in results if is_success_status(r.status))
                fail = sum(1 for r in results if is_failed_status(r.status))
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
    success = [r for r in results if is_success_status(r.status)]
    failed = [r for r in results if is_failed_status(r.status)]
    skipped = [r for r in results if is_skipped_status(r.status)]
    degraded = [r for r in results if r.degradations]

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
        f"**成功**: {len(success)} | **失败**: {len(failed)} | **跳过**: {len(skipped)}"
        + (f" | **降级**: {len(degraded)}" if degraded else ""),
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
        f"| ⚠️ 降级 | {len(degraded)} |",
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

    # 【审计链 P0-3】降级产物清单：产物已生成但保真度下降/内容可能失真，
    # 必须显式列出，避免「静默失真」被当作正常产物归档。
    if degraded:
        lines.extend([
            f"",
            f"## ⚠️ 降级产物清单（需人工复核）",
            f"",
            f"共 {len(degraded)} 个文件在脱敏过程中发生降级。降级产物**已生成**，"
            f"但保真度下降或内容可能失真（如 `.doc` 经备用转换器转换后表格结构丢失），"
            f"须人工比对确认脱敏完整后再归档。",
            f"",
            f"| # | 文件名 | 级别 | 组件 | 细节 | 复核提示 |",
            f"|---|--------|------|------|------|---------|",
        ])
        row = 0
        for r in degraded:
            for d in r.degradations:
                row += 1
                lines.append(
                    f"| {row} | `{Path(r.input_path).name}` "
                    f"| {d.get('level', '')} | {d.get('component', '')} "
                    f"| {d.get('detail', '')} | {d.get('note', '')} |"
                )
        lines.append("")

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
    parser.add_argument("--resume-state", default=None,
                        help="断点续传状态文件路径（默认: <输出目录>/.redact_state.json）")
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

    # 创建输出目录
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 断点续传
    # 【修复】状态文件默认落**输出目录**而非当前工作目录——
    # 落在 CWD 会污染调用方工作目录（Agent 场景 CWD 往往是用户数据目录），
    # 且同一次运行的多次调用会互相覆盖。显式传 --resume-state 时尊重用户指定。
    resume_state = None
    if args.resume:
        state_path = args.resume_state or str(output_dir / ".redact_state.json")
        resume_state = ResumeState(state_path)
        summary = resume_state.get_summary()
        logger.info(f"断点续传状态: 已完成 {summary['done']}/{summary['total']}")

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
    success = [r for r in results if is_success_status(r.status)]
    failed = [r for r in results if is_failed_status(r.status)]
    skipped = [r for r in results if is_skipped_status(r.status)]
    degraded = [r for r in results if r.degradations]
    total_time = time.time() - start_time

    print("\n" + "=" * 60)
    print(f"  脱敏完成  —  ✅{len(success)}  ❌{len(failed)}  ⏭️{len(skipped)}"
          + (f"  ⚠️{len(degraded)}" if degraded else "")
          + f"  ⏱️{total_time:.1f}s")
    print(f"  报告: {report_path}")
    if degraded:
        print(f"  ⚠️ 存在 {len(degraded)} 个降级产物（保真度下降/内容可能失真）"
              f"，详见报告「降级产物清单」章节，须人工复核后再归档")
    print("=" * 60)

    # 【审计链 P0-3】退出码体现降级：降级产物以成功状态返回会让调度侧无从感知。
    #   0 = 全部成功且无降级
    #   1 = 存在失败文件
    #   2 = 无失败，但存在降级产物（须人工复核）
    if failed:
        sys.exit(1)
    if degraded:
        sys.exit(2)


if __name__ == "__main__":
    main()
