# SKILL.md - 文档脱敏技能包

> doc-redact-project / v1.0.0
> 金融文档智能脱敏 — Word / Excel / PPT / PDF / 图片批量处理

## 触发条件

当用户发送以下内容时触发本技能：
- "脱敏"、"文档脱敏"、"敏感信息处理"
- "帮我脱敏 XXX.docx"
- "批量脱敏"
- "去除敏感信息"
- "隐私保护处理"
- 发送了 .docx/.xlsx/.pptx/.pdf/.doc/.xls/.ppt/.png/.jpg/.jpeg 等格式的文件

---

## 执行流程

### Step 1：理解任务

确认以下信息（如用户未明确说明则按默认值处理）：

| 参数 | 默认值 | 选项 |
|------|--------|------|
| 输入路径 | 用户指定 | 文件路径或文件夹路径 |
| 输出目录 | `./output` | 可指定 |
| 文件类型 | `all`（全部） | `word` / `excel` / `ppt` / `pdf` / `image` / `all` |
| 并行数 | `5` | 1-10 |
| 断点续传 | `否` | `--resume` 启用 |
| 覆盖输出 | `否` | `--overwrite` 覆盖 |

### Step 2：检查环境

在执行前检查：

```bash
# 1. 确认 Python 依赖已安装
pip show lxml openpyxl python-pptx PyMuPDF Pillow pytesseract 2>/dev/null | grep Name

# 2. 确认 tesseract 可用
tesseract --version 2>&1 | head -1

# 3. 确认 LLM API Key
echo $MINIMAX_API_KEY  # 或 config.json 中的 api_key
```

### Step 3：执行脱敏

**单文件：**
```bash
cd ~/Projects/doc-redact-project
python3 pipeline.py "<输入文件路径>" -o "<输出目录>"
```

**批量（指定类型）：**
```bash
python3 pipeline.py "<输入文件夹>" -t word,excel,pdf,ppt,image -o "<输出目录>" --workers 5
```

**批量（全部类型）：**
```bash
python3 pipeline.py "<输入文件夹>" -t all -o "<输出目录>" --workers 5
```

**启用断点续传：**
```bash
python3 pipeline.py "<输入文件夹>" -t all -o "<输出目录>" --workers 5 --resume
```

### Step 4：输出报告

脱敏完成后，将以下信息告知用户：

1. **处理结果摘要**：成功数 / 失败数 / 跳过数
2. **报告文件路径**：Markdown 汇总报告的位置
3. **脱敏详情**：各类敏感信息检测数量

---

## 脱敏范围

| 类型 | 替换示例 |
|------|---------|
| 邮箱 | `user@bank.com` → `XXXXX@XXXXX` |
| 身份证号 | `310101199001011234` → `XXXXXXXXXXXXXXXXXX` |
| 银行卡号 | `6222021234567890` → `XXXXXXXXXXXXXXXX` |
| 手机号 | `13812345678` → `XXXXXXXXXXX` |
| 固定电话 | `010-12345678` → `0XX-XXXXXXXX` |
| 日期 | `2024-03-15` → `YYYY/MM/DD` |
| 日期范围 | 保留连接符，如 `2024/03/15 至 2024/03/20` |
| 银行名称 | `中国工商银行` → `XX银行` |
| 人员姓名 | `张三` → `XXX` |
| 地址 | `北京市朝阳区XX路1号` → `XX省XX市XX区XXXX` |
| IP 地址 | `192.168.1.100` → `X.X.X.X` |
| 金额 | 具体数字 → `[金额]` |
| 组织名 | `XX部` / `XX组` / `XX公司` → `XXXX` |

---

## 错误处理

| 错误 | 解决方案 |
|------|---------|
| `ModuleNotFoundError: lxml` | `pip install lxml openpyxl python-pptx PyMuPDF Pillow pytesseract` |
| `tesseract not found` | macOS: `brew install tesseract tesseract-lang` |
| `LibreOffice not found`（.doc/.ppt） | `brew install --cask libreoffice` |
| `MINIMAX_API_KEY` 未设置 | 设置环境变量或在 `config.json` 中配置 |
| 输出文件已存在 | 添加 `--overwrite` 参数，或更换输出目录 |

---

## 输出文件说明

```
<输出目录>/
├── <原文件名>_脱敏.docx        # 脱敏后文档
├── <原文件名>_脱敏.docx_manifest.json  # 单文件脱敏清单
├── 脱敏报告_YYYYMMDD_HHMMSS.md  # 批量处理汇总报告（Markdown）
└── .redact_state.json           # 断点续传状态文件
```

---

## 技术说明（供 Agent 内部参考）

- **Word/Excel/PPT**：使用 `lxml + zipfile` 直接操作 OOXML 内部 XML，完全绕开 python-docx/python-pptx 的保存逻辑，格式 100% 保留
- **PDF 文本型**：PyMuPDF 提取带坐标文本，在原位置绘制黑块 + 写入占位符
- **PDF 扫描型**：PyMuPDF 300DPI 渲染 → pytesseract OCR → 坐标遮盖
- **图片**：pytesseract OCR 坐标 + OpenCV 启发式 Logo 检测
- **LLM 增强**：MiniMax-Text-01 辅助识别未知业务词汇，与 Regex 规则互补
- **断点续传**：状态文件记录已处理文件列表，中断后可从断点继续

---

## 注意事项

1. **不要删除用户的原始文件** — 输出文件与输入文件分离存放
2. **先确认输入路径存在** — 再执行脱敏
3. **告知用户报告位置** — 让用户能看到具体脱敏了哪些内容
4. **处理失败的文件** — 记录错误原因，不中断其他文件处理
