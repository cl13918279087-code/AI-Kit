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
DEV_SCRIPT_EXCLUDE = [
    "mine_boundary_pairs.py",
    "mine_guard_words.py",
    "decision_mining.py",
    "redact_iterative_compare.py",
    "redact_iterative_v2.py",
    "bench_metrics.py",
    "quality_validator.py",
    "keep_audit.py",
    "residual_audit.py",
    "r4_name_scoring.py",
    "table_role_detector.py",
    "name_region_detector.py",
    "test_common_rules.py",
    "redact_llm.py",
    "redact_iterative_v2.py",
]

# 全部剥离的顶层文件
TOP_EXCLUDE = [
    ".DS_Store",
    ".gitignore",
    ".pytest_cache",
]

# 需要在包内生成的说明文件
README_DIST = "安装与部署说明（分发包内）"


def read_version() -> str:
    """从 SKILL.md frontmatter 读 version，缺省回退到常量。"""
    skill = PKG_ROOT / "SKILL.md"
    if skill.exists():
        for line in skill.read_text(encoding="utf-8").splitlines()[:20]:
            if line.strip().startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    return sys_version


def collect_files(include_dev: bool) -> List[Tuple[Path, str]]:
    """收集待打包文件，返回 (源路径, 包内相对路径) 列表。"""
    picked: Dict[str, Path] = {}

    for rel in REQUIRED_INCLUDE:
        p = PKG_ROOT / rel
        if p.exists():
            picked[rel] = p

    for d in OPTIONAL_DIRS:
        dd = PKG_ROOT / d
        if not dd.is_dir():
            continue
        for p in sorted(dd.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(PKG_ROOT))
                picked.setdefault(rel, p)

    if include_dev:
        for d in ("tests", "docs", ".github"):
            dd = PKG_ROOT / d
            if not dd.is_dir():
                continue
            for p in sorted(dd.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    picked.setdefault(str(p.relative_to(PKG_ROOT)), p)

    # 剥离开发期脚本
    if not include_dev:
        for rel in list(picked):
            if rel.startswith("scripts/") and Path(rel).name in DEV_SCRIPT_EXCLUDE:
                picked.pop(rel)

    # 剥离顶层杂项
    for rel in list(picked):
        if Path(rel).name in TOP_EXCLUDE or rel.startswith("."):
            picked.pop(rel)

    return sorted(((p, rel) for rel, p in picked.items()), key=lambda x: x[1])


def strip_pii_from_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """分发包脱敏：移除内联真实人名库（仅保留外置文件引用）。

    语义影响：精确匹配层失去语料派生名单，字频自举种子随之改变，
    姓名召回会下降——因此**仅在已提供本行花名册外置文件时**使用，
    否则拒绝脱敏（避免交付一个"看着干净但能力残缺"的包）。
    """
    ext = str(cfg.get("full_name_list_file") or "").strip()
    if not ext:
        raise SystemExit(
            "[拒绝] --strip-pii 需要先在 config.json 配置 full_name_list_file "
            "（指向本行员工花名册外置文件），否则脱敏后姓名召回将显著下降。"
        )
    cfg["full_name_list"] = []
    return cfg


def write_dist_readme(out_zip: zipfile.ZipFile, version: str,
                      files: List[str], deps: Dict[str, str]) -> None:
    """在包内写入安装与部署说明。"""
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


def build(outdir: Path, include_dev: bool, strip_pii: bool) -> Path:
    version = read_version()
    outdir.mkdir(parents=True, exist_ok=True)
    zip_path = outdir / f"doc-redact-{version}.zip"

    files = collect_files(include_dev)

    # 配置脱敏（若启用）
    cfg_rel = "scripts/config.json"
    cfg_override: Dict[str, Any] | None = None
    if strip_pii:
        cfg = json.loads((PKG_ROOT / cfg_rel).read_text(encoding="utf-8"))
        cfg_override = strip_pii_from_config(cfg)

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
            zf.writestr(rel, data)
            manifest["files"].append({
                "path": rel,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
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

        write_dist_readme(zf, version, [f["path"] for f in manifest["files"]], deps)

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
                    help="移除配置内联真实人名库（须已配置 full_name_list_file）")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    zip_path = build(outdir, args.include_dev, args.strip_pii)

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    print(f"[完成] 分发包：{zip_path}")
    print(f"[大小] {zip_path.stat().st_size:,} 字节")
    print(f"[SHA256] {digest}")
    print(f"[清单] {outdir / (zip_path.stem + '.manifest.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
