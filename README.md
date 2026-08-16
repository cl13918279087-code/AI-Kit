# doc-redact-project

> 金融文档智能脱敏工具包 — 支持 Word / Excel / PPT / PDF / 图片批量脱敏

[![Python >= 3.9](https://img.shields.io/badge/Python-%3E%3D%203.9-blue)](https://www.python.org/)
[![License MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

## 功能特性

| 格式 | 支持类型 | 核心能力 |
|------|---------|---------|
| **Word** | .docx / .doc | XML 级别直写，100% 保留样式；正文/表格/页眉页脚/批注/文本框 |
| **Excel** | .xlsx / .xls | OOXML 直写，保留公式；共享字符串/单元格/批注 |
| **PPT** | .pptx / .ppt | XML 直写，幻灯片/备注/母版/媒体文件名 |
| **PDF** | 文本型 + 扫描版 | PyMuPDF 坐标遮盖；OCR 扫描版自动转图片处理 |
| **图片** | png/jpg/bmp/gif/webp/tiff | OCR 坐标定位 + OpenCV 启发式 Logo 检测 |

### 核心脱敏能力

| 类别 | 替换结果 | 示例 |
|------|---------|------|
| 人员姓名 | `XXX` | `张三` → `XXX`（双规则体系：姓氏+名字汉字 + 冒号后姓名） |
| 银行名称 | `XX银行` | `福建海峡银行` → `XX银行` |
| 分行/支行 | `XX分行` / `XX支行` | `福州三山支行` → `XX支行`（保留后缀） |
| 日期（中文） | `YYYY年MM月DD日` | `2022年3月31日` → `YYYY年MM月DD日` |
| 日期（斜杠） | `YYYY/MM/DD` | `2022-03-31` → `2022/03/31` |
| 邮箱 | `XXXXX@XXXXX` | `zhangsan@bank.com` |
| 身份证号 | `XXXXXXXXXXXXXXXXXX` | `310101199001011234` |
| 银行卡号 | `XXXXXXXXXXXXXXXX` | `622202**** **** 0123` |
| 手机号 | `XXXXXXXXXXX` | `138****5678` |
| IP 地址 | `X.X.X.X` | `192.168.1.1` |
| 密码 | `******` | `密码123456` → `密码******` |

> ⚠️ 以下类型**默认禁用**（误脱敏风险高）：固定电话（ADDRESS）、组织名（ORG）、银行卡号（BANK_CARD）

## 核心架构

```
输入文档
  │
  ▼
┌──────────────────────────────────────────┐
│  pipeline.py  — 统一入口 + 批量并行        │
│  支持断点续传 / 进度条 / 类型过滤          │
└───────────────┬──────────────────────────┘
                │
    ┌───────────┼───────────┐
    ▼           ▼           ▼
 entity_      common_      manifest
 detector     rules         (报告生成)
 (LLM层)      (Regex层)
    │           │
    ▼           ▼
 MiniMax     链式正则替换
 Text-01     (邮箱/手机/身份证/银行名...)
```

**姓名检测双规则体系：**

```
Rule A: (?<![a-zA-Z0-9])(?:姓氏)[名字汉字]{1,2}(?![a-zA-Z0-9]...)
  └─ 通用场景，前后非 ASCII/数字，防止中文词内部误判

Rule B: (?<=[\u4e00-\u9fa5\uff1a])(?:姓氏)[名字汉字]{2}(?![a-zA-Z0-9])
  └─ "总行支持人员：汪晶晶"等场景，左边界含全角冒号
```

## 安装

```bash
# 克隆项目
git clone https://github.com/cl13918279087-code/AI-Kit.git
cd AI-Kit/doc-redact-project

# 安装依赖
pip install -r requirements.txt

# macOS 安装 tesseract（OCR 依赖）
brew install tesseract tesseract-lang

# 配置 API Key
export MINIMAX_API_KEY="***"
# 或直接编辑 config.json 中的 api_key 字段
```

## 快速开始

### 单文件脱敏

```bash
python3 pipeline.py ./sample.docx -o ./output
```

### 批量脱敏（指定类型）

```bash
python3 pipeline.py ./docs -t word,excel,pdf -o ./output --workers 4
```

### 批量脱敏（全部类型）

```bash
python3 pipeline.py ./docs -t all -o ./output --workers 5
```

### 断点续传

```bash
python3 pipeline.py ./docs -t all -o ./output --resume
```

### 覆盖已存在输出

```bash
python3 pipeline.py ./docs -t all -o ./output --overwrite
```

## 配置文件

编辑 `config.json` 自定义规则：

```json
{
  "llm": {
    "provider": "minimax",
    "model": "MiniMax-Text-01",
    "api_key": "***",
    "timeout": 300
  },
  "replacement": {
    "NAME": "XXX",
    "BANK": "XX银行"
  },
  "bank_names": [
    "中国工商银行",
    "中国农业银行"
  ],
  "name_pool": "伟强志建华文静宇轩...",
  "surname_pool": "王李张刘陈杨赵黄周吴...",
  "batch": {
    "max_workers": 5
  }
}
```

> ⚠️ **重要**：`llm_client.py` 的 HTTP timeout 控制在 `config.json` 的 `timeout` 字段，**不是** `openclaw.json` 的 `timeoutSeconds`。

## OpenClaw Skill 使用

将 `SKILL.md` 复制到 OpenClaw skills 目录，即可通过对话调用：

```
帮我脱敏 ~/Documents/银行报告.docx
批量脱敏 ~/Projects/docs 目录下的所有 Word 文档
生成脱敏报告
```

## 输出文件

```
output/
├── 银行报告_脱敏.docx      # 脱敏后的文档
├── 银行报告_脱敏.docx_manifest.json  # 详细清单（JSON）
├── 演示文稿_脱敏.pptx
├── 脱敏报告_20260813_143022.md  # Markdown 汇总报告
└── .redact_state.json     # 断点续传状态（--resume 时生成）
```

## 项目结构

```
doc-redact-project/
├── config.json          # 全局配置（LLM / 规则 / 批处理参数）
├── requirements.txt     # Python 依赖
├── pipeline.py          # 统一入口 / 批量处理 / 断点续传
├── common_rules.py      # 统一脱敏规则（所有脚本共享）
├── entity_detector.py   # LLM 增强检测层
├── llm_client.py        # 多模型 LLM 客户端
├── manifest.py          # 脱敏清单与报告生成
├── prompts.py           # LLM Prompt 模板
├── scripts/
│   ├── __init__.py
│   ├── redact_word.py   # Word 脱敏
│   ├── redact_excel.py  # Excel 脱敏
│   ├── redact_ppt.py    # PPT 脱敏
│   ├── redact_pdf.py    # PDF 脱敏
│   └── redact_image.py  # 图片脱敏
├── docs/                # 项目文档
│   ├── 技术架构文档.md   # v1.1.0
│   └── 项目实施手册.md   # v1.1.0
├── tests/               # 单元测试
├── SKILL.md             # OpenClaw Skill
└── README.md
```

## 常见问题

**Q: tesseract 未找到？**
```bash
# macOS
brew install tesseract tesseract-lang
# Linux
sudo apt install tesseract-ocr tesseract-ocr-chi-sim
# Windows: 下载 https://github.com/UB-Mannheim/tesseract/wiki
```

**Q: LLM 调用失败？**
检查 `config.json` 中 `api_key` 是否正确，或设置环境变量 `MINIMAX_API_KEY`。

**Q: .doc 文件无法处理？**
需要安装 LibreOffice：
```bash
# macOS
brew install --cask libreoffice
# Linux
sudo apt install libreoffice
```

**Q: 脱敏后格式丢失？**
Word/Excel/PPT 脚本直接操作 XML，不会调用 python-docx/pptx 的保存方法，格式完整保留。

**Q: OpenClaw 任务运行时被中断？**
桌面应用配置中设置 `agents.defaults.timeoutSeconds: 300`（5分钟），防止长任务被中断。

## License

MIT License

---

## 修订记录（与 GitHub 下载版本的差异）

| 项目 | GitHub v1.0.0 | 本地 v1.1.0 | 说明 |
|------|-------------|------------|------|
| 姓名检测 | 单规则（硬编码字池） | 双规则（Rule A + Rule B）| Rule B 支持"总行支持人员：XXX"场景 |
| name_pool | 119 字，含误脱敏字 | 154 字，干净版 | 移除 `行/为/业/务/支` 等常见字 |
| 分行/支行后缀 | 无保留 | `_branch_repl` 保留后缀 | `长汀支行` → `XX支行` |
| DATE 后处理 | 无 | Post-processing 纠正 | `应为YYYY年MM月DD日` → `应为XXXX年XX月XX日` |
| 密码脱敏 | 无 | 新增模式 | `密码456456` → `******` |
| IP 地址脱敏 | 无 | 新增模式 | `192.168.x.x` → `X.X.X.X` |
| LLM timeout | 120s | 300s | 防止大文档超时 |
| 固定电话 | 启用 | 禁用 | 误脱敏风险高 |
| 组织名 ORG | 启用 | 禁用（`org_suffixes=[]`）| 误脱敏风险高 |
| 技术文档 | v1.0.0 | v1.1.0 新增第4章姓名规则详解 + 第13章已知问题记录 | 详细记录 Issue 3/4 根因与修复 |
| 实施手册 | v1.0.0 | v1.1.0 新增第3章脱敏范围说明 + 修订记录 | 新增 name_pool 管理原则、排除词配置 |
