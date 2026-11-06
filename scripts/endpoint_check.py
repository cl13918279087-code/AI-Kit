#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# endpoint_check.py - Agent 直处理模式端点合规自检
# ---------------------------------------------------------------------------
#
# 【为什么需要】
#   本技能包是 **Agent 技能包**：LLM 调用能力由 Agent 侧承载。
#   脚本侧的出网守卫（scripts/llm_egress.py）只管得住**脚本自带的**
#   LLM 客户端通道；Agent 直处理模式（SKILL.md 第五节-A）用的是 Agent
#   自己配置的 LLM 端点，**不在本仓库的守卫管辖范围内**。
#
#   这带来一个部署口径上的空洞：
#     「技能包侧端点收口了」≠「实际发文的端点是内网」。
#   内网投产方需要一个能**独立验证**「Agent 侧 LLM 端点落在内网或白名单」
#   的手段，否则只能依赖口头承诺。
#
# 【本工具做什么】
#   对一个或多个「实际使用的 LLM 端点 URL」做与脚本侧守卫**同源**的合规判定：
#     1. scheme 必须是 http/https；
#     2. host 必须命中 allowed_hosts 白名单（精确或 .domain 后缀）；
#     3. 或 host 为内网地址（私网 IP / localhost / .internal / .lan / .corp …）；
#     4. 两者都不满足 → 判定**不合规**，并给出可执行的整改提示。
#
# 【与脚本侧守卫的关系】
#   本工具**只读**、**不发任何网络请求**，复用 llm_egress.check_egress()，
#   保证「自检口径」与「实际守卫口径」永不漂移。
#
# 【用法】
#   # 检查单个端点（端点参数可重复）
#   python3 scripts/endpoint_check.py --base-url https://llm.corp.bank.local/v1
#
#   # 读取 config.json 的 llm 段作为策略（白名单 / 是否强制内网）
#   python3 scripts/endpoint_check.py --base-url https://llm.corp.bank.local/v1 \
#       --config scripts/config.json
#
#   # 只检查 config.json 里配置的 base_url
#   python3 scripts/endpoint_check.py --config scripts/config.json
#
#   # 机器可读输出（供流水线 / 内网合规扫描调用）
#   python3 scripts/endpoint_check.py --base-url https://api.openai.com/v1 --json
#
# 【退出码】
#   0 = 全部端点合规
#   2 = 存在不合规端点（须整改）
#   3 = 参数/配置错误
#
# 【边界】
#   输出只含端点 URL、host 与规则判定，**不含任何文档内容**。
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_egress import (  # noqa: E402
    ALLOWED_SCHEMES,
    check_egress,
    describe_policy,
    is_private_host,
    normalize_host,
)

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# 整改提示（不含任何业务数据）
_HINT_INTERNAL = (
    "改用企业内网推理网关地址（如 http://llm-gw.corp.bank.local/v1），"
    "或在 config.json 的 llm.allowed_hosts 中显式加入该 host"
)
_HINT_SCHEME = "端点必须使用 http/https 协议"
_HINT_EMPTY = "请显式提供 --base-url，或在 config.json 的 llm.base_url 中配置端点"


def _load_llm_config(config_path: str) -> Dict[str, Any]:
    """读取 config.json 的 llm 段（不存在或非法时返回空字典）。"""
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    llm = data.get("llm")
    return llm if isinstance(llm, dict) else {}


def check_endpoint(base_url: str,
                   allowed_hosts: Optional[List[str]] = None,
                   require_private_network: bool = True) -> Dict[str, Any]:
    """对单个端点做合规判定，返回结构化结果。

    返回字段：base_url / host / compliant / reason / private_host /
              whitelist_hit / hint。**不含任何文档内容**。
    """
    allowed = list(allowed_hosts or [])
    ok, reason = check_egress(base_url, allowed, require_private_network)
    host = normalize_host(base_url)

    hint = ""
    if not ok:
        if not base_url or not str(base_url).strip():
            hint = _HINT_EMPTY
        elif host and host not in allowed and not is_private_host(host):
            hint = _HINT_INTERNAL
        else:
            hint = _HINT_SCHEME

    return {
        "base_url": base_url,
        "host": host,
        "compliant": bool(ok),
        "reason": reason,
        "private_host": bool(is_private_host(host)),
        "whitelist_hit": bool(host) and any(
            host == str(e).strip().lower().lstrip(".")
            or host.endswith("." + str(e).strip().lower().lstrip("."))
            for e in allowed if str(e).strip()
        ),
        "hint": hint,
    }


def _format_text_report(results: List[Dict[str, Any]], policy: str) -> str:
    lines: List[str] = []
    lines.append("=" * 68)
    lines.append("Agent 直处理模式 · LLM 端点合规自检")
    lines.append("=" * 68)
    lines.append(f"策略：{policy}")
    lines.append(f"允许的 scheme：{'/'.join(sorted(ALLOWED_SCHEMES))}")
    lines.append("")
    if not results:
        lines.append("[FAIL] 未提供任何待检端点——" + _HINT_EMPTY)
        return "\n".join(lines)

    for r in results:
        mark = "OK  " if r["compliant"] else "FAIL"
        lines.append(f"[{mark}] {r['base_url']}")
        lines.append(f"         host={r['host'] or '(无法解析)'}")
        lines.append(f"         内网地址={r['private_host']}  白名单命中={r['whitelist_hit']}")
        lines.append(f"         判定：{r['reason']}")
        if not r["compliant"] and r.get("hint"):
            lines.append(f"         整改：{r['hint']}")
        lines.append("")

    bad = [r for r in results if not r["compliant"]]
    if bad:
        lines.append(f"结论：{len(bad)}/{len(results)} 个端点不合规，"
                     f"Agent 直处理模式在整改前**不得**用于生产文档。")
    else:
        lines.append(f"结论：{len(results)}/{len(results)} 个端点全部合规，"
                     f"可按 SKILL.md 第五节-A 执行 Agent 直处理模式。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent 直处理模式 LLM 端点合规自检（只读，不发任何请求）"
    )
    parser.add_argument("--base-url", dest="base_urls", action="append",
                        help="待检 LLM 端点 URL（可重复传入多个）")
    parser.add_argument("--config", dest="config", default=DEFAULT_CONFIG,
                        help="策略配置文件路径（默认 scripts/config.json）")
    parser.add_argument("--allowed-host", dest="allowed_hosts", action="append",
                        help="额外放行的 host（精确或 .domain 后缀），可重复")
    parser.add_argument("--allow-public", dest="allow_public", action="store_true",
                        help="不强制要求内网地址（仅按白名单判定）")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="以 JSON 输出（供流水线调用）")
    args = parser.parse_args()

    llm_cfg = _load_llm_config(args.config)
    allowed = list(llm_cfg.get("allowed_hosts") or [])
    for h in args.allowed_hosts or []:
        if h and str(h).strip():
            allowed.append(str(h).strip())
    require_private = not args.allow_public
    if "require_private_network" in llm_cfg:
        require_private = bool(llm_cfg.get("require_private_network"))

    base_urls = [u for u in (args.base_urls or []) if u and str(u).strip()]
    if not base_urls:
        cfg_url = str(llm_cfg.get("base_url") or "").strip()
        if cfg_url:
            base_urls = [cfg_url]

    if not base_urls:
        if args.as_json:
            print(json.dumps({
                "compliant": False,
                "policy": describe_policy(allowed, require_private),
                "results": [],
                "reason": _HINT_EMPTY,
            }, ensure_ascii=False, indent=2))
        else:
            print(_format_text_report([], describe_policy(allowed, require_private)))
        return 3

    results = [check_endpoint(u, allowed, require_private) for u in base_urls]
    compliant = all(r["compliant"] for r in results)
    policy = describe_policy(allowed, require_private)

    if args.as_json:
        print(json.dumps({
            "compliant": compliant,
            "policy": policy,
            "require_private_network": require_private,
            "results": results,
        }, ensure_ascii=False, indent=2))
    else:
        print(_format_text_report(results, policy))

    return 0 if compliant else 2


if __name__ == "__main__":
    raise SystemExit(main())
