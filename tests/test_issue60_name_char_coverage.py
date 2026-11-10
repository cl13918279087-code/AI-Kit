#!/usr/bin/env python3
"""
test_issue60_name_char_coverage.py - Issue #60 回归守卫（批次二 D-5：角色槽位姓名强判别）

【缺陷现象】
角色槽位（"负责人：……" / "组长：……"）中的低频生僻姓名不被脱敏：

    "组长：刘兆缋"              → "组长：刘兆缋"     ❌
    "负责人：李熔、王谧"          → "负责人：李熔、王谧" ❌
    "将李熔、王谧加入项目组"       → 原文未脱            ❌

根因有两层：
  ① `_ROLE_NAME_RE` 的右边界断言写作 `(?![\\u4e00-\\u9fa5\\d])`，在 Python re 中
     被解析为 `0x30–U+9FA5` 的**连续区间**，导致槽位姓名后紧跟「、」「，」时整条
     名单漏脱；
  ② 生僻字姓名（如「熔」「缋」「谧」「葭」）字频不足，被 D16 字频门控拦截。

【修复（D-5，方案 2）】
  ① 角色槽位内**强判别**：按顿号/逗号/分号切分槽位条目，逐条判定

      | 判据                                        | 结论   |
      |---------------------------------------------|--------|
      | 2–4 字纯汉字 + 姓氏起点                       | 人名   |
      | 命中 L2 职务标签（副行长/总监/组长…）          | 保留   |
      | 命中通用词护栏 / 业务词 / 机构词              | 保留   |
      | 尾字为行政机构后缀（市区县镇乡村路街道巷弄号院所校局委班组厅厂矿站）| 保留 |
      | 「人名 + 职务」                              | 只脱人名 |

     作用域严格限定在**槽位内**，槽位外不做任何新判别，因此通用语料误伤增量为 0。
  ② #60 §一列出的 20 个确知真实姓名写入 `config.extra_name_list`，并入 D16 精确
     全名层（`_full_name_list`），槽位内外均按**精确匹配**脱敏，零误伤风险。

【说明】曾尝试「槽位外低频生僻名兜底」（D-5b，名字位字频 == 0 判据），实测在真实
业务语料造成 24 条新增误伤（支撑/占比/软件/全覆盖/教训…），证明 D16 自举字频对
「熔/谧/葭/缋」与「撑/比/件/盖/训」不可区分，该通道已整体移除。

运行方式：
    python -m pytest tests/test_issue60_name_char_coverage.py -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common_rules  # noqa: E402
from common_rules import apply_redactions  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, "scripts", "config.json")


def _cfg():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. 配置：extra_name_list 落地
# ---------------------------------------------------------------------------

def test_extra_name_list_present():
    """#60 §一的 20 个确知真实姓名写入配置。"""
    names = _cfg().get("extra_name_list") or []
    assert len(names) >= 20, f"extra_name_list 数量不足：{len(names)}"
    for n in ("李熔", "王谧", "刘兆缋", "蔡昀煜", "成葭"):
        assert n in names, f"extra_name_list 缺少 {n}"


# ---------------------------------------------------------------------------
# 2. 槽位内强判别
# ---------------------------------------------------------------------------

def test_slot_single_rare_name_masked():
    """单条目槽位：生僻字姓名脱敏且无残尾。"""
    out = apply_redactions("组长：刘兆缋")
    assert "刘兆缋" not in out
    assert "缋" not in out, f"出现残尾：{out}"
    assert "组长" in out


def test_slot_list_rare_names_masked():
    """多条目槽位：顿号分隔的名单整条脱敏（不再因右边界断言漏脱）。"""
    out = apply_redactions("负责人：李熔、王谧")
    assert "李熔" not in out and "王谧" not in out
    assert out.count("XXX") >= 2, out
    assert "负责人" in out


def test_slot_names_outside_anchor():
    """无槽位前缀但名单并列出现时也应脱敏（句式「将 A、B 加入项目组」）。"""
    out = apply_redactions("将李熔、王谧加入项目组")
    assert "李熔" not in out and "王谧" not in out
    assert "加入项目组" in out


# ---------------------------------------------------------------------------
# 3. 精确名单层（extra_name_list 并入 D16 全名层）
# ---------------------------------------------------------------------------

def test_extra_name_exact_match_anywhere():
    """精确名单在任意上下文脱敏，不依赖槽位。"""
    for text in ("李熔负责", "由李熔牵头", "联系人蔡昀煜", "成葭提交了方案"):
        out = apply_redactions(text)
        for n in ("李熔", "蔡昀煜", "成葭"):
            if n in text:
                assert n not in out, f"{text} -> {out}"


def test_all_issue60_names_masked_in_both_contexts():
    """#60 §一的 20 个实名，在「角色槽位」与「无槽位」两种上下文中均脱敏。

    对应 #60 §四 验收第 1 条（槽位 + 无槽位双覆盖）。
    """
    names = _cfg().get("extra_name_list") or []
    assert len(names) >= 20
    missed = []
    for n in names:
        for ctx in (f"负责人：{n}", f"该文件由{n}签署", f"参会人员：{n}、其他"):
            if n in apply_redactions(ctx):
                missed.append((ctx, apply_redactions(ctx)))
    assert not missed, f"未脱敏：{missed}"


def test_extra_name_keeps_surrounding_words():
    """脱敏不吞掉相邻动词/介词。"""
    out = apply_redactions("李熔负责")
    assert out.endswith("负责"), out
    out = apply_redactions("由李熔牵头")
    assert out.startswith("由"), out


# ---------------------------------------------------------------------------
# 4. 护栏：槽位内的职务标签 / 通用词不被误脱
# ---------------------------------------------------------------------------

def test_slot_role_labels_kept():
    """槽位内的纯职务标签保持保留。"""
    for text in ("副组长：各条线副行长、总监", "成员：业务和技术骨干"):
        out = apply_redactions(text)
        for kw in ("副行长", "总监", "骨干"):
            if kw in text:
                assert kw in out, f"{text} -> {out}"


def test_gb2312_rare_chars_recall_in_slot():
    """GB2312 区间生僻字抽样 ≥50 例，**槽位内**召回率必须 100%（#60 §四 第 1 条）。

    对应方案 2（槽位强判别）：槽位语义本身即强证据，不依赖字池/字频。

    【更新 2026-11-10】方案 1（GB2312 二级字候选池）已随 D-6 落地
    （见整改方案 §十七），**无槽位上下文亦已达 100% 召回**，
    该口径由 `tests/test_issue60_d6_rare_char_pool.py` 覆盖；
    本测试仍只覆盖槽位口径（两文件互为补充，不重复断言）。
    """
    import random
    cfg = _cfg()
    pool = set(common_rules._get_name_pool_chars(cfg))
    sur = list(cfg.get("surname_pool") or [])
    cands = [chr(c) for c in range(0x4E00, 0x9FA6) if chr(c) not in pool]
    random.seed(7)
    sample = random.sample(cands, 50)
    names = [random.choice(sur) + c for c in sample]
    missed = [n for n in names if n in apply_redactions(f"负责人：{n}")]
    assert not missed, f"槽位内生僻字漏脱：{missed}"


def test_generic_words_not_misfired():
    """常见通用词不得因 D-5 被误脱（真实业务语料高频误伤样本）。"""
    samples = [
        "系统支撑能力较强", "占比逐年提升", "配套软件已完成部署",
        "实现全覆盖目标", "深刻吸取经验教训", "某城商行",
    ]
    for s in samples:
        out = apply_redactions(s)
        assert out == s, f"误伤：{s} -> {out}"


def test_slot_head_conjunction_not_misfired():
    """【批次二 G4 回归】槽位/角色条目以连词起首 → 不得判为人名。

    根因：「和」既是真实姓氏又是高频连词，D-5 槽位强判别与职务姓名通道
    仅凭「姓氏起点」放行，导致 "审核和发布" 被切成 "审核XXX"。
    """
    samples = [
        "质量管理补充流程审核和发布",
        "评审与发布安排另行通知",
        "编制及下发工作方案",
        "项目立项或者终止由领导小组决定",
    ]
    for s in samples:
        out = apply_redactions(s)
        assert out == s, f"误伤：{s} -> {out}"


def test_slot_real_names_still_masked_after_head_veto():
    """起首虚词否决不得影响真实人名脱敏。"""
    cases = {
        "审核：张三、李四": ("张三", "李四"),
        "负责人：刘兆缋": ("刘兆缋",),
        "组长：王组长": ("王组长",),
    }
    for s, names in cases.items():
        out = apply_redactions(s)
        for n in names:
            assert n not in out, f"{s} -> {out}（{n} 未脱敏）"


def test_role_conjunction_regex_requires_name_chars():
    """【批次二 G7 回归】连词正则的姓氏分支必须成组。

    根因：surname_alt 用 `|` 拼接但未加 `(?:...)`，导致
    `{name_class}{1,3}` 只挂在**最后一个**姓氏分支上，
    其余姓氏可「零名用字」单字命中 → "位及时" 被切成 "位XXX及XXX"。
    """
    pattern = common_rules._get_role_conjunction_re()
    assert pattern.groups == 3, f"连词正则分组数应为 3，实际 {pattern.groups}"
    text = "为保证监管单位及时掌握投产情况"
    out = apply_redactions(text)
    assert out == text, f"误伤：{text} -> {out}"


def test_role_conjunction_real_names_still_masked():
    """连词两侧的真实人名仍须脱敏。"""
    for s in ("王力和张明", "李四和王五"):
        out = apply_redactions(s)
        assert out.count("XXX") == 2, f"{s} -> {out}"


def test_no_new_misfire_on_generic_corpus():
    """通用语料脱敏增量必须为 0（D-5 作用域限定槽位内）。"""
    corpus = [
        "本季度营业收入同比增长百分之十二点三",
        "会议决定成立安全生产领导小组",
        "请各科室于月底前报送统计数据",
        "该批材料已通过审核并归档保存",
    ]
    for s in corpus:
        assert apply_redactions(s) == s, f"误伤：{s}"


# ---------------------------------------------------------------------------
# 5. 幂等性
# ---------------------------------------------------------------------------

def test_idempotent():
    for s in ("负责人：李熔、王谧", "组长：刘兆缋", "李熔负责"):
        once = apply_redactions(s)
        assert apply_redactions(once) == once, s
