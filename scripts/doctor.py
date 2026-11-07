#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# doctor.py - 安装自检 / 环境体检（内网部署必做）
# ---------------------------------------------------------------------------
# 【为什么必须有】
#   本技能包将被下载至企业内网 Agent 运行。内网机器与开发机的差异是
#   **不可见的**：缺 Python 包、缺 OCR 二进制、配置指向已不存在的路径、
#   LLM 端点配置遗漏——这些问题在开发机全部正常，只有到了内网才暴露，
#   且暴露形式往往是「处理到一半才失败」，而不是「装载即失败」。
#
#   本脚本把这些不可见差异变成**装载后即可见的体检报告**。
#
# 【检查项】
#   A. Python 运行时版本
#   B. Python 依赖包（必装 / 可选，分级）
#   C. 外部二进制（tesseract / LibreOffice / antiword / textutil）
#   D. 技能包自身完整性（SKILL.md / 配置 / 词典 / references）
#   E. LLM 端点合规（复用 llm_egress 守卫口径，与运行时同源）
#   F. 开发者本机绝对路径残留（防止把开发机路径带进内网）
#
# 【退出码】
#   0 = 全部通过（可含可选项缺失的提示）
#   1 = 存在必装项缺失 / 关键检查失败（不可投产）
#   2 = 存在告警项（可运行，但需确认降级影响）
#   3 = 自检本身无法执行（参数或路径错误）
#
# 【用法】
#   python3 scripts/doctor.py                    # 人读报告
#   python3 scripts/doctor.py --json             # 机器可读（Agent 编排用）
#   python3 scripts/doctor.py --strict           # 告警也视为失败
#   python3 scripts/doctor.py --base-url <url>   # 追加校验指定 LLM 端点
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_INFO = "info"

_EXIT_BY_SEVERITY = {STATUS_OK: 0, STATUS_INFO: 0, STATUS_WARN: 2, STATUS_FAIL: 1}

MIN_PYTHON = (3, 9)

# --- 必装 Python 依赖：缺失则对应能力完全不可用 ---
REQUIRED_PACKAGES = [
    ("lxml", "Word/Excel/PPT XML 操作（全部格式共用）"),
    ("requests", "LLM 客户端（llm_client.py 顶层 import）"),
    ("tqdm", "批量处理进度条（pipeline.py 顶层 import）"),
]

# --- 可选 Python 依赖：缺失则对应能力降级或不可用，但不影响其他格式 ---
OPTIONAL_PACKAGES = [
    ("docx", "python-docx", ".doc 降级链 antiword 重建"),
    ("openpyxl", "openpyxl", ".xlsx 读写"),
    ("xlrd", "xlrd", ".xls 读取"),
    ("xlwt", "xlwt", ".xls 写入"),
    ("pptx", "python-pptx", "PPT 辅助解析"),
    ("PIL", "Pillow", "图片处理"),
    ("fitz", "pymupdf", "PDF 文本提取与页面渲染"),
    ("pytesseract", "pytesseract", "扫描件 OCR"),
    ("numpy", "numpy", "图像数组运算"),
    ("cv2", "opencv-python", "银行 Logo 启发式检测"),
    ("olefile", "olefile", ".doc/.xls/.ppt 二进制格式检测"),
    ("jieba", "jieba", "分词（缺失时挖掘脚本降级为滑窗统计）"),
]

# --- 外部二进制：不在 Python 包体系内，须由内网运维单独安装 ---
EXTERNAL_BINARIES = [
    ("tesseract", "OCR 引擎", "扫描版 PDF 与图片脱敏"),
    ("soffice", "LibreOffice", ".doc/.ppt 转 .docx/.pptx（可替代项见提示）"),
    ("antiword", "antiword", ".doc 纯文本兜底提取"),
]

# --- 技能包自身必须存在的文件（相对包根） ---
REQUIRED_FILES = [
    "SKILL.md",
    "requirements.txt",
    "scripts/config.json",
    "scripts/common_rules.py",
    "scripts/entity_detector.py",
    "scripts/pipeline.py",
    "references/redaction_rules.md",
    "references/redaction_spec.md",
]

# 开发者本机绝对路径特征（内网机器上必然不存在）
_DEV_PATH_PATTERNS = [
    re.compile(r"/Users/[A-Za-z0-9_.-]+/"),
    re.compile(r"/home/[A-Za-z0-9_.-]+/"),
    re.compile(r"C:\\\\Users\\\\"),
]


class Report:
    """体检结果收集器。"""

    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []

    def add(self, group: str, name: str, status: str, detail: str = "",
            hint: str = "") -> None:
        self.checks.append({
            "group": group,
            "name": name,
            "status": status,
            "detail": detail,
            "hint": hint,
        })

    def worst(self) -> str:
        order = [STATUS_OK, STATUS_INFO, STATUS_WARN, STATUS_FAIL]
        return max((c["status"] for c in self.checks), key=order.index) \
            if self.checks else STATUS_OK

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in (STATUS_OK, STATUS_INFO, STATUS_WARN, STATUS_FAIL)}
        for c in self.checks:
            out[c["status"]] += 1
        return out


# ---------------------------------------------------------------------------
# A. Python 运行时
# ---------------------------------------------------------------------------

def check_python(rep: Report) -> None:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PYTHON:
        rep.add("A. Python 运行时", "版本", STATUS_OK, ver,
                f"满足最低要求 >= {'.'.join(map(str, MIN_PYTHON))}")
    else:
        rep.add("A. Python 运行时", "版本", STATUS_FAIL, ver,
                f"内网机器须升级到 Python >= {'.'.join(map(str, MIN_PYTHON))}")
    impl = platform.python_implementation()
    rep.add("A. Python 运行时", "实现", STATUS_INFO, f"{impl} @ {platform.platform()}")


# ---------------------------------------------------------------------------
# B. Python 依赖包
# ---------------------------------------------------------------------------

def check_packages(rep: Report) -> None:
    for mod, desc in REQUIRED_PACKAGES:
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "") or _dist_version(mod)
            rep.add("B. Python 依赖", mod, STATUS_OK, ver or "已安装", desc)
        except Exception as exc:
            rep.add("B. Python 依赖", mod, STATUS_FAIL,
                    f"未安装（{type(exc).__name__}）",
                    f"必装项：pip install {mod} —— {desc}")

    for mod, pkg, desc in OPTIONAL_PACKAGES:
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "") or _dist_version(mod)
            rep.add("B. Python 依赖", pkg, STATUS_OK, ver or "已安装", desc)
        except Exception:
            rep.add("B. Python 依赖", pkg, STATUS_WARN, "未安装",
                    f"可选依赖：pip install {pkg} —— 缺失时 {desc} 不可用")


def _dist_version(mod: str) -> str:
    """取发行包版本（import 名与发行名不一致时用）。"""
    try:
        import importlib.metadata as md
        mapping = {
            "docx": "python-docx", "PIL": "Pillow", "fitz": "pymupdf",
            "cv2": "opencv-python",
        }
        return md.version(mapping.get(mod, mod))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# C. 外部二进制
# ---------------------------------------------------------------------------

def check_binaries(rep: Report) -> None:
    for exe, name, desc in EXTERNAL_BINARIES:
        path = _which(exe)
        if path:
            rep.add("C. 外部二进制", name, STATUS_OK, path, desc)
        else:
            hint = {
                "tesseract": "内网需离线安装 tesseract 及中文语言包（chi_sim）",
                "soffice": "内网需离线安装 LibreOffice；或依赖 antiword/textutil 兜底",
                "antiword": "内网需离线安装 antiword（.doc 兜底提取）",
            }.get(exe, "")
            rep.add("C. 外部二进制", name, STATUS_WARN, "未在 PATH 中找到", hint)

    # LibreOffice 实际路径（find_libreoffice 覆盖非 PATH 安装场景）
    try:
        from common_rules import find_libreoffice
        so = find_libreoffice()
        if so:
            rep.add("C. 外部二进制", "LibreOffice(实际路径)", STATUS_OK, so,
                    "find_libreoffice() 探测命中")
    except Exception:
        pass


def _which(exe: str) -> str:
    """定位可执行文件；Windows 下补查标准安装目录。"""
    p = shutil.which(exe)
    if p:
        return p
    extra = {
        "tesseract": [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"/opt/homebrew/bin/tesseract",
            "/usr/bin/tesseract",
        ],
        "soffice": [
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            "/usr/bin/soffice",
            "/usr/bin/libreoffice",
        ],
    }.get(exe, [])
    for c in extra:
        if os.path.exists(c):
            return c
    return ""


# ---------------------------------------------------------------------------
# D. 技能包完整性
# ---------------------------------------------------------------------------

def check_package_files(rep: Report) -> None:
    for rel in REQUIRED_FILES:
        p = PKG_ROOT / rel
        if p.exists():
            rep.add("D. 技能包完整性", rel, STATUS_OK, f"{p.stat().st_size} 字节")
        else:
            rep.add("D. 技能包完整性", rel, STATUS_FAIL, "缺失",
                    "分发包不完整，请重新获取")

    # 词典文件（通用人名词典层）
    dict_rel = "scripts/data/common_name_dict.txt"
    dp = PKG_ROOT / dict_rel
    if dp.exists():
        rep.add("D. 技能包完整性", dict_rel, STATUS_OK, f"{dp.stat().st_size} 字节",
                "通用人名词典层词表")
    else:
        rep.add("D. 技能包完整性", dict_rel, STATUS_WARN, "缺失",
                "缺失时通用人名词典层降级为空集，姓名召回下降")

    # 配置可解析性
    cfg_path = PKG_ROOT / "scripts" / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        rep.add("D. 技能包完整性", "config.json 解析", STATUS_OK,
                f"{len(cfg)} 个配置项")
        _check_config_shape(rep, cfg)
    except Exception as exc:
        rep.add("D. 技能包完整性", "config.json 解析", STATUS_FAIL,
                f"{type(exc).__name__}: {exc}", "配置损坏，无法运行")


def _check_config_shape(rep: Report, cfg: Dict[str, Any]) -> None:
    """配置关键项体检（LLM 段合规在 E 段单独处理）。"""
    required_keys = ["replacement", "surname_pool", "bank_names"]
    missing = [k for k in required_keys if not cfg.get(k)]
    if missing:
        rep.add("D. 技能包完整性", "config.json 关键项", STATUS_WARN,
                f"缺失：{', '.join(missing)}", "相关脱敏规则可能不生效")
    else:
        rep.add("D. 技能包完整性", "config.json 关键项", STATUS_OK,
                "关键项齐备")

    llm = cfg.get("llm") or {}
    if llm.get("enabled"):
        rep.add("D. 技能包完整性", "config.json LLM 开关", STATUS_INFO,
                "enabled=true（脚本侧 LLM 通道已开启）",
                "须确认 base_url 已通过出网守卫校验（见 E 段）")
    else:
        rep.add("D. 技能包完整性", "config.json LLM 开关", STATUS_OK,
                "enabled=false（默认走 Agent 侧 LLM 或纯规则）",
                "脚本侧 LLM 通道关闭，符合 Agent 技能包定位")


# ---------------------------------------------------------------------------
# E. LLM 端点合规
# ---------------------------------------------------------------------------

def check_endpoints(rep: Report, extra_urls: List[str]) -> None:
    try:
        from llm_egress import check_egress, describe_policy, is_private_host
        from endpoint_check import _load_llm_config
    except Exception as exc:
        rep.add("E. LLM 端点合规", "守卫模块加载", STATUS_FAIL,
                f"{type(exc).__name__}: {exc}", "出网守卫不可用，禁止投产")
        return

    cfg = _load_llm_config(str(PKG_ROOT / "scripts" / "config.json"))
    llm = (cfg.get("llm") or {}) if isinstance(cfg, dict) else {}
    allowed = list(llm.get("allowed_hosts") or [])
    require_private = bool(llm.get("require_private_network", True))

    urls: List[str] = list(extra_urls)
    base = str(llm.get("base_url") or "").strip()
    if base and base not in urls:
        urls.append(base)

    if not urls:
        rep.add("E. LLM 端点合规", "配置端点", STATUS_OK,
                "未配置脚本侧 LLM 端点",
                "符合 Agent 技能包定位（LLM 由 Agent 侧承载）")
        rep.add("E. LLM 端点合规", "守卫口径", STATUS_OK, describe_policy(
            allowed, require_private))
        return

    for u in urls:
        ok, reason = check_egress(u, allowed_hosts=allowed,
                                   require_private_network=require_private)
        if ok:
            host = re.sub(r"^\w+://", "", u).split("/")[0]
            rep.add("E. LLM 端点合规", f"端点 {host}", STATUS_OK, "合规", reason)
        else:
            host = re.sub(r"^\w+://", "", u).split("/")[0]
            rep.add("E. LLM 端点合规", f"端点 {host}", STATUS_FAIL, reason,
                    "不合规端点禁止使用；须改内网端点或补 allowed_hosts 白名单")


# ---------------------------------------------------------------------------
# F. 开发者本机路径残留
# ---------------------------------------------------------------------------

def check_dev_paths(rep: Report) -> None:
    hits: List[str] = []
    for py in sorted((PKG_ROOT / "scripts").glob("*.py")):
        try:
            text = py.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, line in enumerate(text, 1):
            if line.lstrip().startswith("#"):
                continue
            for pat in _DEV_PATH_PATTERNS:
                if pat.search(line):
                    hits.append(f"{py.name}:{i}")
                    break
    if hits:
        rep.add("F. 路径残留", "开发者绝对路径", STATUS_WARN,
                f"{len(hits)} 处：{', '.join(hits[:5])}",
                "内网机器上该路径不存在，相关脚本不可用；须改为环境变量")
    else:
        rep.add("F. 路径残留", "开发者绝对路径", STATUS_OK, "无残留")


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------

_ICON = {STATUS_OK: "✅", STATUS_INFO: "ℹ️ ", STATUS_WARN: "⚠️ ", STATUS_FAIL: "❌"}


def render_text(rep: Report) -> str:
    lines = []
    cur_group = None
    for c in rep.checks:
        if c["group"] != cur_group:
            cur_group = c["group"]
            lines.append(f"\n{cur_group}")
        detail = f" — {c['detail']}" if c.get("detail") else ""
        hint = f"\n      → {c['hint']}" if c.get("hint") else ""
        lines.append(f"  {_ICON[c['status']]} {c['name']}{detail}{hint}")

    cnt = rep.counts()
    lines.append("\n" + "─" * 60)
    lines.append(
        f"总计 {len(rep.checks)} 项：通过 {cnt[STATUS_OK]}，"
        f"提示 {cnt[STATUS_INFO]}，告警 {cnt[STATUS_WARN]}，失败 {cnt[STATUS_FAIL]}"
    )
    worst = rep.worst()
    verdict = {
        STATUS_OK: "✅ 自检通过，技能包可用于内网 Agent",
        STATUS_INFO: "✅ 自检通过",
        STATUS_WARN: "⚠️ 存在告警项：功能可运行，但相关能力降级，须确认影响面",
        STATUS_FAIL: "❌ 自检失败：存在必装项缺失或关键不合规，禁止投产",
    }[worst]
    lines.append(verdict)
    return "\n".join(lines)


def render_json(rep: Report) -> str:
    cnt = rep.counts()
    return json.dumps({
        "package_root": str(PKG_ROOT),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "worst_status": rep.worst(),
        "counts": cnt,
        "checks": rep.checks,
    }, ensure_ascii=False, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="doc-redact 安装自检（内网部署必做）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--strict", action="store_true", help="告警项也视为失败")
    ap.add_argument("--base-url", action="append", default=[],
                    help="额外校验的 LLM 端点（可重复）")
    args = ap.parse_args()

    rep = Report()
    check_python(rep)
    check_packages(rep)
    check_binaries(rep)
    check_package_files(rep)
    check_endpoints(rep, args.base_url)
    check_dev_paths(rep)

    if args.json:
        print(render_json(rep))
    else:
        print(render_text(rep))

    worst = rep.worst()
    code = _EXIT_BY_SEVERITY[worst]
    if args.strict and worst == STATUS_WARN:
        code = 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
