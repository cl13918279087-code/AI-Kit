# 文档脱敏说明文档

> 本文档说明 `doc-redact` 技能包对 Word 文档的脱敏处理能力、规则逻辑、技术实现及验证方法。

---

## 一、支持格式与处理范围

| 格式 | 脚本 | 处理范围 |
|---|---|---|
| Word `.docx` / `.doc` | `scripts/redact_word.py` | 正文、页眉页脚、文本框、表格、批注、文档属性 |
| PPT `.pptx` / `.ppt` | `scripts/redact_ppt.py` | 文本框、表格、形状、演讲者备注 |
| Excel `.xlsx` / `.xls` | `scripts/redact_excel.py` | 单元格内容、批注、页眉页脚 |
| PDF `.pdf` | `scripts/redact_pdf.py` | 文本型 PDF 坐标遮盖；扫描版 OCR + 图像遮盖 |
| 图片 `.png/.jpg/.jpeg/.bmp` | `scripts/redact_image.py` | OCR 文字定位 → 马赛克/模糊遮盖 |

> `.doc` 文件先通过 macOS `textutil` 转换为 `.docx` 再处理。

---

## 二、脱敏常量对照表

| 敏感类别 | 替换为 | 示例 |
|---|---|---|
| 银行名称 | `XX银行` | 郑州银行 → XX银行 |
| 人员姓名 | `XXX` | 张三 / 欧阳六郎 → XXX |
| 日期信息 | `YYYY/MM/DD` | 2017年4月11日 → YYYY/MM/DD |
| 日期+时间范围 | `YYYY/MM/DD HH:MM-HH:MM` | 2017年4月11日15:30-16:30 → YYYY/MM/DD 15:30-16:30 |
| 身份证号 | `XXXXXXXXXXXXXXXXXX` | 110101199003072345 → XXXXXXXXXXXXXXXXXX |
| 手机号码 | `XXXXXXXXXXX` | 13812345678 → XXXXXXXXXXX |
| 固定电话 | `0XX-XXXXXXXX` | 010-88888888 → 0XX-XXXXXXXX |
| 电子邮箱 | `XXXXX@XXXXX` | zhangsan@example.com → XXXXX@XXXXX |
| 详细地址 | `XX省XX市XX区XXXX` | 北京市朝阳区建国门外大街1号 → XX省XX市XX区XXXX |
| 银行卡号 | `XXXXXXXXXXXXXXXX` | 6222021234567890 → XXXXXXXXXXXXXXXX |
| 银行 Logo（嵌入图片） | **纯黑图替换** | 检测文件名含 bank/logo/icon 等关键词的图片 → 纯黑填充 |

---

## 三、执行顺序（链式替换）

```
① 电子邮箱
② 详细地址
③ 身份证号
④ 银行卡号
⑤ 日期信息（含时间范围）
⑥ 手机号码 / 固定电话
⑦ 银行名称
⑧ 方案（独立词）
⑨ 银行 Logo（图片替换）
⑩ 人员姓名
```

> **为什么要按此顺序？**
> - 邮箱/地址较长，先处理不会误伤其他规则。
> - 银行卡号/身份证号位数长，先处理可避免日期正则误匹配数字串。
> - 日期含时间范围时，通过后向引用保留时间部分（如 `YYYY/MM/DD 15:30-16:30`）。
> - 人员姓名2-4字最短，放在最后防止误伤其他已脱敏内容。

---

## 四、核心正则规则

### 4.1 日期（含时间范围）

```python
# 规则1：日期+时间范围（无空格，如 2017年4月11日15:30-16:30）
r'(\d{4}年\d{1,2}月\d{1,2}日)(?!\s)(\d{1,2}:\d{2}[-–]\d{1,2}:\d{2})'
# 替换为：YYYY/MM/DD \2（时间部分通过后向引用保留）

# 规则2：日期+时间范围（有空格，如 2017年4月11日 15:30-16:30）
r'(\d{4}年\d{1,2}月\d{1,2}日)(\s+\d{1,2}:\d{2}[-–]\d{1,2}:\d{2})'
# 替换为：YYYY/MM/DD\2

# 规则3：普通日期（无时间）
r'\d{4}年\d{1,2}月\d{1,2}日'
r'\d{4}-\d{1,2}-\d{1,2}'
r'\d{4}/\d{1,2}/\d{1,2}'
r'\d{4}\.\d{1,2}\.\d{1,2}'
# 替换为：YYYY/MM/DD
```

> 规则1使用负向前瞻 `(?!\s)`，防止匹配已转换的 `YYYY/MM/DD 15:30-16:30` 造成二次替换。

### 4.2 银行名称（上下文感知）

```python
# 匹配前缀：各银行简称 + "银行/农商银行/信用社/农信社/合作银行/人民银行"
# 替换为：XX银行
# 排除：前面紧邻 CJK 字符的匹配（防止"XX银行方案"被误脱为"XX银XXXX"）
```

### 4.3 人员姓名（双模式 + 四重验证）

#### 正则模式

```python
# STRICT（保守）：姓 + 常用名用字1~2字
NAME_PATTERN_STRICT = re.compile(
    f'(?:(?:{复姓库})[{名用字库}]{{1,2}}|[{单姓库}][{名用字库}]{{1,2}})'
)

# LOOSE（宽松）：姓 + 任意CJK字符1~3字（兜底罕见姓名）
NAME_PATTERN_LOOSE = re.compile(
    f'(?:(?:{复姓库})[\u4e00-\u9fa5]{{2,3}}|[{单姓库}][\u4e00-\u9fa5]{{1,3}})'
)
```

#### 四重验证（满足任一即拒绝）

| 验证维度 | 规则 | 示例 |
|---|---|---|
| 完整词保护 | 匹配词本身在 `{'责任人', '马上', '负责人', '主办人'}` 中 | `马上` 不替换 |
| 前置CJK组合词 | 匹配词前紧邻CJK字符，且 CJK+full 是已知组合词 | `责任人` 中 `责`+`人` 不替换 |
| 后缀黑名单 | 匹配词后紧跟（startswith）职务/机构词 | `韩慧丽负责人` 中 `韩慧丽` 不替换 |
| 名用字验证 | LOOSE 模式：姓氏后所有字符须≥50%在名用字库中 | `州银行` 拒绝（0/3 < ceil(2)） |

#### 执行策略

1. **STRICT 先匹配**（高可信度，命中即替换）
2. **LOOSE 兜底**（防止罕见姓名遗漏，排除已被 STRICT 覆盖的区域）

---

## 五、技术实现：XML 碎片化问题处理

### 5.1 问题背景

Word 文档中同一个段落内的文字可能被拆散到多个 `<w:r><w:t>` XML 元素中，每个 `<w:t>` 只含 1 个或几个字符：

```
<w:p>
  <w:r><w:t>2017年</w:t></w:r>
  <w:r><w:t>4</w:t></w:r>
  <w:r><w:t>月</w:t></w:r>
  <w:r><w:t>11</w:t></w:r>
  <w:r><w:t>日</w:t></w:r>
  <w:r><w:t>15:30-16:30</w:t></w:r>
</w:p>
```

若逐 `<w:t>` 独立正则，`2017年4月11日15:30-16:30`（跨15个碎片）永远无法被完整匹配。

### 5.2 解决方案：`paragraph-level` 拼接算法

```
Step 1: 提取段落中所有 <w:t> 文本，拼接为 orig_concat
Step 2: 在 orig_concat 上执行全量正则脱敏
Step 3: 建立 orig_pos → redacted_pos 前向映射
Step 4: 按原始 <w:t> 长度将 redacted_concat 拆分回各段
Step 5: 从后向前逐一替换各 <w:t> 的文本内容
```

#### 前向映射函数

```python
def orig_to_redacted_pos(p):
    """对于原始位置 p，返回对应的脱敏后字符串索引"""
    total = 0
    prev_end = 0
    for s, e, r in replacements:   # (orig_start, orig_end, replacement_str)
        if p < s:
            return total + (p - prev_end)
        elif s <= p < e:
            return total + (p - s)  # p 在 replacement 区间内
        else:  # p >= e
            total += (s - prev_end) + len(r)
            prev_end = e
    return total + (p - prev_end)
```

> 此算法解决了旧版"占位符标记法"的死循环问题，无需在 redacted 文本中插入临时标记。

### 5.3 python-docx 保存覆盖问题

python-docx 的 `doc.save()` 会重新生成整个 docx 包，会覆盖 `_process_xml_deep_v2` 中精心处理的 XML。

**解决方案**：直接操作 ZIP，不走 python-docx：

```python
# Step 1: 将处理好的 patched 字符串写入临时目录
doc_xml_path.write_text(patched, 'utf-8')
# Step 2: 重新打包为 docx
with zipfile.ZipFile(str(tmp_docx), 'w', ZIP_DEFLATED) as zf:
    for fp in extract_dir.rglob('*'):
        if fp.is_file():
            zf.write(fp, str(fp.relative_to(extract_dir)))
# Step 3: 直接将 Step1 的 document.xml 写入最终 docx（绕过 doc.save）
with zipfile.ZipFile(str(tmp_docx), 'r') as zin:
    with zipfile.ZipFile(output_path, 'w', ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == 'word/document.xml':
                zout.writestr(item, patched.encode('utf-8'))  # 用我们处理好的版本
            else:
                zout.writestr(item, zin.read(item.filename))
```

---

## 六、使用方式

### 命令行

```bash
python3 scripts/redact_word.py <输入文件路径> [输出文件路径]
```

- 不指定输出路径时，自动生成为 `{原文件名_脱敏}.docx`
- 文件名中的银行名称/日期/手机号同步脱敏

### 示例

```bash
python3 scripts/redact_word.py "/Users/clzxr/WorkBuddy/Claw/工作目录/郑州银行新一代信息系统建设项目文档_差异分析启动会.docx"
# 输出：/Users/clzxr/.../郑州银行新一代信息系统建设项目文档_差异分析启动会_脱敏.docx
```

---

## 七、验证清单

执行脱敏后，人工核查以下要点：

- [ ] 日期格式统一为 `YYYY/MM/DD`，时间范围部分（`15:30-16:30`）保留完整
- [ ] 日期+时间范围格式（如 `2017年4月11日15:30-16:30`）正确转换为 `YYYY/MM/DD 15:30-16:30`
- [ ] 人员姓名（如 `孙海刚`、`欧阳六郎`）已替换为 `XXX`，无漏网
- [ ] 银行名称（`郑州银行`）已替换为 `XX银行`，不含误脱（如 `XX银行方案` 保留为 `XX银行XXXX`）
- [ ] 身份证号、手机号位数完整
- [ ] 文件名中无原始银行名/日期泄露

---

## 八、局限性说明

| 限制 | 说明 |
|---|---|
| 不可逆 | 所有替换为单向操作，不生成解密映射表 |
| 银行 Logo 漏检风险 | 依赖文件名启发式规则（`bank/logo/icon/brand`），纯色或无名图片可能被遗漏 |
| 罕见姓名 | LOOSE 模式兜底，但仍可能遗漏非常见名用字组合 |
| 手写体/扫描件 | 需走 PDF/图片 OCR 流程，不在 `redact_word.py` 处理范围内 |
