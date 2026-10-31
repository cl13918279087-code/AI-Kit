#!/usr/bin/env python3
"""
test_issue52_nc_d_level.py - Issue #52 回归测试（N-C 补漏通道）

覆盖本轮落地的四块能力：

  1. **D 级补漏通道**（稀有字真名）：在「已确认是姓名列」的上下文里，
     对规则层必然漏掉的真名（名字位字不在 D16 库）做补位遮盖；
     同时对业务词零误伤。
  2. **半脱敏修复**：A 级只命中「姓 + 前 1 个名字位」时（如「吴佳翌」）
     不得产出「XXX翌」这种残留。
  3. **括注型人角色表头**：「数据提供方（对接人）」这类表头应整列进 N-C，
     而「（3）」「（含）」这类非人角色括注不应命中。
  4. **PPT 网格列展开 / 全员姓名列 / 行标签人角色行**：
     - `gridSpan` / `hMerge` 造成的列错位不得漏脱真名；
     - 表头无人角色词、但取值全是姓名的列应被识别；
     - 行向结构（首列是人角色标签）里的姓名应被遮盖。

运行方式：
    python tests/test_issue52_nc_d_level.py
    # 或 pytest tests/test_issue52_nc_d_level.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))

import name_region_detector as nrd  # noqa: E402
import table_role_detector as trd  # noqa: E402


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _tc(text, attrs=""):
    """构造一个最小可用的 <a:tc> 单元格。"""
    return (f'<a:tc{attrs}><a:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p>'
            f'</a:txBody></a:tc>')


def _tr(cells, attrs=""):
    return f'<a:tr{attrs}>' + "".join(cells) + '</a:tr>'


def _tbl(grid, rows):
    cols = "".join('<a:gridCol w="1000000"/>' for _ in range(grid))
    return ('<a:tbl><a:tblGrid>' + cols + '</a:tblGrid>' + "".join(rows)
            + '</a:tbl>')


def _cells_texts(tbl_xml):
    """取出表格里每行每个单元格的文本。"""
    out = []
    for r in trd._PPT_TR_RE.finditer(tbl_xml):
        out.append([trd._extract_cell_text(c.group(0))
                    for c in trd._PPT_TC_RE.finditer(r.group(0))])
    return out


# ---------------------------------------------------------------------------
# 1. D 级补漏通道
# ---------------------------------------------------------------------------

# 名字位字不在 D16 名字池里的真实人名（规则层必然漏脱）
D_RARE_NAMES = ["陈鲲", "吴县波", "成新华", "官民", "陈沈麟",
                 "桂宾", "叶助茂", "候睿", "华盛"]

# 同列上下文里不得被吃掉的业务词
D_BIZ_WORDS = ["互联网平台", "科技运营", "客户体验", "待确认", "自测",
               "总行网金", "汽融", "各业务组", "当天值班人员",
               "业务支持组人员", "渠道拓展", "白名单"]


def test_d_level_covers_rare_names():
    """D 级：同列有强证据时，稀有字真名应被补位遮盖"""
    nrd.set_nc_doc_gram(nrd.build_nc_doc_gram(
        ["姓名列", "陈鲲", "吴县波", "成新华", "官民", "陈沈麟",
         "桂宾", "叶助茂", "候睿", "华盛", "李虎", "王芳"]))
    try:
        # 同列 A/C 级确认率 ≥ 0.5（D2 闸门）
        for name in D_RARE_NAMES:
            out = nrd.name_cell_spans(name, allow_extra_surnames=True,
                                      d_column_confirm_ratio=0.8)
            assert out, f"D 级应覆盖稀有字真名「{name}」"
            s, e = out[0]
            assert name[s:e] == name, \
                f"「{name}」应整名遮盖，实得「{name[s:e]}」"
    finally:
        nrd.clear_nc_doc_gram()
    print("✅ D 级覆盖稀有字真名测试通过")


def test_d_level_no_biz_harm():
    """D 级：业务词零误伤（同一列上下文下也不得被遮）"""
    nrd.set_nc_doc_gram(nrd.build_nc_doc_gram(
        ["姓名列", "陈鲲", "李虎", "王芳"] + D_BIZ_WORDS))
    try:
        for word in D_BIZ_WORDS:
            out = nrd.name_cell_spans(word, allow_extra_surnames=True,
                                      d_column_confirm_ratio=0.8)
            assert not out, f"业务词「{word}」不应被 D 级遮盖，实得 {out}"
    finally:
        nrd.clear_nc_doc_gram()
    print("✅ D 级业务词零误伤测试通过")


def test_d_level_requires_doc_gram():
    """未注入文档级词频时，D 级整体不启用（保守）"""
    nrd.clear_nc_doc_gram()
    out = nrd.name_cell_spans("陈鲲", allow_extra_surnames=True,
                              d_column_confirm_ratio=0.9)
    assert not out, "无文档级词频上下文时 D 级不应启用"
    print("✅ D 级保守闸门测试通过")


# ---------------------------------------------------------------------------
# 2. 半脱敏修复
# ---------------------------------------------------------------------------

def test_half_mask_fix():
    """A 级只命中前 2 字时（吴佳翌 → 吴佳），应补足为整名遮盖"""
    nrd.set_nc_doc_gram(nrd.build_nc_doc_gram(
        ["姓名列", "吴佳翌", "董美岑", "洪云霓", "李虎"]))
    try:
        for name in ("吴佳翌", "董美岑", "洪云霓"):
            out = nrd.name_cell_spans(name, allow_extra_surnames=True,
                                      d_column_confirm_ratio=0.8)
            assert out, f"「{name}」应被遮盖"
            s, e = out[0]
            masked = name[:s] + "XXX" + name[e:]
            assert masked == "XXX", \
                f"「{name}」应整名遮盖（不得出现半脱敏），实得「{masked}」"
    finally:
        nrd.clear_nc_doc_gram()
    print("✅ 半脱敏修复测试通过")


# ---------------------------------------------------------------------------
# 3. 括注型人角色表头
# ---------------------------------------------------------------------------

def test_paren_person_header():
    """「数据提供方（对接人）」应是人角色表头；「（3）」不是"""
    for h in ("数据提供方（对接人）", "捞数需求方（对接人）"):
        assert nrd.is_person_header(h), f"「{h}」应判定为人角色表头"
    for h in ("序号（3）", "备注（含）", "金额（元）"):
        assert not nrd.is_person_header(h), f"「{h}」不应判定为人角色表头"
    print("✅ 括注型人角色表头测试通过")


# ---------------------------------------------------------------------------
# 4. PPT 适配层：网格列展开 / 全员姓名列 / 行向通道
# ---------------------------------------------------------------------------

def test_ppt_gridspan_columns():
    """gridSpan/hMerge 造成的列错位不得漏脱真名

    结构对齐真实 PPT slide40：
        表头行  <a:tc>捞数阶段</a:tc> <a:tc>投产阶段</a:tc>
                <a:tc>数据提供方（对接人）</a:tc>
                <a:tc gridSpan="4">捞数需求方（对接人）</a:tc>
                <a:tc hMerge="1"/>×3
        子表头  <a:tc vMerge="1"/>×3 + 运营/风险/市场/技术
        数据行  … 谈叙 / 朱丽芸 / 邓君 / 陈瑜 / 胡璐 / 董欣 …
    """
    rows = [
        _tr([_tc("捞数阶段", ' rowSpan="2"'), _tc("投产阶段", ' rowSpan="2"'),
             _tc("数据提供方（对接人）", ' rowSpan="2"'),
             _tc("捞数需求方（对接人）", ' gridSpan="4"'),
             _tc("", ' hMerge="1"'), _tc("", ' hMerge="1"'),
             _tc("", ' hMerge="1"')]),
        _tr([_tc("", ' vMerge="1"'), _tc("", ' vMerge="1"'),
             _tc("", ' vMerge="1"'),
             _tc("运营"), _tc("风险"), _tc("市场"), _tc("技术")]),
        _tr([_tc("数迁"), _tc("T-1日"), _tc("李虎"),
             _tc("自动化，CRS中提取", ' gridSpan="3"'),
             _tc("", ' hMerge="1"'), _tc("", ' hMerge="1"'), _tc("--")]),
        _tr([_tc("批前关键案例"), _tc("T日"), _tc("戚亚亚"),
             _tc("--"), _tc("谈叙"), _tc("--"), _tc("--")]),
        _tr([_tc("首批后验证"), _tc("T+1日"), _tc("李虎"),
             _tc("陈瑜"), _tc("谈叙"), _tc("朱丽芸"), _tc("--")]),
        _tr([_tc("正式营业"), _tc("T+1日"), _tc("李虎"),
             _tc("胡璐"), _tc("董欣"), _tc("--"), _tc("--")]),
    ]
    tbl = _tbl(7, rows)

    model = trd._nc_ppt_table_model(tbl)
    assert model, "应能建立逻辑列模型"
    cols = model["cols"]
    # 合并表头下的 4 个子列都必须进入姓名列
    for c in (3, 4, 5, 6):
        assert c in cols, f"逻辑列 {c} 应被判定为姓名列，实得 {cols}"
    assert 2 in cols, "「数据提供方（对接人）」列应进入 N-C"

    new_xml, n = trd._apply_nc_name_columns_ppt(tbl)
    assert n > 0, "应有脱敏产出"
    text = "".join("".join(row) for row in _cells_texts(new_xml))
    for name in ("李虎", "戚亚亚", "谈叙", "朱丽芸", "陈瑜", "胡璐", "董欣"):
        assert name not in text, f"「{name}」应被遮盖（列错位漏脱）"
    assert "自动化，CRS中提取" in text, "业务文本不应被误遮"
    print("✅ PPT gridSpan 列展开测试通过")


def test_ppt_name_only_column():
    """表头无人角色词、但取值全是姓名的列应被识别（全员姓名列）"""
    rows = [
        _tr([_tc("报送单位"), _tc("报送内容"), _tc("业务所属"),
             _tc("报送前是否人工检核")]),
        _tr([_tc("人行"), _tc("个人结算账户信息"), _tc("陈柯"), _tc("是")]),
        _tr([_tc("外管局"), _tc("境外交易信息报送"), _tc("胡璐"), _tc("是")]),
        _tr([_tc("人行"), _tc("人行报送"), _tc("唐雨晨"), _tc("是")]),
        _tr([_tc("外管局"), _tc("监管集中报送平台"), _tc("董欣"), _tc("是")]),
    ]
    tbl = _tbl(4, rows)
    model = trd._nc_ppt_table_model(tbl)
    assert model, "应能建立逻辑列模型"
    assert 2 in model["cols"], \
        f"「业务所属」列应被识别为姓名列，实得 {model['cols']}"
    new_xml, n = trd._apply_nc_name_columns_ppt(tbl)
    assert n > 0, "应有脱敏产出"
    text = "".join("".join(row) for row in _cells_texts(new_xml))
    for name in ("陈柯", "胡璐", "唐雨晨", "董欣"):
        assert name not in text, f"「{name}」应被遮盖"
    assert "人行报送" in text, "业务文本不应被误遮"
    print("✅ PPT 全员姓名列测试通过")


def test_ppt_row_person_cells():
    """行向结构：首列是人角色标签的行，姓名应被遮盖"""
    rows = [
        _tr([_tc("信息要素"), _tc("填写要求"), _tc("示例1"), _tc("示例2")]),
        _tr([_tc("工单标题"), _tc("简述"), _tc("内部试营业"), _tc("生产监控")]),
        _tr([_tc("捞数执行人"), _tc("数据提供方（对接人）"),
             _tc("戚亚亚"), _tc("李虎")]),
    ]
    tbl = _tbl(4, rows)
    new_xml, n = trd._apply_nc_name_columns_ppt(tbl)
    assert n >= 2, f"应有 2 处脱敏，实得 {n}"
    text = "".join("".join(row) for row in _cells_texts(new_xml))
    assert "戚亚亚" not in text and "李虎" not in text, "行内姓名应被遮盖"
    assert "数据提供方（对接人）" in text, "角色标签不应被吃掉"
    print("✅ PPT 行标签人角色通道测试通过")


def test_ppt_org_label_not_person_row():
    """组织标签（「总行业务实施组」）不得触发行向通道"""
    rows = [
        _tr([_tc("事项"), _tc("说明"), _tc("负责人"), _tc("时限")]),
        _tr([_tc("总行业务实施组"), _tc("总行网金、汽融"), _tc("--"),
             _tc("38")]),
    ]
    tbl = _tbl(4, rows)
    cells = trd._ppt_row_cells(trd._PPT_TR_RE.finditer(tbl).__next__().group(0))
    assert not trd._ppt_row_person_cells(cells), \
        "组织标签不应触发行向通道"
    print("✅ PPT 组织标签守卫测试通过")


# ---------------------------------------------------------------------------
# 5. 业务词零误伤（A/B 级前缀猜测防护）
# ---------------------------------------------------------------------------

def test_biz_word_prefix_guard():
    """A 级最长前缀是业务词时整段不作姓名；B 级不做中文前缀猜测"""
    # 「白名单用户」的最长前缀「白名单」是业务词 → 不得遮「白名」
    assert nrd._nc_name_len("白名单用户", allow_extra_surnames=True) == 0, \
        "「白名单用户」不应被当作姓名"
    # 「渠道拓展」不得被拆成姓名「渠道」
    assert nrd._nc_name_len("渠道拓展", allow_extra_surnames=True,
                            relaxed=True) == 0, \
        "「渠道拓展」不应被当作姓名"
    for word in D_BIZ_WORDS:
        assert nrd._nc_name_len(word, allow_extra_surnames=True,
                                relaxed=True) == 0, \
            f"业务词「{word}」不应被当作姓名"
    print("✅ 业务词前缀防护测试通过")


if __name__ == "__main__":
    print("=" * 64)
    print("Issue #52 回归测试（N-C 补漏通道）")
    print("=" * 64)

    test_d_level_covers_rare_names()
    test_d_level_no_biz_harm()
    test_d_level_requires_doc_gram()
    test_half_mask_fix()
    test_paren_person_header()
    test_ppt_gridspan_columns()
    test_ppt_name_only_column()
    test_ppt_row_person_cells()
    test_ppt_org_label_not_person_row()
    test_biz_word_prefix_guard()

    print("=" * 64)
    print("全部测试通过 ✅")
    print("=" * 64)
