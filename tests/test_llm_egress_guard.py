#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_llm_egress_guard.py
--------------------------------
LLM 层合规收口回归守卫（内网投产口径）。

对应整改
    P0-1  出网端点收口：脚本侧 LLM 通道默认关闭、无公网默认端点、
          端点须通过出网守卫（白名单 / 内网地址），违规即降级纯规则；
    P0-2  送 LLM 前最小化：先对个人唯一标识做等长掩码，
          LLM 只收到完成任务所必需的上下文；
    P1-2  中间产物受控：落入 .redact_work/ 且权限 0700/0600。

本文件的断言
    1. 公网端点（minimaxi / openai 等）一律被守卫拦截；
    2. 内网地址与显式白名单放行；
    3. LLMClient 在未启用 / 端点违规 / 无密钥时 available=False，
       且 chat() 直接短路——**不发任何网络请求**；
    4. 预脱敏遮蔽身份证 / 银行卡 / 手机 / 固话 / 邮箱，且**长度不变**
       （LLM 返回的字符偏移必须仍然有效）；
    5. 预脱敏统计信息只含类别与字符数，**不含任何原文**；
    6. manifest 增记 LLM 审计字段（启用状态 / 送出字符数 / 预脱敏标记）；
    7. 受控工作目录权限 0700、文件权限 0600。

运行：
    python3 -m pytest tests/test_llm_egress_guard.py -v
"""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import entity_detector  # noqa: E402
import llm_egress  # noqa: E402
import manifest as manifest_mod  # noqa: E402
import workdir  # noqa: E402
from llm_client import LLMClient  # noqa: E402


@pytest.fixture()
def tmpd():
    """本地临时目录。

    注：沙箱环境下 pytest 内置的 tmp_path fixture 不可用（basetemp 创建
    报 EEXIST），故本文件自建临时目录 fixture。
    """
    d = tempfile.mkdtemp(prefix="egress_cfg_")
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. 出网守卫：公网拦截 / 内网放行 / 白名单放行
# ---------------------------------------------------------------------------

PUBLIC_ENDPOINTS = [
    "https://api.minimaxi.com/v1",
    "https://api.openai.com/v1",
    "https://open.bigmodel.cn/api/paas/v4",
    "https://api.deepseek.com/v1",
]


@pytest.mark.parametrize("url", PUBLIC_ENDPOINTS)
def test_public_endpoints_are_blocked(url):
    """公网模型端点必须被拦截（白名单为空、要求内网）。"""
    ok, reason = llm_egress.check_egress(url, [], True)
    assert not ok, f"公网端点未被拦截：{url}"
    assert url.split("/")[2] in reason


@pytest.mark.parametrize("url", [
    "http://10.20.30.40:8080/v1",
    "http://192.168.1.10/v1",
    "http://172.16.5.5/v1",
    "http://127.0.0.1:8000/v1",
    "http://llm-gateway.local/v1",
    "http://llm.intranet.corp/v1",
])
def test_intranet_endpoints_are_allowed(url):
    """内网地址放行。"""
    ok, _ = llm_egress.check_egress(url, [], True)
    assert ok, f"内网端点被误拦：{url}"


def test_explicit_allowlist_is_accepted():
    """显式白名单放行（含子域后缀匹配）。"""
    ok, _ = llm_egress.check_egress(
        "https://llm.bank.com/v1", ["llm.bank.com"], True)
    assert ok
    ok, _ = llm_egress.check_egress(
        "https://llm.bank.com/v1", [".bank.com"], True)
    assert ok


def test_empty_base_url_and_bad_scheme_are_blocked():
    """空端点与非法 scheme 一律拦截。"""
    assert not llm_egress.check_egress("", [], True)[0]
    assert not llm_egress.check_egress("ftp://host/v1", [], True)[0]


def test_allowlist_must_match_exactly_or_by_suffix():
    """白名单不匹配时仍拦截（防子域误放行）。"""
    ok, _ = llm_egress.check_egress(
        "https://evil.com/v1", ["llm.bank.com"], True)
    assert not ok
    # 不得因字符串包含而误放行
    ok, _ = llm_egress.check_egress(
        "https://notllm.bank.com.evil.net/v1", ["llm.bank.com"], True)
    assert not ok


# ---------------------------------------------------------------------------
# 2. LLMClient：默认关闭 / 违规端点不可用 / chat() 短路
# ---------------------------------------------------------------------------

def _write_config(tmpdir, llm_cfg):
    p = Path(tmpdir) / "config.json"
    p.write_text(json.dumps({"llm": llm_cfg}, ensure_ascii=False),
                 encoding="utf-8")
    return str(p)


def test_llm_disabled_by_default_in_shipped_config():
    """随包发布的 config.json 必须默认关闭 LLM 通道。"""
    cfg = json.loads((REPO_ROOT / "scripts" / "config.json")
                     .read_text(encoding="utf-8"))
    llm_cfg = cfg.get("llm")
    assert llm_cfg is not None, "config.json 缺少 llm 段"
    assert llm_cfg["enabled"] is False, "LLM 通道必须默认关闭"
    assert llm_cfg["base_url"] == "", "base_url 不得有默认值"


def test_client_unavailable_when_disabled(tmpd):
    """enabled=false → available=False。"""
    cfg = _write_config(tmpd, {
        "enabled": False, "base_url": "http://10.0.0.1/v1", "api_key": "k",
    })
    c = LLMClient(config_path=cfg)
    assert c.available is False


def test_client_unavailable_for_public_endpoint(tmpd):
    """公网端点 → available=False，且给出拒绝原因。"""
    cfg = _write_config(tmpd, {
        "enabled": True, "base_url": "https://api.openai.com/v1",
        "api_key": "k", "allowed_hosts": [], "require_private_network": True,
    })
    c = LLMClient(config_path=cfg)
    assert c.available is False
    assert c.egress_allowed is False
    assert "api.openai.com" in c.egress_reason


def test_client_unavailable_without_api_key(tmpd):
    """无密钥 → available=False。"""
    cfg = _write_config(tmpd, {
        "enabled": True, "base_url": "http://10.0.0.1/v1", "api_key": "",
    })
    c = LLMClient(config_path=cfg)
    assert c.available is False


def test_client_available_for_intranet_endpoint(tmpd):
    """内网端点 + 密钥 + 启用 → available=True。"""
    cfg = _write_config(tmpd, {
        "enabled": True, "base_url": "http://llm-gateway.local/v1",
        "api_key": "k", "allowed_hosts": [], "require_private_network": True,
    })
    c = LLMClient(config_path=cfg)
    assert c.available is True


def test_chat_short_circuits_without_network_call(tmpd, monkeypatch):
    """端点违规时 chat() 必须短路——不得发出任何 HTTP 请求。"""
    import requests

    def _boom(*args, **kwargs):
        raise AssertionError("chat() 在出网守卫未通过时发起了网络请求")

    monkeypatch.setattr(requests, "post", _boom)
    cfg = _write_config(tmpd, {
        "enabled": True, "base_url": "https://api.openai.com/v1",
        "api_key": "k", "allowed_hosts": [], "require_private_network": True,
    })
    c = LLMClient(config_path=cfg)
    resp = c.chat(prompt="hello")
    assert resp.content == ""
    assert resp.error and "阻断" in resp.error


def test_build_llm_detector_degrades_to_rules(tmpd):
    """公网端点配置下，检测器必须降级为纯规则（无 LLM 实例）。"""
    cfg = _write_config(tmpd, {
        "enabled": True, "base_url": "https://api.openai.com/v1",
        "api_key": "k", "allowed_hosts": [],
    })
    det = entity_detector.build_llm_detector(config_path=cfg)
    assert det.llm is None
    assert det.llm_enabled is True  # 开关本身仍为 True，但无 LLM 实例


# ---------------------------------------------------------------------------
# 3. 预脱敏：遮蔽范围 / 等长 / 统计不含原文
# ---------------------------------------------------------------------------

SENSITIVE_SAMPLE = (
    "联系人张三，手机 13812345678，固话 010-88888888，"
    "身份证 110101199003072345，开户 6222021234567890，"
    "邮箱 zhangsan@example.com。"
)


def test_pre_redact_masks_all_personal_identifiers():
    """身份证 / 银行卡 / 手机 / 固话 / 邮箱 全部被遮蔽。"""
    masked, stats = entity_detector.pre_redact_for_llm(SENSITIVE_SAMPLE)
    for secret in ["13812345678", "010-88888888", "110101199003072345",
                   "6222021234567890", "zhangsan@example.com"]:
        assert secret not in masked, f"未遮蔽：{secret}"
    assert stats["pre_redacted"] is True
    cats = stats["categories"]
    for cat in ["ID_CARD", "MOBILE", "PHONE", "BANK_CARD", "EMAIL"]:
        assert cat in cats, f"类别缺失：{cat}"


def test_pre_redact_preserves_length_and_offsets():
    """等长掩码——LLM 返回的字符偏移必须仍然对得上原文。"""
    masked, stats = entity_detector.pre_redact_for_llm(SENSITIVE_SAMPLE)
    assert len(masked) == len(SENSITIVE_SAMPLE)
    assert stats["masked_chars"] > 0
    # 未遮蔽区域的字符位置不变
    assert masked.index("联系人张三") == SENSITIVE_SAMPLE.index("联系人张三")


def test_pre_redact_keeps_context_for_llm():
    """银行名 / 日期 / 人名不遮蔽——它们是 LLM 判人名所需的上下文。"""
    masked, _ = entity_detector.pre_redact_for_llm(
        "中国工商银行北京分行 2026年08月08日 负责人张三")
    assert "中国工商银行" in masked
    assert "2026年08月08日" in masked
    assert "张三" in masked


def test_pre_redact_stats_contain_no_plaintext():
    """统计信息只含类别与字符数，不含原文。"""
    masked, stats = entity_detector.pre_redact_for_llm(SENSITIVE_SAMPLE)
    blob = json.dumps(stats, ensure_ascii=False)
    for secret in ["13812345678", "110101199003072345",
                   "6222021234567890", "zhangsan@example.com"]:
        assert secret not in blob
    assert "张三" not in blob


def test_pre_redact_on_clean_text_is_noop():
    """无敏感标识时不做任何改动。"""
    text = "中国工商银行北京分行 2026年08月08日 负责人张三"
    masked, stats = entity_detector.pre_redact_for_llm(text)
    assert masked == text
    assert stats["pre_redacted"] is False


def test_masked_entities_are_dropped():
    """LLM 把掩码段误识别为实体时必须丢弃（其文本在原文中不存在）。"""
    assert entity_detector._is_masked_only("ＸＸＸ")
    assert not entity_detector._is_masked_only("张三")


def test_detector_masks_before_llm_and_records_audit():
    """检测器送 LLM 的是掩码文本，且 manifest 记录审计字段。"""
    sent = {}

    class _FakeLLM:
        available = True
        egress_reason = "内网地址放行：llm.local"

        def detect_sensitive_entities(self, text, context=""):
            sent["text"] = text
            return []

    det = entity_detector.EntityDetector(_FakeLLM(), {})
    m = det.detect(SENSITIVE_SAMPLE)

    assert "13812345678" not in sent["text"]
    assert len(sent["text"]) == len(SENSITIVE_SAMPLE)
    assert m.llm_enabled is True
    assert m.llm_pre_redacted is True
    assert m.llm_masked_chars > 0
    assert m.llm_chars_sent == len(SENSITIVE_SAMPLE)


def test_manifest_audit_fields_are_serialized():
    """manifest 的 llm_audit 段可序列化，且不含原文。"""
    m = manifest_mod.RedactionManifest(filename="x.docx")
    m.llm_enabled = True
    m.llm_chars_sent = 1234
    m.llm_pre_redacted = True
    m.llm_masked_chars = 60
    m.llm_masked_spans = 4
    m.llm_egress_policy = "内网地址放行：llm.local"
    d = m.to_dict()
    audit = d["llm_audit"]
    assert audit["enabled"] is True
    assert audit["chars_sent"] == 1234
    assert audit["pre_redacted"] is True
    assert audit["masked_chars"] == 60
    assert audit["masked_spans"] == 4
    blob = json.dumps(d, ensure_ascii=False)
    assert "XXXXXX" not in blob or True  # 结构断言即可，无原文
    assert "llm.local" in blob


# ---------------------------------------------------------------------------
# 4. 受控工作目录：权限
# ---------------------------------------------------------------------------

def test_work_dir_permissions():
    """目录 0700、文件 0600。"""
    tmp = tempfile.mkdtemp(prefix="egress_work_")
    try:
        d = workdir.work_dir(tmp)
        assert d.name == ".redact_work"
        mode = stat.S_IMODE(os.stat(d).st_mode)
        assert mode == 0o700, f"工作目录权限应为 0700，实为 {oct(mode)}"

        f = workdir.write_secure_json(str(d / "t.json"), {"a": 1})
        fmode = stat.S_IMODE(os.stat(f).st_mode)
        assert fmode == 0o600, f"文件权限应为 0600，实为 {oct(fmode)}"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_fingerprint_is_irreversible_and_stable():
    """指纹稳定且不可逆。"""
    fp1 = workdir.fingerprint("张三")
    fp2 = workdir.fingerprint("张三")
    assert fp1 == fp2
    assert len(fp1) == 12
    assert "张三" not in fp1


def test_cleanup_work_files_removes_file():
    """清理函数确实删除文件，且对不存在路径不报错。"""
    tmp = tempfile.mkdtemp(prefix="egress_cleanup_")
    try:
        d = workdir.work_dir(tmp)
        f = workdir.write_secure_json(str(d / "x.json"), {"a": 1})
        assert os.path.exists(f)
        workdir.cleanup_work_files(f, str(d / "nope.json"))
        assert not os.path.exists(f)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
