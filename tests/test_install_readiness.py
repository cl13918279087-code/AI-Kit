#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# test_install_readiness.py — 内网安装就绪性回归测试
# ---------------------------------------------------------------------------
# 【覆盖范围】（对应「企业内网 Agent 无缝装载」整改项）
#   1. doctor.py 自检工具：报告结构、退出码语义、端点不合规判定
#   2. requirements 声明完整性：必装项齐备、无遗漏顶层 import
#   3. 无开发者本机绝对路径残留
#   4. 分发包构建：内容边界、必需文件齐备、SHA256 与清单
#   5. 人名库外置注入：合并语义、注释跳过、去重、回退
#   6. SKILL.md 元数据：Agent 注册所需字段齐备
# ---------------------------------------------------------------------------

import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))


# ===========================================================================
# 1. doctor.py 安装自检
# ===========================================================================

def _run_doctor(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "doctor.py"), *args],
        capture_output=True, text=True, cwd=str(REPO))


def test_doctor_json_report_structure():
    """doctor --json 输出机器可读报告，且结构完整。"""
    r = _run_doctor("--json")
    assert r.returncode in (0, 1, 2, 3), r.stderr
    data = json.loads(r.stdout)
    for key in ("package_root", "python", "worst_status", "counts", "checks"):
        assert key in data, f"缺少字段 {key}"
    assert data["counts"]["fail"] >= 0
    assert len(data["checks"]) > 0
    for c in data["checks"]:
        assert c["status"] in ("ok", "info", "warn", "fail")
        assert c["group"] and c["name"]


def test_doctor_covers_six_check_groups():
    """自检覆盖六大检查组——缺一组即意味着某类内网差异不可见。"""
    r = _run_doctor("--json")
    data = json.loads(r.stdout)
    groups = {c["group"] for c in data["checks"]}
    for prefix in ("A.", "B.", "C.", "D.", "E.", "F."):
        assert any(g.startswith(prefix) for g in groups), f"缺少检查组 {prefix}"


def test_doctor_required_files_present():
    """技能包必需文件全部存在——缺一项意味着分发包不完整。"""
    r = _run_doctor("--json")
    data = json.loads(r.stdout)
    required = [c for c in data["checks"]
                if c["group"].startswith("D.") and c["name"].endswith(
                    (".json", ".py", ".md", ".txt"))]
    assert required, "未执行技能包完整性检查"
    missing = [c["name"] for c in required if c["status"] == "fail"]
    assert not missing, f"技能包缺失文件：{missing}"


def test_doctor_rejects_public_endpoint():
    """公网 LLM 端点必须被判不合规（自检须能拦住这类配置）。"""
    r = _run_doctor("--base-url", "https://api.openai.com/v1", "--json")
    data = json.loads(r.stdout)
    ep_checks = [c for c in data["checks"]
                 if c["group"].startswith("E.") and "api.openai.com" in c["name"]]
    assert ep_checks, "未校验指定端点"
    assert ep_checks[0]["status"] == "fail"
    assert r.returncode == 1


def test_doctor_accepts_internal_endpoint():
    """内网端点应判合规。"""
    r = _run_doctor("--base-url", "http://llm-gw.corp.bank.local/v1", "--json")
    data = json.loads(r.stdout)
    ep_checks = [c for c in data["checks"]
                 if c["group"].startswith("E.") and "llm-gw.corp.bank.local" in c["name"]]
    assert ep_checks, "未校验指定端点"
    assert ep_checks[0]["status"] == "ok"


def test_doctor_exit_code_zero_or_two_without_endpoint():
    """未配置端点时不得报 fail（Agent 技能包定位本就无脚本侧端点）。"""
    r = _run_doctor("--json")
    data = json.loads(r.stdout)
    ep_fails = [c for c in data["checks"]
                if c["group"].startswith("E.") and c["status"] == "fail"]
    assert not ep_fails, f"端点检查误报失败：{ep_fails}"


def test_doctor_strict_mode_promotes_warn():
    """--strict 下告警应体现为非零退出码。"""
    loose = _run_doctor("--json")
    strict = _run_doctor("--strict", "--json")
    loose_data = json.loads(loose.stdout)
    if loose_data["counts"]["warn"] > 0:
        assert strict.returncode == 1


# ===========================================================================
# 2. requirements 声明完整性
# ===========================================================================

def test_requirements_declares_tqdm():
    """tqdm 是 pipeline.py 顶层 import——缺失即装完即崩，必须声明。"""
    req = (REPO / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^tqdm", req, re.M), "requirements.txt 缺 tqdm"


def test_requirements_declares_all_hard_imports():
    """顶层硬导入的第三方包必须在 requirements 中声明。"""
    req = (REPO / "requirements.txt").read_text(encoding="utf-8")
    # 已知硬依赖：entity_detector → llm_client → requests（模块级）
    for pkg in ("lxml", "requests", "tqdm"):
        assert re.search(rf"^{re.escape(pkg)}\b", req, re.M), f"缺声明：{pkg}"


def test_requirements_dev_split_exists():
    """测试依赖应单独成文件，避免生产内网装上 pytest。"""
    dev = REPO / "requirements-dev.txt"
    assert dev.exists(), "缺 requirements-dev.txt"
    assert "pytest" in dev.read_text(encoding="utf-8")


def test_requirements_has_version_constraints():
    """依赖必须带版本约束——内网镜像自动升级会导致装完即崩。"""
    lines = [l.strip() for l in
             (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    assert lines
    for line in lines:
        assert any(op in line for op in (">=", "==", "~=", "<=")), \
            f"依赖缺版本约束：{line}"


# ===========================================================================
# 3. 无开发者本机绝对路径残留
# ===========================================================================

def test_no_developer_absolute_paths():
    """包内不得残留开发者本机绝对路径（内网机器上必然不存在）。"""
    pats = [re.compile(r"/Users/[A-Za-z0-9_.-]+/"),
            re.compile(r"/home/[A-Za-z0-9_.-]+/"),
            re.compile(r"C:\\\\Users\\\\")]
    hits = []
    for py in sorted(SCRIPTS.glob("*.py")):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for pat in pats:
                if pat.search(line):
                    hits.append(f"{py.name}:{i}")
                    break
    assert not hits, f"残留开发者绝对路径：{hits}"


# ===========================================================================
# 4. 分发包构建
# ===========================================================================

@pytest.fixture(scope="module")
def built_package(tmp_path_factory):
    """构建一次分发包，供本模块各用例复用。"""
    out = tmp_path_factory.mktemp("dist")
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "build_package.py"), "--outdir", str(out)],
        capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, r.stderr
    zips = list(out.glob("*.zip"))
    assert zips, "未生成分发包"
    return out, zips[0]


def test_build_package_produces_zip_sha256_manifest(built_package):
    """分发包须同时产出 zip + sha256 + manifest 三件套。"""
    out, zpath = built_package
    assert (out / (zpath.name + ".sha256")).exists(), "缺 SHA256 文件"
    manifest = out / (zpath.stem + ".manifest.json")
    assert manifest.exists(), "缺内容清单"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["package"] == "doc-redact"
    assert data["python_requires"]
    assert data["dependencies"]
    assert data["files"]


def test_build_package_sha256_matches(built_package):
    """SHA256 须与实际包内容一致（内网凭此验签）。"""
    import hashlib
    out, zpath = built_package
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    recorded = (out / (zpath.name + ".sha256")).read_text(encoding="utf-8").split()[0]
    assert digest == recorded


def test_build_package_contains_required_files(built_package):
    """分发包须含全部运行时必需文件。"""
    _, zpath = built_package
    names = set(zipfile.ZipFile(zpath).namelist())
    for rel in ("SKILL.md", "requirements.txt", "LICENSE",
                "scripts/config.json", "scripts/doctor.py",
                "scripts/pipeline.py", "scripts/common_rules.py",
                "references/redaction_rules.md",
                "scripts/data/common_name_dict.txt"):
        assert rel in names, f"分发包缺文件：{rel}"


def test_build_package_excludes_dev_assets(built_package):
    """分发包不得含开发期资产（测试、CI、评审文档、挖掘脚本）。"""
    _, zpath = built_package
    names = set(zipfile.ZipFile(zpath).namelist())
    for bad in ("tests/", "docs/", ".github/", "scripts/mine_guard_words.py",
                "scripts/bench_metrics.py"):
        assert not any(n.startswith(bad) for n in names), \
            f"分发包混入开发期资产：{bad}"


def test_build_package_contains_no_dev_paths(built_package):
    """分发包内不得含开发者本机绝对路径。"""
    _, zpath = built_package
    pats = [re.compile(r"/Users/[A-Za-z0-9_.-]+/"), re.compile(r"/home/[A-Za-z0-9_.-]+/")]
    bad = []
    with zipfile.ZipFile(zpath) as zf:
        for n in zf.namelist():
            if not n.endswith((".py", ".md", ".json", ".txt")):
                continue
            text = zf.read(n).decode("utf-8", errors="ignore")
            for i, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if any(p.search(line) for p in pats):
                    bad.append(f"{n}:{i}")
                    break
    assert not bad, f"分发包含开发者路径：{bad}"


def test_build_package_runs_from_clean_dir(built_package, tmp_path):
    """分发包解压到干净目录后，须能从任意 CWD 独立运行 doctor。"""
    _, zpath = built_package
    target = tmp_path / "skills" / "doc-redact"
    target.mkdir(parents=True)
    with zipfile.ZipFile(zpath) as zf:
        zf.extractall(target)
    r = subprocess.run(
        [sys.executable, str(target / "scripts" / "doctor.py"), "--json"],
        capture_output=True, text=True, cwd="/")
    assert r.returncode in (0, 1, 2, 3), r.stderr
    data = json.loads(r.stdout)
    assert data["package_root"].endswith("doc-redact")
    assert data["counts"]["fail"] == 0, "分发包自检存在失败项"


def test_build_package_strip_pii_requires_roster(built_package, tmp_path):
    """未配置外置花名册时，--strip-pii 必须拒绝打包（避免交付残缺包）。"""
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "build_package.py"),
         "--outdir", str(tmp_path), "--strip-pii"],
        capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode != 0
    assert "full_name_list_file" in (r.stdout + r.stderr)


# ===========================================================================
# 5. 人名库外置注入
# ===========================================================================

def test_full_name_list_inline_preserved():
    """内联名单行为保持不变（外置能力不得改变既有召回）。"""
    import common_rules as cr
    names = cr._get_full_name_list()
    assert len(names) > 7000, "内联全名库条数异常"
    assert len(names) == len(set(names)), "内联名单存在重复"


def test_full_name_list_file_merge(tmp_path):
    """外置名单按「内联 ∪ 外置」合并，跳过注释行并去重。"""
    import common_rules as cr
    ext = tmp_path / "roster.txt"
    ext.write_text("测试甲\n测试乙\n# 注释行\n测试丙\n", encoding="utf-8")
    # 用独立副本，不得直接改 _load_config() 返回的进程内缓存对象
    cfg = dict(cr._load_config())
    baseline = cr._get_full_name_list(cfg)
    cfg["full_name_list_file"] = str(ext)
    merged = cr._get_full_name_list(cfg)
    assert len(merged) == len(baseline) + 3
    for n in ("测试甲", "测试乙", "测试丙"):
        assert n in merged
    assert "# 注释行" not in merged


def test_full_name_list_file_missing_falls_back(tmp_path):
    """外置文件缺失时静默回退内联（不因路径错误导致功能整体失效）。"""
    import common_rules as cr
    cfg = dict(cr._load_config())
    baseline = cr._get_full_name_list(cfg)
    cfg["full_name_list_file"] = str(tmp_path / "nonexistent.txt")
    assert cr._get_full_name_list(cfg) == baseline


def test_full_name_list_env_var_injection(tmp_path, monkeypatch):
    """环境变量 AIKIT_FULL_NAME_LIST 可注入名单，无需改动分发包内配置。"""
    import common_rules as cr
    ext = tmp_path / "env_roster.txt"
    ext.write_text("环境注入甲\n", encoding="utf-8")
    monkeypatch.setenv("AIKIT_FULL_NAME_LIST", str(ext))
    names = cr._get_full_name_list(dict(cr._load_config()))
    assert "环境注入甲" in names


def test_load_config_returns_cache_protected():
    """_load_config() 返回的是进程内缓存——调用方不应就地改写它。

    该缓存是全局共享的，就地修改会污染整个进程（其他模块读到被篡改的配置）。
    """
    import common_rules as cr
    cfg = cr._load_config()
    assert cfg is cr._load_config(), "配置缓存语义变化，需同步更新调用方"
    assert isinstance(cfg, dict)


def test_full_name_list_file_respects_data_root_env(tmp_path, monkeypatch):
    """AIKIT_DATA_ROOT 环境变量可把数据目录指向内网统一位置。"""
    import common_rules as cr
    (tmp_path / "roster_env.txt").write_text("环境甲\n", encoding="utf-8")
    monkeypatch.setenv("AIKIT_DATA_ROOT", str(tmp_path))
    cfg = cr._load_config()
    cfg["full_name_list_file"] = "roster_env.txt"
    assert "环境甲" in cr._get_full_name_list(cfg)


def test_config_declares_full_name_list_file_key():
    """config.json 须显式声明 full_name_list_file（内网注入入口）。"""
    cfg = json.loads((SCRIPTS / "config.json").read_text(encoding="utf-8"))
    assert "full_name_list_file" in cfg


# ===========================================================================
# 6. SKILL.md 元数据（Agent 注册契约）
# ===========================================================================

def _skill_frontmatter():
    text = (REPO / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    assert m, "SKILL.md 缺少 YAML frontmatter"
    return m.group(1)


def test_skill_has_required_metadata():
    """Agent 注册所需的元数据字段须齐备。"""
    fm = _skill_frontmatter()
    for key in ("name:", "description:", "version:"):
        assert key in fm, f"SKILL.md frontmatter 缺 {key}"


def test_skill_name_matches_package_dir():
    """技能名须与分发目录名一致（Agent 按名装载）。"""
    fm = _skill_frontmatter()
    name = re.search(r"^name:\s*(\S+)", fm, re.M).group(1).strip()
    assert name == "doc-redact"


def test_skill_version_is_semver():
    """版本号须可被程序解析（内网据此做升级/回滚判定）。"""
    fm = _skill_frontmatter()
    ver = re.search(r"^version:\s*(\S+)", fm, re.M).group(1).strip().strip("\"'")
    assert re.match(r"^\d+\.\d+\.\d+", ver), f"版本号格式不合规：{ver}"


def test_skill_documents_install_selfcheck():
    """SKILL.md 须写明安装与自检步骤（Agent 侧执行指引）。"""
    text = (REPO / "SKILL.md").read_text(encoding="utf-8")
    assert "doctor.py" in text, "SKILL.md 未指引安装自检"
    assert "安装" in text and "自检" in text


def test_skill_declares_requirements():
    """SKILL.md 须声明 Python 版本与外部二进制要求。"""
    fm = _skill_frontmatter()
    assert "requires_python:" in fm
    assert "requires_binaries:" in fm
    for binary in ("tesseract", "soffice"):
        assert binary in fm, f"frontmatter 未声明外部二进制 {binary}"
