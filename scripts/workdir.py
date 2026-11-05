#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# workdir.py - 受控工作目录（敏感中间产物的统一落盘口径）
# ---------------------------------------------------------------------------
#
# 【问题】
#   脱敏链路会产生若干含**原文片段**的中间文件（灰区评审清单、裁决回灌
#   文件、格式转换中间产物）。这些副本原先散落在系统临时目录，
#   无访问控制、无生命周期、无审计——在数据分类分级体系下，
#   它们应当与原始文档同级管理。
#
# 【口径】
#   1. 统一落盘于 `<基准目录>/.redact_work/`；
#   2. 目录权限 0700、文件权限 0600；
#   3. 任务结束时清理由调用方显式触发（`cleanup_work_files`），
#      异常路径亦须清理（调用方用 try/finally）；
#   4. 需长期留存的人工评审交付物（灰区评审 JSON）保留在受控目录，
#      由评审方自行处置——不在工具侧自动删除。
#
# 【基准目录选择】
#   优先取输出文件所在目录，其次取输入文件所在目录；均不可得时
#   回落到系统临时目录（此时仍受权限与生命周期管控）。
# ---------------------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

WORK_DIR_NAME = ".redact_work"


def work_dir(base_dir: Optional[str] = None) -> Path:
    """获取（并创建）受控工作目录，权限 0700。"""
    if base_dir:
        base = Path(base_dir)
    else:
        base = Path(tempfile.gettempdir())
    d = base / WORK_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def write_secure_json(path: str, payload) -> str:
    """写 JSON 文件并设权限 0600，返回路径。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return str(p)


def fingerprint(text: str) -> str:
    """原文指纹（sha256 前 12 位十六进制）——不可逆，用于审计关联。"""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def cleanup_work_files(*paths: Optional[str]) -> None:
    """清理中间文件（异常路径亦须调用）。"""
    for p in paths:
        if not p:
            continue
        try:
            fp = Path(p)
            if fp.exists():
                fp.unlink()
        except OSError:
            pass
