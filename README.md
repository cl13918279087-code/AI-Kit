# AI-Kit

AI-Kit for Project Managers — 文档脱敏技能包

LLM 增强的 Word 文档脱敏工具包，支持 `.doc`（Word 6.0 OLE2）和 `.docx`（OOXML）格式，原位保留全部格式（标题、目录、表格、页眉图片）。

## 目录结构

```
Claw/
├── doc-redact/              # doc-redact Skill（WorkBuddy）
│   ├── SKILL.md
│   ├── scripts/
│   │   ├── redact_word.py   # Word 文档脱敏入口
│   │   ├── redact_excel.py
│   │   ├── redact_pdf.py
│   │   ├── redact_ppt.py
│   │   └── redact_image.py
│   └── references/
├── 工作目录/
│   ├── redact_kit/          # 核心代码库
│   │   ├── pipeline.py          # Pipeline 主控
│   │   ├── editor_executor.py   # editor_sdk 执行层（含 extract_all_text）
│   │   ├── entity_detector.py   # 实体检测（LLM + regex）
│   │   ├── llm_client.py        # LLM 客户端
│   │   ├── manifest.py          # 脱敏清单管理
│   │   ├── ocr_processor.py     # 图片 OCR
│   │   ├── prompts.py           # LLM prompt 模板
│   │   └── config.json          # 配置文件
│   └── 文档脱敏技能包技术架构方案.md   # 架构文档
```

## 快速使用

```bash
cd 工作目录/redact_kit
python3 -m pipeline input.docx -o output_redacted.docx
```

## 关键经验（v3→v5 迭代总结）

| 问题 | 根因 | 解决方案 |
|------|------|---------|
| 表格内姓名漏脱敏 | `.doc` 表格 block type 为 `"text"`，文本在嵌套 `content[i]["t"]` | 见 `editor_executor.extract_all_text()` |
| editor_sdk 替换不落盘 | SDK 内部缓存，10MB+ 大文档状态同步问题 | python-docx 直接读写 OOXML |
| 银行名残留 | 分次替换策略不一致 | 统一规则集（长名优先替换） |

## 关键经验（v6→v12 迭代总结）

| 问题 | 根因 | 解决方案 |
|------|------|---------|
| v10 脱敏后格式全部丢失 | python-docx 保存时会规范化 run 结构 | 使用 `lxml + zipfile` 直接操作 XML，在 `word/document.xml` 上逐 `w:t` 节点替换 |
| `.doc` 文件 python-docx 报错 | WPS 创建的 `.doc` 实际为 `.docx` 格式（OLE2 壳） | 用 `file` 命令检测；python-docx 可正常读取 |
| 阿拉伯日期跨 XML runs 分割 | Word 将长文本拆到多个 `<w:r>` 中 | 用 `body.iter(f"{W}t")` 全局迭代，跨 run 拼接后替换 |
| "股份有限公司"单独出现 | 表格中 `XX银行` 前缀已在上一轮替换消失，只剩后缀 | 追加 `股份有限公司` → `XXXX` 单独规则 |
| rename 发生在 zip 回写前 | 脚本逻辑错误导致写入不存在的路径 | 确保 `zip.write()` 完成后再执行 `os.rename()` |
| 日期编码版号残留 | `20220401` 等数字串未被正则覆盖 | 追加 `2022MMDD` → `YYYYMMDD` 规则 |
| 产品名/系统名残留 | `海峡掌柜` `众行海峡系统` 等业务词汇未收录 | 建立业务词表手动追加规则 |

> **核心原则**：对格式敏感文档，一律使用 **XML 级别直接操作**（`lxml + zipfile`），避免经任何高层库的写入路径。

详见 `工作目录/文档脱敏技能包技术架构方案.md`。
