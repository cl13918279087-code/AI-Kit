#!/usr/bin/env python3
"""
redact_all.py - 文档脱敏统一入口
根据文件扩展名自动分发到对应处理器。

支持格式：
  .docx / .doc  → Word 文档
  .xlsx / .xls  → Excel 电子表格
  .pptx / .ppt  → PPT 演示文稿
  .pdf          → PDF 文档
  .png / .jpg / .jpeg / .bmp / .gif / .webp / .tiff → 图片

用法：
  python3 redact_all.py <输入文件> [输出文件]

  # 阶段一（检测 + 灰区评审）
  python3 redact_all.py input.docx --detect-only
  # Agent 读取 gray_review.json，评审后写入 decisions.json

  # 阶段二（应用 Agent 决策后执行）
  python3 redact_all.py input.docx --agent-decisions /path/to/decisions.json

  # 纯自动化（灰区自动遮盖，无人交互）
  python3 redact_all.py input.docx --auto-gray
"""

import sys
import os
import json
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from common_rules import (
    add_custom_replacement, REDACTION_LABELS,
    GRAY_CONFIDENCE_THRESHOLD, is_gray_entity, load_agent_decisions,
    classify_hit_channel, find_entity_span, CHANNEL_UNKNOWN
)
from workdir import (
    WORK_DIR_NAME, work_dir as _work_dir,
    write_secure_json as _write_secure_json,
    fingerprint as _fingerprint,
    cleanup_work_files,
)
import llm_egress
import degradation


# ---------------------------------------------------------------------------
# 受控工作目录（合规：敏感中间产物不再散落系统临时目录）
# ---------------------------------------------------------------------------
# 具体实现见 scripts/workdir.py：统一 <基准目录>/.redact_work/，
# 目录 0700 / 文件 0600，任务结束由 main() 的 finally 清理。


def _resolve_base_dir(input_path: str = None, output_path: str = None) -> str:
    """选择受控工作目录的基准目录：输出目录优先，其次输入目录。"""
    if output_path:
        return str(Path(output_path).resolve().parent)
    if input_path:
        return str(Path(input_path).resolve().parent)
    return tempfile.gettempdir()


# ---------------------------------------------------------------------------
# 运行时 LLM 状态（供 manifest 审计留痕）
# ---------------------------------------------------------------------------
# 具体状态存储见 llm_egress.record_runtime / get_runtime，
# 只记状态与计数，绝不含原文。


def record_llm_runtime(detector=None) -> None:
    """记录本次运行的 LLM 状态（供审计留痕）。

    detector 为 None 或其 llm 为 None（LLM 不可用）时，enabled 置 False。
    """
    llm = getattr(detector, "llm", None) if detector is not None else None
    if llm is None:
        llm_egress.record_runtime(enabled=False)
        return
    llm_egress.record_runtime(
        enabled=True,
        egress_policy=getattr(llm, "egress_reason", "") or "",
    )


def _find_span(text: str, snippet: str, start_hint=None, end_hint=None):
    """定位实体字符区间（Issue #54 D1 候选清单 span 字段）。

    优先采用实体自带偏移（LLM 层会给 start/end），否则回退到文本查找。
    """
    if start_hint is not None and end_hint is not None:
        return start_hint, end_hint
    return find_entity_span(text, snippet)


# ---------------------------------------------------------------------------
# 动态导入各格式处理器（延迟加载，加快启动速度）
# ---------------------------------------------------------------------------

def _load_handler(ext: str):
    handlers = {
        (".docx", ".doc"):  ("redact_word",   "redact_word"),
        (".xlsx", ".xls"):  ("redact_excel",  "redact_excel"),
        (".pptx", ".ppt"):  ("redact_ppt",    "redact_ppt"),
        (".pdf",):           ("redact_pdf",     "redact_pdf"),
        (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"):
                            ("redact_image",   "redact_image"),
    }
    for keys, (module, func) in handlers.items():
        if ext in keys:
            mod = __import__(module, fromlist=[func])
            return getattr(mod, func)
    return None


# ---------------------------------------------------------------------------
# 阶段一：灰区检测（不修改文件，输出评审 JSON）
# ---------------------------------------------------------------------------

def _extract_text_for_detection(file_path: str, ext: str) -> str:
    """提取纯文本用于实体检测"""
    p = Path(file_path)
    if ext in (".docx", ".xlsx", ".pptx"):
        import zipfile
        try:
            with zipfile.ZipFile(p) as z:
                if ext == ".docx":
                    members = [n for n in z.namelist() if n.endswith(".xml") and "word/" in n]
                elif ext == ".xlsx":
                    members = [n for n in z.namelist() if n.endswith(".xml")]
                else:
                    members = [n for n in z.namelist() if n.endswith(".xml")]
                texts = []
                for member in members[:20]:  # 限制最多 20 个 XML，避免超长
                    try:
                        content = z.read(member).decode("utf-8", errors="ignore")
                        # 简单去除 XML 标签
                        import re as _re
                        txt = _re.sub(r"<[^>]+>", " ", content)
                        txt = _re.sub(r"\s+", " ", txt).strip()
                        if txt:
                            texts.append(txt)
                    except Exception:
                        pass
                return " ".join(texts)[:50000]  # 限制 5 万字
        except Exception as e:
            return f"[文本提取失败: {e}]"
    elif ext == ".pdf":
        return f"[PDF 请用 --auto-gray 模式直接处理，不支持 --detect-only]"
    elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"):
        return f"[图片请用 --auto-gray 模式，不支持 --detect-only]"
    elif ext == ".doc":
        return f"[.doc 请先转换为 .docx 后再使用 --detect-only]"
    else:
        # 纯文本文件直接读
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                return f.read()[:50000]
        except Exception:
            return ""


def detect_gray_entities(input_path: str, output_json: str = None,
                         quiet: bool = False) -> dict:
    """
    阶段一：对输入文件执行实体检测，识别灰区实体并写入 JSON。

    返回：
        {
          "file": str,           # 输入文件路径
          "total_entities": int,  # 总实体数
          "green": [entity],     # 高置信，直接遮盖
          "gray": [entity],       # 灰区，交 Agent 评审
          "gray_json_path": str, # 灰区详情写入路径
        }
    """
    from entity_detector import build_llm_detector

    p = Path(input_path).resolve()
    ext = p.suffix.lower()

    if not p.exists():
        print(f"[错误] 文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    # 提取文本
    print(f"[检测] {p.name}")
    print(f"[格式] {ext}")
    text = _extract_text_for_detection(str(p), ext)
    if text.startswith("[") or not text:
        print(f"[错误] {text}", file=sys.stderr)
        sys.exit(1)

    print(f"[文本] 已提取 {len(text)} 字")

    # 构建检测器
    config_path = Path(__file__).parent / "config.json"
    detector = build_llm_detector(str(config_path) if config_path.exists() else None)

    # 执行检测
    manifest = detector.detect(text)
    manifest.filename = p.name

    # 【合规】记录 LLM 运行时状态（审计留痕，不含原文）
    record_llm_runtime(detector)
    llm_egress.record_runtime(
        enabled=bool(getattr(manifest, "llm_enabled", False)),
        chars_sent=int(getattr(manifest, "llm_chars_sent", 0)),
        pre_redacted=bool(getattr(manifest, "llm_pre_redacted", False)),
        masked_chars=int(getattr(manifest, "llm_masked_chars", 0)),
        masked_spans=int(getattr(manifest, "llm_masked_spans", 0)),
        egress_policy=getattr(manifest, "llm_egress_policy", "") or "",
    )

    green = []
    gray = []

    for e in manifest.entities:
        info = {
            "text": e.text,
            "replacement": e.replacement,
            "category": e.category,
            "confidence": e.confidence,
            "source": e.source,
            "evidence": e.evidence,
        }
        # Issue #54 D1：候选清单须带 span + 命中通道（否则裁决方无法定位责任规则）
        span_start, span_end = _find_span(text, e.text,
                                          getattr(e, "start", None),
                                          getattr(e, "end", None))
        info["span"] = {"start": span_start, "end": span_end}
        info["channel"] = classify_hit_channel(text, span_start, span_end,
                                               source=e.source) if span_start is not None \
            else CHANNEL_UNKNOWN
        if is_gray_entity(e.confidence):
            gray.append(info)
        else:
            green.append(info)

    print(f"[结果] 共检测 {len(green)} 个高置信实体，{len(gray)} 个灰区实体（需 Agent 评审）")
    print(f"[阈值] 灰区置信度 < {GRAY_CONFIDENCE_THRESHOLD}")

    # 生成灰区评审 JSON
    if output_json:
        gray_detail_path = output_json
    else:
        # 【合规】写入受控工作目录（0700/0600），不再散落系统临时目录
        gray_detail_path = str(_work_dir(_resolve_base_dir(input_path=str(p)))
                               / f"redact_gray_{os.getpid()}.json")

    _write_secure_json(gray_detail_path, {
        "file": str(p),
        "threshold": GRAY_CONFIDENCE_THRESHOLD,
        "total": len(green) + len(gray),
        "green_count": len(green),
        "gray_count": len(gray),
        "green": green,
        "gray": gray,
    })

    print(f"[灰区] 已写入: {gray_detail_path}")
    if not quiet:
        print(f"\n请将 gray_review.json 交给 Agent 评审，")
        print(f"评审后将决策写入 decisions.json，然后运行：")
        print(f"  python3 redact_all.py {input_path} --agent-decisions decisions.json\n")

    return {
        "file": str(p),
        "total_entities": len(green) + len(gray),
        "green": green,
        "gray": gray,
        "gray_json_path": gray_detail_path,
    }


# ---------------------------------------------------------------------------
# 阶段一（续）：--auto-gray 灰区自动裁决留痕
# ---------------------------------------------------------------------------

def export_auto_gray_decisions(input_path: str, output_path: str = None) -> Optional[str]:
    """`--auto-gray` 模式：导出灰区**自动裁决**留痕（Issue #21 / #54 D1）。

    语义：本次运行中所有灰区实体（置信度 < GRAY_CONFIDENCE_THRESHOLD）
    由规则引擎**自动裁决为遮盖**（不交 Agent 评审）。本函数把这份
    自动裁决结果落盘为 `decisions_pending.json`，用于合规留痕与后续
    「高频裁决沉淀为规则候选」分析。

    返回：decisions_pending.json 路径；无法检测时返回 None（不阻断脱敏）。
    """
    try:
        result = detect_gray_entities(input_path, quiet=True)
    except SystemExit:
        return None
    except Exception as e:
        print(f"[提示] 灰区留痕导出跳过（{e}）", file=sys.stderr)
        return None

    if output_path:
        base = Path(output_path)
        pending_path = str(base.with_name(base.stem + "_decisions_pending.json"))
    else:
        # 【合规】写入受控工作目录（0700/0600）
        pending_path = str(_work_dir(str(Path(input_path).resolve().parent))
                           / f"decisions_pending_{os.getpid()}.json")

    pending = []
    for e in result.get("gray", []):
        pending.append({
            "text": e.get("text", ""),
            # 【合规】不可逆指纹，供审计以非明文方式关联
            "fingerprint": _fingerprint(e.get("text", "")),
            "decision": "redact",          # 自动裁决：灰区一律遮盖
            "confidence": e.get("confidence"),
            "category": e.get("category"),
            "source": e.get("source"),
            "evidence": e.get("evidence"),
            # Issue #54 D1：候选清单须带 span + 命中通道
            "span": e.get("span"),
            "channel": e.get("channel"),
            "auto_adjudicated": True,       # 标记为「非 Agent 人工裁决」
        })

    try:
        _write_secure_json(pending_path, {
            "file": str(Path(input_path).resolve()),
            "threshold": GRAY_CONFIDENCE_THRESHOLD,
            "mode": "auto-gray",
            "gray_count": len(pending),
            "pending": pending,
        })
        print(f"[留痕] 灰区自动裁决已写入: {pending_path}")
        return pending_path
    except OSError as e:
        print(f"[提示] 灰区留痕写入失败（{e}）", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 阶段二：应用 Agent 决策（读取 decisions.json，跳过指定实体）
# ---------------------------------------------------------------------------

def apply_agent_decisions(input_path: str, decisions_path: str) -> None:
    """
    阶段二：根据 Agent 评审决策执行脱敏。

    decisions.json 格式：
      [{"text": "张明", "decision": "skip"},
       {"text": "黎明", "decision": "redact"}]
    """
    decisions = load_agent_decisions(decisions_path)
    if not decisions:
        print(f"[警告] 决策文件为空或解析失败，将正常执行脱敏", file=sys.stderr)
        return

    skip_set = {t for t, d in decisions.items() if d == "skip"}
    redact_set = {t for t, d in decisions.items() if d == "redact"}
    print(f"[决策] 跳过 {len(skip_set)} 个实体，追加遮盖 {len(redact_set)} 个实体")

    # 将追加遮盖实体写入受控工作目录的 override 文件，供 handler 读取
    # 【合规】写入受控目录（0700/0600），并在任务结束后清理（见 main 的 finally）
    base_dir = _resolve_base_dir(input_path=input_path)
    override_path = _work_dir(base_dir) / f"redact_override_{os.getpid()}.json"
    _write_secure_json(str(override_path),
                       {"skip": list(skip_set), "redact": list(redact_set)})

    return str(override_path)


# ---------------------------------------------------------------------------
# 裁决记录归档（Issue #54 D1）
# ---------------------------------------------------------------------------

def read_decision_records(path: str) -> list:
    """读取裁决文件（decisions.json / decisions_pending.json）为记录列表。

    两种格式均支持：
      · 列表格式：[{"text": ..., "decision": "skip|redact", ...}, ...]
      · 字典格式：{"pending": [...]}（自动裁决留痕文件）
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        for key in ("pending", "decisions", "items"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def write_redaction_manifest(output_path: str, counts: dict = None,
                             decisions: list = None,
                             source_path: str = "",
                             mode: str = "manual") -> str:
    """写 manifest.json（+ manifest.md），把裁决记录一并归档。

    Issue #54 D1 要点：「裁决记录随 manifest.json 归档，满足合规留痕」。
    产出路径：`<输出文件>_manifest.json` / `<输出文件>_manifest.md`
    （与 pipeline.py 的 manifest_path 口径一致）。

    返回：manifest.json 路径（失败返回空串）。
    """
    try:
        from manifest import RedactionManifest
    except Exception as e:      # pragma: no cover
        print(f"[提示] manifest 归档跳过（{e}）", file=sys.stderr)
        return ""
    try:
        out = Path(output_path)
        m = RedactionManifest(filename=out.name)
        stats = {k: v for k, v in (counts or {}).items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool)}
        m.set_stats(**stats)
        m.stats["decisions_count"] = len(decisions or [])
        m.stats["mode"] = mode
        m.stats["source_file"] = str(source_path)
        m.add_decisions(decisions or [])
        # 【合规】LLM 审计字段随 manifest 归档（只记状态与计数，不含原文）
        rt = llm_egress.get_runtime()
        m.llm_enabled = rt["enabled"]
        m.llm_chars_sent = rt["chars_sent"]
        m.llm_pre_redacted = rt["pre_redacted"]
        m.llm_masked_chars = rt["masked_chars"]
        m.llm_masked_spans = rt["masked_spans"]
        m.llm_egress_policy = rt["egress_policy"]
        # 【审计链 P0-3】降级事件随 manifest 归档（只有降级信息，无原文）
        m.add_degradations(degradation.get_degradations())
        json_path = str(out) + "_manifest.json"
        m.save_json(json_path)
        m.save_markdown(str(out) + "_manifest.md")
        print(f"[归档] 裁决记录已随 manifest 归档: {json_path}")
        return json_path
    except Exception as e:
        print(f"[提示] manifest 归档失败（{e}）", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# 批量脱敏（支持 glob 模式）
# ---------------------------------------------------------------------------

#: 本次运行中是否有文件**处理失败**（失败 ≠ 降级）。
#: 失败意味着**没有产物**，必须以退出码 1 呈现；
#: 降级意味着**有产物但保真度下降**，以退出码 2 呈现。
#: 两者同时发生时，失败优先（无产物比有次品更需要立即介入）。
_LAST_FILE_FAILED = False

#: 本次运行累计的全部降级事件（跨文件）。
#: 台账本身是「每文件清空」的进程内状态，批量场景下需在此累积，
#: 否则退出码只会反映最后一个文件的降级情况。
_ALL_DEGRADATIONS: list = []


def _resolve_output_path(input_path: str, output_path: str = None) -> str:
    """推导最终输出路径（与 redact_file 的默认命名口径一致）。"""
    if output_path:
        return str(Path(output_path).resolve())
    p = Path(input_path).resolve()
    ext = p.suffix.lower()
    suffix_map = {
        ".docx": "_脱敏.docx", ".doc": "_脱敏.doc",
        ".xlsx": "_脱敏.xlsx", ".xls": "_脱敏.xls",
        ".pptx": "_脱敏.pptx", ".ppt": "_脱敏.ppt",
        ".pdf": "_脱敏.pdf",
    }
    default_suffix = suffix_map.get(ext, "_脱敏" + ext)
    return str(p.with_name(f"{p.stem}{default_suffix}"))


def redact_file(input_path: str, output_path: str = None,
                method: str = "mosaic",
                manifest_override: str = None,
                auto_gray: bool = False) -> dict:
    """对单个文件执行脱敏，返回统计"""
    input_path = Path(input_path).resolve()
    ext = input_path.suffix.lower()

    output_path = Path(_resolve_output_path(input_path, output_path))

    handler = _load_handler(ext)
    if handler is None:
        print(f"[错误] 不支持的文件格式: {ext}", file=sys.stderr)
        return {}

    print(f"\n[处理] {input_path.name}")
    print(f"[格式] {ext}")
    if manifest_override:
        print(f"[决策] 加载 Agent 评审决策: {manifest_override}")

    # 【审计链 P0-3】每次处理前清空降级台账，避免跨文件串味；
    # 处理结束后立即取走本文件事件并并入累计列表（台账是每文件口径）。
    degradation.clear_degradations()

    try:
        if ext in (".pdf",):
            return handler(str(input_path), str(output_path),
                          manifest_override=manifest_override, auto_gray=auto_gray)
        elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"):
            return handler(str(input_path), str(output_path), method,
                          manifest_override=manifest_override, auto_gray=auto_gray)
        else:
            return handler(str(input_path), str(output_path),
                           manifest_override=manifest_override, auto_gray=auto_gray)
    except Exception as e:
        # 【审计链 P0-3】处理失败 = **无产物**，与「降级但有产物」是两种终态。
        # 此处显式置失败标志，使退出码取 1（失败）而非 2（降级），
        # 避免「失败被降级掩盖」——那正是 P0-3 要消除的静默失真。
        global _LAST_FILE_FAILED
        _LAST_FILE_FAILED = True
        print(f"[错误] 处理失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return {}
    finally:
        # 【审计链 P0-3】本文件的降级事件并入累计列表（含失败路径——
        # 失败路径同样可能伴随降级事件，如「转换全链失败」本身被登记为
        # manual_check 告警），保证批量场景下退出码不漏计。
        _ALL_DEGRADATIONS.extend(degradation.get_degradations())


def redact_batch(patterns: list, output_dir: str = None,
                 method: str = "mosaic") -> None:
    """对多个文件/glob 模式执行批量脱敏"""
    files = []
    for pattern in patterns:
        p = Path(pattern)
        if p.is_file():
            files.append(p)
        else:
            files.extend(p.glob(pattern) if "*" in pattern else [])

    files = sorted(set(f for f in files if f.is_file()))
    if not files:
        print("[错误] 未找到匹配的文件", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(output_dir) if output_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    total_counts = {}
    for f in files:
        out = out_dir / f.name if out_dir else None
        counts = redact_file(str(f), str(out) if out else None, method)
        for k, v in counts.items():
            total_counts[k] = total_counts.get(k, 0) + v

    print(f"\n{'='*50}")
    print(f"批量处理完成: {len(files)} 个文件")
    if total_counts:
        print("汇总统计:")
        for k, v in sorted(total_counts.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v} 处")
    # 【审计链 P0-3】批量路径同样按终态给出退出码（1=失败 / 2=降级）
    sys.exit(_exit_code_for_degradations())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _exit_code_for_degradations() -> int:
    """【审计链 P0-3】按终态决定退出码。

    0 = 全部成功且无降级
    1 = 存在**失败**文件（无产物，必须立即介入）
    2 = 无失败但存在降级产物（产物已生成，但保真度下降/内容可能失真，须人工复核）

    失败优先于降级：失败意味着「没有产物」，降级意味着「有次品」，
    二者不可混为一谈——否则失败会被降级掩盖，正是 P0-3 要消除的静默失真。
    """
    global _LAST_FILE_FAILED
    if _LAST_FILE_FAILED:
        print("[⚠️ 失败] 存在处理失败的文件（无产物），退出码 1")
        return 1
    # 累计口径：批量场景下台账是「每文件清空」的，退出码须反映全量降级
    events = list(_ALL_DEGRADATIONS) or degradation.get_degradations()
    if not events:
        return 0
    summary = degradation.summarize(events)
    print(f"\n[⚠️ 降级] 共 {summary['count']} 项降级事件"
          f"（最重级别：{summary['worst_level']}）：")
    for e in events:
        print(f"  - [{e.get('level')}] {e.get('component')}: "
              f"{e.get('detail')} {e.get('note')}")
    print("[⚠️ 降级] 产物已生成，须人工复核后归档（退出码 2）")
    return 2


def print_usage():
    print(__doc__)


def main():
    # 【审计链 P0-3】运行级状态初始化（进程内单次运行语义）
    global _LAST_FILE_FAILED, _ALL_DEGRADATIONS
    _LAST_FILE_FAILED = False
    _ALL_DEGRADATIONS = []

    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    method = "mosaic"
    files = []
    output = None
    detect_only = False
    agent_decisions = None
    auto_gray = False

    args = sys.argv[1:]
    while args:
        arg = args.pop(0)
        if arg == "--help" or arg == "-h":
            print_usage()
            sys.exit(0)
        elif arg.startswith("--method="):
            method = arg.split("=", 1)[1]
        elif arg == "--output" or arg == "-o":
            output = args.pop(0)
        elif arg == "--batch":
            files.extend(args)
            args = []
        elif arg == "--detect-only":
            detect_only = True
        elif arg == "--agent-decisions":
            agent_decisions = args.pop(0)
        elif arg == "--auto-gray":
            auto_gray = True
        elif not arg.startswith("--"):
            files.append(arg)

    if not files:
        print("[错误] 请指定输入文件", file=sys.stderr)
        sys.exit(1)

    # 阶段一：灰区检测
    if detect_only:
        if len(files) != 1:
            print("[错误] --detect-only 仅支持单文件", file=sys.stderr)
            sys.exit(1)
        detect_gray_entities(files[0], output)
        sys.exit(0)

    # 阶段二：应用 Agent 评审决策
    if agent_decisions:
        if len(files) != 1:
            print("[错误] --agent-decisions 仅支持单文件", file=sys.stderr)
            sys.exit(1)
        override_path = apply_agent_decisions(files[0], agent_decisions)
        try:
            # 正常执行，handlers 会读取 override_path
            counts = redact_file(files[0], output, method, manifest_override=override_path)
            # Issue #54 D1：裁决记录随 manifest.json 归档（合规留痕）
            write_redaction_manifest(
                _resolve_output_path(files[0], output),
                counts=counts,
                decisions=read_decision_records(agent_decisions),
                source_path=files[0],
                mode="agent-decisions",
            )
        finally:
            # 【合规】override 文件含实体原文，任务结束即清理（异常路径亦清理）
            cleanup_work_files(override_path)
        sys.exit(_exit_code_for_degradations())

    # 默认：纯自动化（灰区直接遮盖，不交 Agent）
    if len(files) == 1:
        pending_path = None
        if auto_gray:
            # Issue #21 / #54 D1：灰区自动裁决留痕（不阻断脱敏）
            pending_path = export_auto_gray_decisions(files[0], output)
        counts = redact_file(files[0], output, method, auto_gray=auto_gray)
        # Issue #54 D1：裁决记录随 manifest.json 归档（合规留痕）
        write_redaction_manifest(
            _resolve_output_path(files[0], output),
            counts=counts,
            decisions=read_decision_records(pending_path) if pending_path else [],
            source_path=files[0],
            mode="auto-gray" if auto_gray else "manual",
        )
        sys.exit(_exit_code_for_degradations())
    else:
        redact_batch(files, output, method)


if __name__ == "__main__":
    main()
