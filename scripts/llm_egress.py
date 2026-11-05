#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# llm_egress.py - LLM 出网端点守卫（内网投产合规）
# ---------------------------------------------------------------------------
#
# 【背景】
#   本技能包是 Agent 技能包，LLM 调用能力由 Agent 侧承载。但仓库内仍存在
#   脚本级 LLM 客户端（llm_client.py / redact_llm.py），若配置不当会把
#   **未脱敏原文**直发公网模型端点——这在银行内网投产口径下不可接受。
#
# 【守卫口径】
#   1. base_url 必须显式配置（**无公网默认值**）；
#   2. 端点 host 必须命中 allowed_hosts 白名单，**或**为内网地址
#      （私网 IP / localhost / .local / .internal / .lan / .corp 等）；
#   3. 两者都不满足 → 判定拒绝，调用方降级为纯规则检测，绝不发起请求；
#   4. 拒绝原因**不回显任何文档原文**，仅回显端点与规则名。
#
# 【配置】（config.json 的 llm 段）
#   {
#     "llm": {
#       "enabled": false,              # 总开关，默认关闭
#       "provider": "openai-compatible",
#       "model": "",
#       "base_url": "",                # 必填，无默认值
#       "api_key": "",
#       "allowed_hosts": [],           # 显式白名单（精确或 .domain 后缀）
#       "require_private_network": true,  # 仅允许内网地址
#       "timeout": 120,
#       "max_retries": 3
#     }
#   }
# ---------------------------------------------------------------------------

from __future__ import annotations

import ipaddress
from typing import List, Optional, Tuple
from urllib.parse import urlparse

# 内网主机名后缀（企业内网常见的内部域名约定）
INTERNAL_SUFFIXES = (
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".corp",
    ".intra",
)

# 内网主机名（精确匹配）
INTERNAL_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "host.docker.internal",
})

# 允许的 URL scheme
ALLOWED_SCHEMES = frozenset({"http", "https"})


def normalize_host(base_url: str) -> str:
    """从 URL 提取 host（去端口、小写）。无法解析时返回空串。"""
    if not base_url or not isinstance(base_url, str):
        return ""
    try:
        host = urlparse(base_url.strip()).hostname or ""
    except Exception:
        return ""
    return host.strip().lower()


def is_private_host(host: str) -> bool:
    """判断 host 是否为内网地址。

    判定范围：
      · IPv4 私网段 10/8、172.16/12、192.168/16、127/8、169.254/16
      · IPv6 回环 ::1 与唯一本地地址 fc00::/7、链路本地 fe80::/10
      · 内网主机名（localhost / *.local / *.internal / *.lan / *.corp …）
    """
    if not host:
        return False
    h = host.strip().lower()

    if h in INTERNAL_HOSTNAMES:
        return True
    if any(h.endswith(sfx) for sfx in INTERNAL_SUFFIXES):
        return True

    # 去掉 IPv6 的方括号
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]

    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False

    if ip.version == 4:
        return bool(
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_unspecified
        )
    # IPv6
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
    )


def _host_matches(host: str, allowed: List[str]) -> bool:
    """host 是否命中白名单（精确匹配或 .domain 后缀匹配）。"""
    for entry in allowed or []:
        if not entry:
            continue
        e = str(entry).strip().lower().lstrip(".")
        if not e:
            continue
        if host == e:
            return True
        # 后缀匹配：example.com 命中 llm.example.com
        if host.endswith("." + e):
            return True
    return False


def check_egress(base_url: str,
                 allowed_hosts: Optional[List[str]] = None,
                 require_private_network: bool = True) -> Tuple[bool, str]:
    """出网端点校验。

    返回 (是否放行, 原因)。原因串只含端点与规则名，不含任何文档内容。
    """
    if not base_url or not isinstance(base_url, str) or not base_url.strip():
        return False, "未配置 base_url（LLM 出网默认禁止）"

    try:
        parsed = urlparse(base_url.strip())
    except Exception:
        return False, "base_url 格式非法"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return False, f"base_url scheme 不允许（仅 http/https）：{scheme or '(空)'}"

    host = normalize_host(base_url)
    if not host:
        return False, "base_url 缺少 host"

    allowed = list(allowed_hosts or [])

    # 规则 1：命中显式白名单 → 放行
    if _host_matches(host, allowed):
        return True, f"命中白名单：{host}"

    # 规则 2：内网地址放行
    if require_private_network and is_private_host(host):
        return True, f"内网地址放行：{host}"

    # 规则 3：白名单为空且未要求内网 → 仍不放行（默认拒绝）
    if not allowed:
        return False, (
            f"端点未配置白名单：{host}"
            "（请配置 llm.allowed_hosts 或使用内网地址）"
        )

    return False, (
        f"端点不在白名单内：{host}"
        "（请加入 llm.allowed_hosts，或改用内网推理网关）"
    )


def describe_policy(allowed_hosts: Optional[List[str]] = None,
                    require_private_network: bool = True) -> str:
    """生成人类可读的守卫策略说明（用于 manifest 审计留痕，不含敏感内容）。"""
    allowed = list(allowed_hosts or [])
    return (
        f"allowed_hosts={allowed or '(空)'}；"
        f"require_private_network={'是' if require_private_network else '否'}"
    )


# ---------------------------------------------------------------------------
# 运行时 LLM 状态（供 manifest 审计留痕）
# ---------------------------------------------------------------------------
# 只记状态与计数，**绝不记原文**。

_RUNTIME: dict = {
    "enabled": False,
    "chars_sent": 0,
    "pre_redacted": False,
    "masked_chars": 0,
    "masked_spans": 0,
    "egress_policy": "",
}


def record_runtime(enabled: bool = False,
                   chars_sent: int = 0,
                   pre_redacted: bool = False,
                   masked_chars: int = 0,
                   masked_spans: int = 0,
                   egress_policy: str = "") -> None:
    """记录本次运行的 LLM 状态。"""
    _RUNTIME.update({
        "enabled": bool(enabled),
        "chars_sent": int(chars_sent or 0),
        "pre_redacted": bool(pre_redacted),
        "masked_chars": int(masked_chars or 0),
        "masked_spans": int(masked_spans or 0),
        "egress_policy": egress_policy or "",
    })


def get_runtime() -> dict:
    """读取本次运行的 LLM 状态（副本）。"""
    return dict(_RUNTIME)
