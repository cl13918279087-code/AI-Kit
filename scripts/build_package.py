#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# build_package.py - 生成分发给企业内网 Agent 的技能包
# ---------------------------------------------------------------------------
# 【为什么需要】
#   仓库 ≠ 可交付物。仓库里混着开发期资产（测试、CI、挖掘脚本、本机残留、
#   个人语料、评审文档），直接 zip 整个仓库交付到内网会带来：
#     · 无关代码扩大攻击面与审计范围；
#     · 开发期脚本残留本机路径与个人语料引用；
#     · 无版本标识与完整性校验，内网无法追溯包来源。
#
#   本脚本产出**最小可运行集**：只含 Agent 运行必需文件 + 安装元数据，
#   并输出 SHA256 与内容清单，供内网侧验签与追溯。
#
# 【产物】
#   dist/doc-redact-<version>.zip
#   dist/doc-redact-<version>.zip.sha256      # 完整性校验
#   dist/doc-redact-<version>.manifest.json  // 内容清单 + 版本 + 依赖声明
#
# 【用法】
#   python3 scripts/build_package.py
#   python3 scripts/build_package.py --outdir /tmp/out --strip-pii
#   python3 scripts/build_package.py --include-dev    # 连测试一起打包（内网自测用）
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

PKG_ROOT = Path(__file__).resolve().parent.parent
sys_version = "1.0.0"

# --- 分发包内必含文件（相对包根） ---
REQUIRED_INCLUDE = [
    "SKILL.md",
    "README.md",
    "LICENSE",
    "requirements.txt",
    "requirements-dev.txt",
    "scripts/config.json",
    "scripts/common_rules.py",
    "scripts/entity_detector.py",
    "scripts/degradation.py",
    "scripts/endpoint_check.py",
    "scripts/llm_client.py",
    "scripts/llm_egress.py",
    "scripts/llm_premask.py",
    "scripts/manifest.py",
    "scripts/pipeline.py",
    "scripts/redact_all.py",
    "scripts/redact_excel.py",
    "scripts/redact_image.py",
    "scripts/redact_pdf.py",
    "scripts/redact_ppt.py",
    "scripts/redact_word.py",
    "scripts/workdir.py",
    "scripts/doctor.py",
    "scripts/data/common_name_dict.txt",
    "scripts/data/full_name_list.README.txt",
    "scripts/data/extra_name_list.README.txt",
    "scripts/data/bank_names.txt",
    "references/redaction_rules.md",
    "references/redaction_spec.md",
]

# --- 可选附带（目录整体纳入） ---
OPTIONAL_DIRS = [
    "scripts/data",
    "references",
]

# --- 开发期资产：默认剥离 ---
DEV_EXCLUDE = [
    "tests",
    "docs",
    ".github",
    ".git",
    "output",
    "工作目录",
    "共建项目",
    ".pytest_cache",
    ".workbuddy",
    "dist",
]

# --- 开发期脚本：默认剥离（挖掘/迭代对比类，非运行时必需） ---
# 【重要】名单必须与运行时 import 闭包一致。
#   曾经的错误：table_role_detector / name_region_detector / quality_validator /
#   bench_metrics / r4_name_scoring 被列为"开发期脚本"剥离，但它们被
#   redact_excel / redact_ppt / redact_word / common_rules / entity_detector
#   直接 import —— 内网装包后 Excel/PPT 脱敏会 ImportError。
#   剥离清单改由 _RUNTIME_MODULES 求闭包自动推导，只剥离真正无引用的脚本。
DEV_SCRIPT_EXCLUDE = [
    "mine_boundary_pairs.py",
    "mine_guard_words.py",
    "decision_mining.py",
    "redact_iterative_compare.py",
    "redact_iterative_v2.py",
    "keep_audit.py",
    "residual_audit.py",
    "test_common_rules.py",
    "redact_llm.py",
]

# --- 纯评测/开发资产：虽被函数内 import，但引用点只在 CLI 评测入口，
#     脱敏主链路永不触发 —— 默认剥离，不进分发包 ---
DEV_EVAL_ASSETS = [
    "bench_metrics.py",
]

# --- 运行时入口模块：用于推导 import 闭包 ---
RUNTIME_ENTRY_MODULES = [
    "redact_all",
    "redact_word",
    "redact_excel",
    "redact_ppt",
    "redact_pdf",
    "redact_image",
    "pipeline",
    "entity_detector",
    "common_rules",
    "degradation",
    "endpoint_check",
    "llm_client",
    "llm_egress",
    "llm_premask",
    "manifest",
    "workdir",
    "doctor",
]

# 全部剥离的顶层文件
TOP_EXCLUDE = [
    ".DS_Store",
    ".gitignore",
    ".pytest_cache",
]

# --- 需要在包内生成的说明文件 ---
README_DIST = "安装与部署说明（分发包内）"

# ---------------------------------------------------------------------------
# 源码文本脱敏改写（内网分发用）
# ---------------------------------------------------------------------------
# 【为什么需要】
#   仓库的注释与文档字符串里引用了大量真实案例的客户机构名与自然人姓名
#   （如「郑州银行」「平安信用卡A+项目」「王根利」）。这些是**开发期的
#   案例引用**，不是运行逻辑，但随包分发等于把客户名单和自然人姓名
#   带进了内网 —— 对银行内网而言属于不必要的信息暴露。
#
# 【处理方式】
#   只改写注释/文档字符串里的**案例引用**，不改写运行逻辑：
#     · 机构名 -> 通用占位（XX银行 / XX信用卡项目 等）
#     · 案例人名 -> 通用占位（示例人名甲 等）
#   由于这些词只出现在注释与 docstring 里，改写不影响任何运行行为。
#   判定依据：scripts/_pii_audit.py 的 AST 分类结果——运行时代码中的
#   可执行字符串不含这些词（开发期脚本除外，那部分已整体剥离）。
#
# 【不做的事】
#   不改写 scripts/data/common_name_dict.txt —— 那是 Apache-2.0 公共语料，
#   与客户无关；里面与客户文档同名的条目属巧合，不是 PII。
#
TEXT_REDACTIONS: List[Tuple[str, str]] = [
    # --- 客户机构名 ---
    ("福建海峡银行", "XX银行"),
    ("郑州银行", "XX银行"),
    ("海峡银行", "XX银行"),
    ("中原银行", "XX银行"),
    ("平安信用卡A+项目", "XX信用卡项目"),
    ("平安信用卡", "XX信用卡"),
    ("尖刀组UAT3", "XX组UAT3"),
    ("尖刀组", "XX项目组"),
    ("尖刀测试", "XX测试"),
]

# --- 静态案例人名表：散落在源码/文档中、但**不在当前配置名单**的历史案例人名 ---
# 【与动态推导的分工】
#   动态推导：覆盖 config 的 full_name_list / extra_name_list（名单一改就跟随）
#   静态表：覆盖**已从名单中移除、但仍留在注释与文档里**的历史案例人名
#   两者取并集，缺一不可。
CASE_NAME_STATIC: List[Tuple[str, str]] = [
    ("王根利", "示例人名甲"),
    ("樊霖", "示例人名乙"),
    ("蔡新发", "示例人名丙"),
    ("孙海刚", "示例人名丁"),
    ("毛月珍", "示例人名戊"),
]

# --- 案例自然人姓名：静态表 + **从配置动态推导** ---
# 【为什么要动态推导】
#   客户真实人名散落在两处配置名单里：
#     · full_name_list  （7399 条，字频自举种子）
#     · extra_name_list （Issue #60 D-5 人工确认真名，20 条）
#   手工维护改写表必然随名单更新而漏项——本次合并远端批次二整改时，
#   extra_name_list 新增的人名就整体漏出了包。
#   改为直接从配置推导：名单里有的名字一律改写，名单一改，
#   改写表自动跟随，不会再漏。
#   判定依据：这些名字只出现在注释与 docstring 中（AST 分类确认），
#   改写不影响任何运行行为；运行时代码字符串不含这些词。
_CASE_NAME_PLACEHOLDER = "示例人名"


def _case_name_redactions(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    """推导「客户人名 -> 通用占位」改写规则 = 动态名单推导 ∪ 静态案例表。

    动态部分：从配置的两份客户名单推导（名单一改就自动跟随，不漏项）；
    静态部分：覆盖已从名单移除、但仍留在注释与文档里的历史案例人名。
    """
    names: List[str] = []
    seen = set()
    for key in ("full_name_list", "extra_name_list"):
        for n in (cfg.get(key) or []):
            if isinstance(n, str) and n and n not in seen:
                seen.add(n)
                names.append(n)
    # 长名优先，避免「王谧」先于「王谧然」被替换造成半改写残留
    names.sort(key=len, reverse=True)
    dynamic = [(n, _CASE_NAME_PLACEHOLDER) for n in names]

    static: List[Tuple[str, str]] = []
    for src, dst in CASE_NAME_STATIC:
        if src not in seen:
            static.append((src, dst))
    return dynamic + static


def sanitize_text(data: bytes, extra_rules: Optional[List[Tuple[str, str]]] = None,
                 apply_case_names: bool = True) -> bytes:
    """改写文本中的客户案例引用（机构名 / 案例人名）。

    extra_rules：由配置动态推导出的客户人名改写规则（可为空）。
    apply_case_names：**是否应用案例人名规则**。

    【为什么要区分】
    案例人名只出现在**源码注释与文档示例**里，属于"引用"，改写安全。
    但名单类**数据文件**（config.json 的 full_name_list、
    data/common_name_dict.txt 的公共语料）里的同名字条是**数据本身**：
      - 改写 config.json 的 full_name_list → 包内精确匹配名单归零，
        召回崩塌（实测「李强」不再脱敏）；
      - 改写公共语料 → 破坏 Apache-2.0 上游数据集的内容完整性。
    因此：机构名规则对全部文本生效；案例人名规则**仅对源码/文档生效**。
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    changed = False
    rules = list(TEXT_REDACTIONS)
    if apply_case_names:
        rules += list(extra_rules or [])
    for src, dst in rules:
        if src in text:
            text = text.replace(src, dst)
            changed = True
    return text.encode("utf-8") if changed else data


def read_version() -> str:
    """从 SKILL.md frontmatter 读 version，缺省回退到常量。"""
    skill = PKG_ROOT / "SKILL.md"
    if skill.exists():
        for line in skill.read_text(encoding="utf-8").splitlines()[:20]:
            if line.strip().startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    return sys_version


def _runtime_closure() -> set:
    """用 AST 解析运行时入口模块的本地 import，求出完整依赖闭包。

    为什么需要：手工维护"运行时必需文件"清单必然漏项——历史上
    table_role_detector / name_region_detector 就因被误列为"开发期脚本"
    而剥离，导致内网装包后 Excel/PPT 脱敏直接 ImportError。
    改为从真实 import 关系推导，剥离清单自然正确。

    【顶层 import 与函数内 import 必须区别对待】
    - **顶层 import**（模块级）：模块一被 import 就要求依赖存在，
      缺失即 ImportError —— 属于**硬依赖**，必须在包内。
      例：redact_excel.py:22 `from table_role_detector import ...`
    - **函数内 import**（延迟 import）：只在被调用时才 import，
      缺失只影响该功能分支，不影响模块加载 —— 属于**软依赖**。
      例：quality_validator.py:350 `import bench_metrics`（仅评测用）

    因此：硬依赖一律纳入包；软依赖按需纳入——但**评测/开发资产**
    （bench_metrics 等）仍应剥离，它们由 `--include-dev` 控制，
    不属于运行时能力。判定依据是"是否被运行时入口真正加载"，
    而非"是否在 import 语句里出现过"。
    """
    import ast

    local = {p.stem for p in (PKG_ROOT / "scripts").glob("*.py")}
    closure: set = set()
    stack = list(RUNTIME_ENTRY_MODULES)

    def _target(node) -> str:
        """从 Import / ImportFrom 节点取出本地模块名。"""
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in local:
                    return top
            return ""
        # ImportFrom：相对导入（level>0）与标准库都跳过，只关心同目录本地模块
        if node.module and node.level == 0:
            top = node.module.split(".")[0]
            if top in local:
                return top
        return ""

    def deps_of(mod: str, top_level_only: bool) -> set:
        """求 mod 的本地 import 依赖。

        top_level_only=True 时只取**模块级** import（硬依赖）；
        False 时取全部 import（含函数内延迟 import，软依赖）。
        """
        f = PKG_ROOT / "scripts" / f"{mod}.py"
        if not f.exists():
            return set()
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            return set()
        found: set = set()
        if top_level_only:
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    t = _target(node)
                    if t:
                        found.add(t)
        else:
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    t = _target(node)
                    if t:
                        found.add(t)
        return found

    # --- 硬依赖闭包：模块级 import 递归 ---
    stack = list(RUNTIME_ENTRY_MODULES)
    while stack:
        mod = stack.pop()
        if mod in closure:
            continue
        closure.add(mod)
        for d in deps_of(mod, top_level_only=True):
            if d not in closure:
                stack.append(d)

    # --- 软依赖闭包：函数内延迟 import 递归（仅在被运行时真正用到时纳入）---
    # 判据：软依赖模块若被**硬依赖模块**在函数内引用，
    #       说明该功能在运行时会走到，需要随包提供；
    #       否则视为评测/开发资产，随 --include-dev 控制。
    soft: set = set()
    stack = list(RUNTIME_ENTRY_MODULES)
    while stack:
        mod = stack.pop()
        if mod in soft:
            continue
        soft.add(mod)
        for d in deps_of(mod, top_level_only=False):
            if d not in soft:
                stack.append(d)

    # 软依赖中，只有"被硬依赖模块引用"的才纳入运行时闭包。
    hard_reachable_soft: set = set()
    stack = list(closure)
    while stack:
        mod = stack.pop()
        for d in deps_of(mod, top_level_only=False):
            if d in closure:
                continue
            if d not in hard_reachable_soft:
                hard_reachable_soft.add(d)
                stack.append(d)

    runtime = closure | (hard_reachable_soft & soft)

    # --- 剥离纯评测/开发资产 ---
    # 这些模块虽被**函数内**引用，但引用点只在 CLI 评测入口上，
    # 脱敏主链路永不触发，属开发期资产（上游 DEV_SCRIPT_EXCLUDE 的原意）。
    # 例：bench_metrics 只被 quality_validator.__main__ 的 --json 分支引用。
    # 若未来有人用**顶层 import** 引入它们，则重新纳入（硬依赖优先）。
    hard_only = set()
    stack = list(RUNTIME_ENTRY_MODULES)
    while stack:
        mod = stack.pop()
        if mod in hard_only:
            continue
        hard_only.add(mod)
        for d in deps_of(mod, top_level_only=True):
            if d not in hard_only:
                stack.append(d)

    for asset in DEV_EVAL_ASSETS:
        stem = asset[:-3] if asset.endswith(".py") else asset
        if stem not in hard_only:
            runtime.discard(stem)

    return runtime


def _as_posix(rel: str) -> str:
    """统一包内路径分隔符为 '/'。

    为什么需要：Windows 上 str(Path.relative_to()) 产出反斜杠路径，
    写进 zip 后在 Linux/macOS 解压会得到名为 'references\\xxx.md' 的
    怪文件，且 manifest 里同一文件出现两次（斜杠版 + 反斜杠版）。
    """
    return rel.replace("\\", "/")


def collect_files(include_dev: bool) -> List[Tuple[Path, str]]:
    """收集待打包文件，返回 (源路径, 包内相对路径) 列表。"""
    picked: Dict[str, Path] = {}

    for rel in REQUIRED_INCLUDE:
        p = PKG_ROOT / rel
        if p.exists():
            picked[_as_posix(rel)] = p

    # 运行时 import 闭包：闭包内模块一律纳入（缺一个就是运行期 ImportError）
    closure = _runtime_closure()
    for mod in sorted(closure):
        rel = f"scripts/{mod}.py"
        p = PKG_ROOT / rel
        if p.exists():
            picked.setdefault(rel, p)

    for d in OPTIONAL_DIRS:
        dd = PKG_ROOT / d
        if not dd.is_dir():
            continue
        for p in sorted(dd.rglob("*")):
            if p.is_file():
                rel = _as_posix(str(p.relative_to(PKG_ROOT)))
                picked.setdefault(rel, p)

    if include_dev:
        for d in ("tests", "docs", ".github"):
            dd = PKG_ROOT / d
            if not dd.is_dir():
                continue
            for p in sorted(dd.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    picked.setdefault(_as_posix(str(p.relative_to(PKG_ROOT))), p)

    # 剥离开发期脚本（仅限不在运行时闭包内的）
    if not include_dev:
        for rel in list(picked):
            if rel.startswith("scripts/") and Path(rel).name in DEV_SCRIPT_EXCLUDE:
                if Path(rel).stem not in closure:
                    picked.pop(rel)

    # 剥离顶层杂项
    for rel in list(picked):
        if Path(rel).name in TOP_EXCLUDE or rel.startswith("."):
            picked.pop(rel)

    return sorted(((p, rel) for rel, p in picked.items()), key=lambda x: x[1])


def strip_pii_from_config(cfg: Dict[str, Any],
                          roster_path: Optional[str] = None) -> Dict[str, Any]:
    """分发包脱敏：移除内联真实人名库（保留字频表以维持闸门能力）。

    【为什么要改】
    原实现直接把 `full_name_list` 置空。但该列表有**双重身份**：
      (1) D16 精确匹配名单 —— 7399 条真实自然人姓名，属 PII，必须剥离；
      (2) 名字位字频自举种子 —— 从中派生的**聚合统计量**，非 PII。

    原实现只处理了 (1)，导致 (2) 一起丢失：闸门失效，
    热字集从 262 退回全池 1283，**姓名召回显著下降**，
    交付了一个"看着干净但能力残缺"的包。

    【本实现】
    剥离 (1)，同时把 (2) 导出到独立配置键 `name_char_freq_map`：
      - 字频表是「单字 -> 出现次数」，不含姓名组合；
      - 名字位字池 1278 字，2 字组合空间约 163 万，
        单字频次无法反推具体人名 —— 不构成 PII；
      - `common_rules._get_name_char_freq()` 已支持优先读该键，
        剥离后闸门行为与原始完全一致，召回不掉。

    精确匹配层则由内网部署方用**本行员工花名册**注入
    （`full_name_list_file` 配置项或 `AIKIT_FULL_NAME_LIST` 环境变量），
    这也正是内网投产时真正该用的名单。

    【花名册路径的三级来源】
    1. `--roster` 命令行参数（推荐：不改动仓库任何文件）；
    2. `config.json` 的 `full_name_list_file`；
    3. 都没有 → **拒绝打包**。
       保留上游这条安全契约：没有花名册就交付一个"看着干净但
       精确匹配层为空"的包，等于交付残缺品。
    """
    ext = str(roster_path or "").strip() or str(
        cfg.get("full_name_list_file") or "").strip()
    if not ext:
        raise SystemExit(
            "[拒绝] --strip-pii 需要指定本行员工花名册：\n"
            "  · 命令行：--roster <花名册文件路径>\n"
            "  · 或在 config.json 配置 full_name_list_file\n"
            "  （也可用环境变量 AIKIT_FULL_NAME_LIST 指向外部文件）\n"
            "  否则精确匹配层为空，等于交付一个能力残缺的包。"
        )
    cfg["full_name_list_file"] = ext

    # (2) 先把字频派生出来，再剥离 (1)
    #     字频来源须与运行时 `_get_name_char_freq()` 的自举口径一致：
    #     运行时从「内联 full_name_list ∪ extra_name_list」派生，
    #     故此处两处名单都要计入，否则剥离后闸门行为会与原始不一致。
    names = [n for n in (cfg.get("full_name_list") or [])
             if isinstance(n, str) and n]
    names += [n for n in (cfg.get("extra_name_list") or [])
              if isinstance(n, str) and n]
    freq: Dict[str, int] = {}
    for name in names:
        for ch in name[1:]:
            freq[ch] = freq.get(ch, 0) + 1
    if freq:
        cfg["name_char_freq_map"] = freq

    # (1) 剥离内联人名库
    cfg["full_name_list"] = []

    # (1b) 剥离 D-5 补充确认真名清单（同样是客户 PII，Issue #60 引入）
    if cfg.get("extra_name_list"):
        cfg["extra_name_list"] = []
        cfg["extra_name_list_file"] = str(
            cfg.get("extra_name_list_file") or "scripts/data/extra_name_list.txt")

    # (3) 剥离内联银行名单（客户机构口径），改为外置
    #     仓库内联的 3 条来自历史客户的实际机构名，属于"把客户口径带给别人"；
    #     内网应由部署方填本行及需覆盖的目标机构。
    if cfg.get("bank_names"):
        cfg["bank_names"] = []
        cfg["bank_names_file"] = str(
            cfg.get("bank_names_file") or "scripts/data/bank_names.txt")

    # (4) 移除业务词保护表中的客户项目组名
    #     「尖刀组」是历史客户的项目组代号，被列入保护表意味着**豁免脱敏**。
    #     对通用分发包，客户项目代号本就该按普通机构名脱敏掉，
    #     保留豁免反而会把客户信息带进内网。移除后行为更正确。
    client_terms = ("尖刀组", "尖刀测试")
    gw = cfg.get("biz_guard_words") or []
    kept = [w for w in gw if not any(t in w for t in client_terms)]
    if len(kept) != len(gw):
        cfg["biz_guard_words"] = kept

    return cfg


def write_dist_readme(out_zip: zipfile.ZipFile, version: str,
                      files: List[str], deps: Dict[str, str],
                      pii_stripped: bool) -> None:
    """在包内写入安装与部署说明。"""
    pii_section = ""
    if pii_stripped:
        pii_section = """
## 一点五、花名册配置（**投产必做**）

本包已**剥离语料派生的真实人名库**（原 D16 全名库 7399 条），不携带任何
客户文档中挖掘出的自然人姓名。名字位的分级字频闸门已由独立字频表维持，
**闸门行为与原始版本完全一致，召回不掉**。

精确匹配层的名单来源改为本行花名册，部署方二选一：

```bash
# 方式一：直接填包内文件（推荐）
echo "张三" >> scripts/data/full_name_list.txt

# 方式二：环境变量指向外部花名册（无需改动分发包）
export AIKIT_FULL_NAME_LIST=/opt/ai-kit/data/本行员工花名册.txt
```

格式：每行一个姓名，UTF-8 纯文本。

> 若不配置花名册：技能包仍可运行，姓名识别由公共词典层
> （`scripts/data/common_name_dict.txt`，Apache-2.0）兜底，
> 但**本行员工姓名不在公共词典中，将无法被精确匹配**。
> 投产前务必完成本节配置。

### 一点五之一、人工确认真名补充清单（按需填写）

若本行文档中可能出现**名字位为生僻字、常规字频闸门无法识别**的姓名
（该类姓名会系统性漏脱），请填写补充清单：

```bash
echo "某生僻名" >> scripts/data/extra_name_list.txt
# 或：export AIKIT_EXTRA_NAME_LIST=/opt/ai-kit/data/确认真名.txt
```

格式与花名册一致（每行一个姓名）。留空不影响运行，但生僻字姓名会漏脱。
详见 `scripts/data/extra_name_list.README.txt`。

"""

    text = f"""# doc-redact 技能包（内网分发版）v{version}

本包为 Agent 技能包，供企业内网 Agent 装载调用。LLM 能力由 Agent 侧承载，
脚本侧 LLM 通道默认关闭（`config.json` 的 `llm.enabled = false`）。

## 一、安装

```bash
# 1) 校验包完整性（与随包 .sha256 文件核对）
sha256sum -c doc-redact-{version}.zip.sha256

# 2) 安装 Python 依赖（内网请加 -i <内网 PyPI 镜像>）
pip install -r requirements.txt

# 3) 安装自检（**必做**）
python3 scripts/doctor.py
```

`doctor.py` 输出安装体检报告：Python 版本、依赖包、外部二进制
（tesseract / LibreOffice）、包完整性、LLM 端点合规、路径残留。
退出码 `0` = 通过，`1` = 有必装项缺失（禁止投产），`2` = 有告警项。
{pii_section}
## 二、Agent 装载

- 技能说明与操作指引见包内 `SKILL.md`；
- 包内路径**不依赖当前工作目录**，脚本均以 `__file__` 自解析包根；
- 数据文件（人名库等）可通过 `AIKIT_DATA_ROOT` 环境变量指向内网数据目录。

## 三、LLM 端点合规（内网必做）

脚本侧 LLM 通道默认关闭。若需开启，须显式配置 `config.json` 的 `llm.base_url`，
且通过出网守卫校验（内网地址或 `allowed_hosts` 白名单）：

```bash
python3 scripts/endpoint_check.py --base-url https://llm-gw.corp.bank.local/v1
```

## 四、依赖清单

运行时依赖见 `requirements.txt`（{len(deps)} 项），外部二进制另需离线安装：
- `tesseract`（含中文语言包 chi_sim）—— 扫描件 OCR
- `LibreOffice` —— `.doc` / `.ppt` 格式转换

## 五、内容清单

本包共 {len(files)} 个文件，清单见同名 `.manifest.json`。

"""
    out_zip.writestr(README_DIST, text)


def build(outdir: Path, include_dev: bool, strip_pii: bool,
          roster_path: Optional[str] = None) -> Path:
    version = read_version()
    outdir.mkdir(parents=True, exist_ok=True)
    zip_path = outdir / f"doc-redact-{version}.zip"

    files = collect_files(include_dev)

    # 配置脱敏（若启用）
    cfg_rel = "scripts/config.json"
    cfg_override: Dict[str, Any] | None = None
    # 客户人名改写规则：从**原始**配置的两份名单动态推导
    # （剥离后名单已空，无法再推导，故必须在剥离前取）
    src_cfg = json.loads((PKG_ROOT / cfg_rel).read_text(encoding="utf-8"))
    case_name_rules = _case_name_redactions(src_cfg)
    if strip_pii:
        cfg_override = strip_pii_from_config(src_cfg, roster_path)

    manifest: Dict[str, Any] = {
        "package": "doc-redact",
        "version": version,
        "python_requires": ">=3.9",
        "include_dev": include_dev,
        "pii_stripped": strip_pii,
        "files": [],
    }

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, rel in files:
            data = src.read_bytes()
            if rel == cfg_rel and cfg_override is not None:
                data = json.dumps(cfg_override, ensure_ascii=False,
                                  indent=2).encode("utf-8")
            # 文本类文件改写客户案例引用：
            #   · 机构名规则 → 全部文本文件
            #   · 案例人名规则 → 仅源码与文档（名单/语料等数据文件不改写，
            #     否则 config.json 的 full_name_list 被清空、包内召回归零）
            suffix = Path(rel).suffix.lower()
            if suffix in (".py", ".md", ".txt", ".json"):
                apply_names = suffix in (".py", ".md")
                data = sanitize_text(data, case_name_rules, apply_names)
            zf.writestr(rel, data)
            manifest["files"].append({
                "path": rel,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })

        # PII 剥离包：随包提供空白花名册文件，部署方填入本行名单即可
        if strip_pii:
            roster_specs = [
                (cfg_override.get("full_name_list_file"),
                 "# 本行员工花名册（每行一个姓名，UTF-8）\n"
                 "# 填入本行员工姓名后生效；也可用环境变量 AIKIT_FULL_NAME_LIST 指向外部文件。\n"
                 "# 详细说明见 scripts/data/full_name_list.README.txt\n"),
                (cfg_override.get("extra_name_list_file"),
                 "# 本行人工确认真实姓名补充清单（每行一个姓名，UTF-8）\n"
                 "# 用于名字位生僻字、常规闸门无法识别但已确认是人名的情形。\n"
                 "# 也可用环境变量 AIKIT_EXTRA_NAME_LIST 指向外部文件。\n"),
            ]
            for roster_rel, roster_text in roster_specs:
                roster_rel = str(roster_rel or "").replace("\\", "/")
                if not roster_rel:
                    continue
                rb = roster_text.encode("utf-8")
                zf.writestr(roster_rel, rb)
                manifest["files"].append({
                    "path": roster_rel,
                    "size": len(rb),
                    "sha256": hashlib.sha256(rb).hexdigest(),
                })

        # 依赖声明写入 manifest（供内网 Agent 编排与离线镜像核对）
        req_txt = (PKG_ROOT / "requirements.txt").read_text(encoding="utf-8")
        deps: Dict[str, str] = {}
        for line in req_txt.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in (">=", "<=", "==", ">", "<", "~="):
                if sep in line:
                    name, ver = line.split(sep, 1)
                    deps[name.strip()] = f"{sep}{ver.strip()}"
                    break
            else:
                deps[line] = ""
        manifest["dependencies"] = deps
        manifest["external_binaries"] = ["tesseract (+chi_sim)", "LibreOffice"]

        write_dist_readme(zf, version, [f["path"] for f in manifest["files"]],
                          deps, strip_pii)

    # SHA256
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    (outdir / f"doc-redact-{version}.zip.sha256").write_text(
        f"{digest}  {zip_path.name}\n", encoding="utf-8")

    # manifest
    manifest["package_sha256"] = digest
    (outdir / f"doc-redact-{version}.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return zip_path


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 doc-redact 内网分发包")
    ap.add_argument("--outdir", default=str(PKG_ROOT / "dist"),
                    help="输出目录（默认 ./dist）")
    ap.add_argument("--include-dev", action="store_true",
                    help="连测试与 CI 一起打包（内网自测用）")
    ap.add_argument("--strip-pii", action="store_true",
                    help="移除配置内联真实人名库（须提供 --roster 或已配置 full_name_list_file）")
    ap.add_argument("--roster", default="",
                    help="本行员工花名册文件路径（--strip-pii 必需，除非 config.json 已配置）")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    zip_path = build(outdir, args.include_dev, args.strip_pii, args.roster)

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    print(f"[完成] 分发包：{zip_path}")
    print(f"[大小] {zip_path.stat().st_size:,} 字节")
    print(f"[SHA256] {digest}")
    print(f"[清单] {outdir / (zip_path.stem + '.manifest.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
