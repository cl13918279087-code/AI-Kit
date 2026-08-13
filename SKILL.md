# SKILL.md - 文档脱敏技能包

> doc-redact-project / v1.0.0
> 金融文档智能脱敏 — Word / Excel / PPT / PDF / 图片批量处理

## 触发条件

当用户发送以下内容时触发本技能：
- "脱敏"、"文档脱敏"、"敏感信息处理"
- "帮我脱敏 XXX.docx"、"批量脱敏"
- "去除敏感信息"、"隐私保护处理"
- 发送了 .docx/.xlsx/.pptx/.pdf/.doc/.xls/.ppt/.png/.jpg/.jpeg 等格式的文件

## 执行流程

### Step 1：理解任务

| 参数 | 默认值 | 可选值 |
|------|--------|--------|
| 输入路径 | 用户指定 | 文件路径或文件夹路径 |
| 输出目录 | `./output` | 可指定 |
| 文件类型 | `all` | `word` / `excel` / `ppt` / `pdf` / `image` / `all` |
| 并行数 | `5` | 1-10 |
| 断点续传 | 否 | `--resume` |
| 覆盖输出 | 否 | `--overwrite` |

### Step 2：执行脱敏

**单文件：**
```bash
cd ~/Projects/doc-redact-project
python3 pipeline.py "<输入文件路径>" -o "<输出目录>"
```

**批量（全部类型，5并发）：**
```bash
python3 pipeline.py "<输入文件夹>" -t all -o "<输出目录>" --workers 5
```

**批量（指定类型）：**
```bash
python3 pipeline.py "<输入文件夹>" -t word,excel,pdf -o "<输出目录>" --workers 5
```

**启用断点续传：**
```bash
python3 pipeline.py "<输入文件夹>" -t all -o "<输出目录>" --workers 5 --resume
```

### Step 3：输出结果

告知用户：
1. **处理结果摘要**：✅成功数 / ❌失败数 / ⏭️跳过数
2. **报告文件路径**：Markdown 汇总报告的位置
3. **脱敏详情**：各类敏感信息检测数量

## 脱敏范围

| 类型 | 替换示例 |
|------|---------|
| 邮箱 | `user@bank.com` → `XXXXX@XXXXX` |
| 身份证号 | `310101199001011234` → `XXXXXXXXXXXXXXXXXX` |
| 银行卡号 | `6222021234567890` → `XXXXXXXXXXXXXXXX` |
| 手机号 | `13812345678` → `XXXXXXXXXXX` |
| 固定电话 | `010-12345678` → `0XX-XXXXXXXX` |
| 日期 | `2024-03-15` → `YYYY/MM/DD` |
| 日期（中文） | `2024年3月15日` → `YYYY年MM月DD日` |
| 日期范围 | 保留连接符，如 `2024/03/15 至 2024/03/20` |
| 银行名称 | `中国工商银行` → `XX银行` |
| 人员姓名（独立） | `张三` → `XXX` |
| 地址 | `北京市朝阳区XX路1号` → `XX省XX市XX区XXXX` |
| IP 地址 | `192.168.1.100` → `X.X.X.X` |
| MAC 地址 | `AA:BB:CC:DD:EE:FF` → `XX:XX:XX:XX:XX:XX` |
| 组织名 | `XX部` / `XX组` / `XX公司` → `XXXX` |

**LLM 增强识别**（MiniMax-Text-01）：嵌入型姓名（文字中间的姓名）、未知业务词汇，由 LLM 层补充检测。

## 输出文件

```
<输出目录>/
├── <原文件名>_脱敏.docx        # 脱敏后文档
├── <原文件名>_脱敏.docx_manifest.json  # 单文件清单（JSON）
├── 脱敏报告_YYYYMMDD_HHMMSS.md  # 批量汇总报告（Markdown）
└── .redact_state.json           # 断点续传状态
```

## 错误处理

| 错误 | 解决方案 |
|------|---------|
| `ModuleNotFoundError` | `pip3 install lxml openpyxl python-pptx PyMuPDF Pillow pytesseract tqdm` |
| `tesseract not found` | macOS: `brew install tesseract tesseract-lang` |
| `.doc/.ppt` 老格式 | `brew install --cask libreoffice` |
| LLM 调用失败 | 检查 `config.json` 中 `api_key`，或 `export MINIMAX_API_KEY=***` |
| 输出文件已存在 | 加 `--overwrite` 参数 |

## 技术说明

- **Word/Excel/PPT**：使用 `lxml + zipfile` 直接操作 OOXML 内部 XML，格式 100% 保留
- **PDF 文本型**：PyMuPDF 提取带坐标文本，原位绘制黑块 + 写入占位符
- **PDF 扫描型**：300DPI 渲染 → pytesseract OCR → 坐标遮盖
- **图片**：pytesseract OCR 坐标 + OpenCV 启发式 Logo 检测
- **LLM 增强**：MiniMax-Text-01 智能识别嵌入型/未知敏感词
- **断点续传**：状态文件记录已处理列表，中断后可从断点继续

## 注意事项

1. **不删除原始文件** — 输入/输出分离
2. **先确认路径存在** — 再执行
3. **告知用户报告位置** — 便于人工复核
4. **处理失败不中断** — 继续处理其他文件
