#!/usr/bin/env python3
"""
test_nc_name_region.py - N-C 区域检测层回归测试（Issue #50）

覆盖三块：

  1. N-C 语义层单测（scripts/name_region_detector.py）
     - T1 表格姓名列判定（表头角色词 + 值列形态一致性）
     - T2 正文名单段区域检测（锚点词 + 同构性 + 分隔符结构）
     - T3 签名/落款区检测
     - 区域上下文补漏脱敏 redact_name_cell（含复姓支持）
     - 锚点词表配置治理（config.json 的 name_anchor_words 三类齐备）

  2. Word 适配器接入测试（scripts/redact_word.py）
     - Word 表格「姓名」列触发列级脱敏（修 #50 §4.6 缺口①）
     - 跨 run 拆分的姓名不再漏脱（修 #50 §4.6 缺口②）
     - 表头行数自适应（1 行表头 + 数据行不被吞）
     - 多行表头聚合（真表头不被数据行覆盖）
     - 非姓名列零误伤（业务词表格不被误吃）

  3. 不退化守卫
     - 通用语料不因 N-C 出现新增误伤

运行方式：
    cd <repo>
    python tests/test_nc_name_region.py
    # 或 pytest tests/test_nc_name_region.py -v
"""

import os
import re
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import name_region_detector as nrd  # noqa: E402

W_T = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)


# ---------------------------------------------------------------------------
# 1. N-C 语义层单测
# ---------------------------------------------------------------------------

def test_anchor_words_config():
    """锚点词表是 config.json 的单一可配置项，三类齐备（§4.7）"""
    import json
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "scripts", "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    assert "name_anchor_words" in cfg, "config.json 缺少 name_anchor_words"
    anchors = cfg["name_anchor_words"]
    for key in ("role", "list", "sign"):
        assert key in anchors, f"name_anchor_words 缺少 {key} 类"
        assert anchors[key], f"name_anchor_words.{key} 不应为空"

    # 锚点词表不应按行业分表（单一扁平结构）
    assert isinstance(anchors, dict) and set(anchors) == {"role", "list", "sign"}

    # 「姓名/成员/组员/联系人」必须存在 —— 这是 Word 表格误伤放大器的止血词
    role_set = set(anchors["role"]) | set(anchors["list"])
    for kw in ("姓名", "成员", "组员", "联系人"):
        assert kw in role_set, f"锚点词表缺少「{kw}」"

    # 运行时读取的词表应与配置一致
    nrd.reset_anchor_cache()
    runtime = nrd.get_anchor_words()
    assert set(runtime["role"]) == set(anchors["role"])
    print("✅ 锚点词表配置测试通过")


def test_is_name_shaped():
    """姓名形态判定：姓 + 1~3 个名用字，支持复姓"""
    assert nrd.is_name_shaped("王根利"), "三字名应识别"
    assert nrd.is_name_shaped("李思远"), "三字名应识别"
    assert nrd.is_name_shaped("张伟"), "二字名应识别"
    assert nrd.is_name_shaped("欧阳娜娜"), "复姓四字名应识别"
    assert not nrd.is_name_shaped("需求梳理"), "普通词组不应识别为姓名"
    assert not nrd.is_name_shaped(""), "空串不应识别"
    print("✅ is_name_shaped 测试通过")


def test_detect_name_columns_t1():
    """T1：表格姓名列判定"""
    # 标准：姓名列（值列形态一致）
    cols = nrd.detect_name_columns(
        ["姓名", "部门", "职责"],
        [["王根利", "研发", "开发"], ["李思远", "研发", "测试"]])
    assert cols == [0], f"应仅判定第 0 列为姓名列，实得 {cols}"

    # 非姓名列不得被判定（业务词表格零误伤）
    cols = nrd.detect_name_columns(
        ["需求包括", "开发", "单元测试"],
        [["渠道拓展", "白名单", "定期"], ["活期", "定期", "白名单"]])
    assert cols == [], f"业务词表格不应判定出姓名列，实得 {cols}"

    # 表头角色词「成员」也应触发
    cols = nrd.detect_name_columns(
        ["成员", "职务"],
        [["张伟", "组长"], ["李娜", "组员"]])
    assert cols == [0], f"「成员」表头应判定为姓名列，实得 {cols}"

    # 稀疏列（有效值 <2）仅凭表头词判定
    cols = nrd.detect_name_columns(["姓名", "备注"], [["王根利", "x"]])
    assert cols == [0], f"稀疏姓名列不应漏判，实得 {cols}"

    # 多行表头：逐行匹配（§4.6 表头跨行）
    cols = nrd.detect_name_columns(
        ["基本信息\n姓名", "部门"],
        [["王根利", "研发"], ["李思远", "研发"]])
    assert cols == [0], f"多行表头应识别出姓名列，实得 {cols}"

    print("✅ detect_name_columns（T1）测试通过")


def test_detect_text_regions_t2():
    """T2：正文名单段区域检测"""
    regions = nrd.detect_name_regions("项目组成员如下：王根利、李思远、周慧敏负责需求梳理")
    assert regions, "名单段应被检出"
    texts = [t for r in regions for t in r.texts]
    for name in ("王根利", "李思远", "周慧敏"):
        assert name in texts, f"名单段应包含「{name}」，实得 {texts}"

    # 粘连场景：分隔符后接动词，首项也不能丢
    regions = nrd.detect_name_regions("核心组：蔡新发、张瑜、李明")
    texts = [t for r in regions for t in r.texts]
    for name in ("蔡新发", "张瑜", "李明"):
        assert name in texts, f"粘连名单段应包含「{name}」，实得 {texts}"

    # 无名单结构的普通文本不应误报为区域
    regions = nrd.detect_name_regions("本项目旨在提升系统稳定性与性能表现")
    assert not regions, f"普通文本不应误报区域，实得 {regions}"
    print("✅ detect_name_regions（T2）测试通过")


def test_detect_signature_regions_t3():
    """T3：签名/落款区检测"""
    text = ("特此通知\n\n\n"
            "签字：王海涛\n"
            "日期：2026-01-01")
    regions = nrd.detect_signature_regions(text)
    assert regions, "签名区应被检出"
    print("✅ detect_signature_regions（T3）测试通过")


def test_redact_name_cell():
    """区域/列上下文的补漏脱敏"""
    # 高危姓 + 单字名：全局规则漏掉 → 列上下文应救回
    out = nrd.redact_name_cell("樊霖")
    assert out != "樊霖", f"「樊霖」应被补漏脱敏，实得「{out}」"

    # 已是脱敏产物的保持不变
    assert nrd.redact_name_cell("XXX") == "XXX", "已脱敏项不应被改写"

    # 业务词不得被吃掉
    for word in ("渠道拓展", "白名单", "活期"):
        assert nrd.redact_name_cell(word) == word, f"业务词「{word}」不应被脱敏"

    # 复姓
    out = nrd.redact_name_cell("欧阳娜娜")
    assert out != "欧阳娜娜", "复姓姓名应被脱敏"
    print("✅ redact_name_cell 测试通过")


# ---------------------------------------------------------------------------
# 2. Word 适配器接入测试
# ---------------------------------------------------------------------------

def _docx_texts(path):
    """读取 docx 中所有 <w:t> 文本节点内容"""
    z = zipfile.ZipFile(path)
    out = []
    for n in z.namelist():
        if n.startswith("word/") and n.endswith(".xml"):
            x = z.read(n).decode("utf-8", errors="ignore")
            out.extend(W_T.findall(x))
    return out


def _build_docx(build_fn, path):
    import docx
    d = docx.Document()
    build_fn(d)
    d.save(path)
    return path


def _redact_docx(src):
    import redact_word as rw
    out = src + ".redacted.docx"
    rw.redact_docx(src, out)
    return out


def test_word_table_name_column():
    """Word 表格「姓名」列应触发列级脱敏（§4.6 缺口①）"""
    def build(d):
        t = d.add_table(rows=3, cols=3)
        t.rows[0].cells[0].text = "姓名"
        t.rows[0].cells[1].text = "部门"
        t.rows[0].cells[2].text = "职责"
        t.rows[1].cells[0].text = "樊霖"
        t.rows[1].cells[1].text = "研发"
        t.rows[1].cells[2].text = "开发"
        t.rows[2].cells[0].text = "李思远"
        t.rows[2].cells[1].text = "研发"
        t.rows[2].cells[2].text = "测试"

    tmp = tempfile.mkdtemp(prefix="nc_test_")
    src = _build_docx(build, os.path.join(tmp, "a.docx"))
    texts = _docx_texts(_redact_docx(src))
    joined = "".join(texts)
    assert "樊霖" not in joined, "「樊霖」应被脱敏"
    assert "李思远" not in joined, "「李思远」应被脱敏"
    # 非姓名列不得被改
    assert "研发" in joined and "开发" in joined and "测试" in joined, \
        "非姓名列内容不应被改动"
    print("✅ Word 表格姓名列接入测试通过")


def test_word_table_cross_run_name():
    """跨 run 拆分的姓名不应漏脱（§4.6 缺口②）"""
    import docx
    from docx.oxml.ns import qn

    def build(d):
        p = d.add_paragraph()
        run = p.add_run("樊")
        run2 = p.add_run("霖")
        # 再加一个 run 保证确实被拆分
        run3 = p.add_run("负责需求梳理")

    tmp = tempfile.mkdtemp(prefix="nc_test_")
    src = _build_docx(build, os.path.join(tmp, "b.docx"))
    # 确认确实被拆成多个 run
    z = zipfile.ZipFile(src)
    xml = z.read("word/document.xml").decode()
    assert len(W_T.findall(xml)) >= 2, "前置条件：姓名应被拆到多个 run"
    joined = "".join(_docx_texts(_redact_docx(src)))
    assert "樊霖" not in joined, "跨 run 的「樊霖」不应漏脱"
    print("✅ Word 跨 run 姓名测试通过")


def test_word_table_header_row_adaptive():
    """1 行表头 + 数据行：首个数据行不得被当作表头而漏脱"""
    def build(d):
        t = d.add_table(rows=4, cols=2)
        t.rows[0].cells[0].text = "姓名"
        t.rows[0].cells[1].text = "部门"
        t.rows[1].cells[0].text = "樊霖"
        t.rows[1].cells[1].text = "研发"
        t.rows[2].cells[0].text = "李思远"
        t.rows[2].cells[1].text = "研发"
        t.rows[3].cells[0].text = "周慧敏"
        t.rows[3].cells[1].text = "测试"

    tmp = tempfile.mkdtemp(prefix="nc_test_")
    src = _build_docx(build, os.path.join(tmp, "c.docx"))
    joined = "".join(_docx_texts(_redact_docx(src)))
    for name in ("樊霖", "李思远", "周慧敏"):
        assert name not in joined, f"「{name}」应被脱敏（表头行数自适应）"
    print("✅ Word 表头行数自适应测试通过")


def test_word_table_multi_row_header():
    """多行表头：真表头不得被后续数据行覆盖"""
    def build(d):
        t = d.add_table(rows=3, cols=2)
        t.rows[0].cells[0].text = "员工信息"
        t.rows[0].cells[1].text = "所属部门"
        t.rows[1].cells[0].text = "姓名"
        t.rows[1].cells[1].text = "岗位"
        t.rows[2].cells[0].text = "樊霖"
        t.rows[2].cells[1].text = "研发"

    tmp = tempfile.mkdtemp(prefix="nc_test_")
    src = _build_docx(build, os.path.join(tmp, "d.docx"))
    joined = "".join(_docx_texts(_redact_docx(src)))
    assert "樊霖" not in joined, "多行表头下的「樊霖」应被脱敏"
    assert "员工信息" in joined and "所属部门" in joined, "表头文字不应被改动"
    print("✅ Word 多行表头测试通过")


def test_word_table_non_name_column_no_fp():
    """无姓名列的业务词表格：零误伤"""
    def build(d):
        t = d.add_table(rows=3, cols=3)
        t.rows[0].cells[0].text = "需求包括"
        t.rows[0].cells[1].text = "开发"
        t.rows[0].cells[2].text = "单元测试"
        t.rows[1].cells[0].text = "渠道拓展"
        t.rows[1].cells[1].text = "白名单"
        t.rows[1].cells[2].text = "定期"
        t.rows[2].cells[0].text = "活期"
        t.rows[2].cells[1].text = "定期"
        t.rows[2].cells[2].text = "白名单"

    tmp = tempfile.mkdtemp(prefix="nc_test_")
    src = _build_docx(build, os.path.join(tmp, "e.docx"))
    joined = "".join(_docx_texts(_redact_docx(src)))
    for word in ("渠道拓展", "白名单", "定期", "活期"):
        assert word in joined, f"业务词「{word}」不应被误吃"
    print("✅ Word 非姓名列零误伤测试通过")


# ---------------------------------------------------------------------------
# 3. 不退化守卫
# ---------------------------------------------------------------------------

def test_no_regression_generic_corpus():
    """通用语料不因 N-C 出现新增误伤（§4.8 退出门槛：误伤 ≤5%）"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
    from test_name_false_positive import FALSE_POSITIVE_CORPUS
    lines = [c for c in FALSE_POSITIVE_CORPUS if c and c.strip()]
    if not lines:
        print("⏭ 通用语料为空，跳过")
        return

    hit = 0
    for ln in lines:
        regions = nrd.detect_name_regions(ln)
        if regions:
            hit += 1
    ratio = hit / len(lines)
    assert ratio <= 0.05, f"通用语料误判为区域的比例 {ratio:.1%} 超过 5% 门槛"
    print(f"✅ 通用语料不退化测试通过（{hit}/{len(lines)} = {ratio:.1%}）")


if __name__ == "__main__":
    print("=" * 64)
    print("N-C 区域检测层回归测试（Issue #50）")
    print("=" * 64)

    test_anchor_words_config()
    test_is_name_shaped()
    test_detect_name_columns_t1()
    test_detect_text_regions_t2()
    test_detect_signature_regions_t3()
    test_redact_name_cell()

    test_word_table_name_column()
    test_word_table_cross_run_name()
    test_word_table_header_row_adaptive()
    test_word_table_multi_row_header()
    test_word_table_non_name_column_no_fp()

    test_no_regression_generic_corpus()

    print("=" * 64)
    print("全部测试通过 ✅")
    print("=" * 64)
