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

详见 `工作目录/文档脱敏技能包技术架构方案.md`。
