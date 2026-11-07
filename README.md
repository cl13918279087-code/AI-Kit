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
| 人员姓名 | `XXX` | `张三` → `XXX`（三规则体系：姓氏+名字汉字 + 冒号后姓名 + 冒号后单字名） |
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

> ⚠️ 以下类型**默认禁用**（误脱敏风险高）：**银行卡号（BANK_CARD）**——纯数字长串易误伤账号/订单号，故整段规则不启用。
>
> 固定电话与组织名**当前为启用**状态（与 `common_rules._build_patterns()` 实际生效规则一致）：
> · 固定电话 → `0XX-XXXXXXXX`（R-D 社区决策「全遮」，防号码反查个体）
> · 组织名 ORG → `XXXX`（后缀由 `config.json` 的 `org_suffixes` 驱动，默认非空）
> 如需关闭，请修改 `scripts/config.json` 或调整 `common_rules._build_patterns()`。

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
 内网推理网关  链式正则替换
 (Agent侧LLM)  (邮箱/手机/身份证/银行名...)
```

> 📌 **LLM 归属口径**：本技能包**不自带、不托管**任何 LLM 服务与凭据。LLM 通道默认关闭，
> 实体识别仅**复用 Agent 侧已有的 LLM 调用能力**；若由 Agent 直接执行脱敏脚本，
> 则由 Agent 自行决定是否注入上下文，本包不外呼、不存储 API Key。
> 详见 `docs/内网部署指南.md` §5。

**姓名检测三规则体系：**

```
Rule A: (?<![a-zA-Z0-9])(?:姓氏)[名字汉字]{1,2}(?![a-zA-Z0-9]...)
  └─ 通用场景，前后非 ASCII/数字，防止中文词内部误判

Rule B: (?<=[\u4e00-\u9fa5\uff1a])(?:姓氏)[名字汉字]{2}(?![a-zA-Z0-9])
  └─ "总行支持人员：汪晶晶"等场景，左边界含全角冒号

Rule C: (?<=：)(?:姓氏)[名字汉字]{1}(?![a-zA-Z0-9])
  └─ "组长：樊霖副组长"等场景，姓氏后的第1个名字汉字在角色词上下文中
```

## 安装

```bash
# 克隆项目
git clone https://github.com/cl13918279087-code/AI-Kit.git
cd AI-Kit/doc-redact-project

# 安装依赖（运行时 + 测试）
pip install -r requirements.txt -r requirements-dev.txt

# macOS 安装 tesseract（OCR 依赖）
brew install tesseract tesseract-lang

# 装载后自检（内网投产必做）
python3 scripts/doctor.py
```

> ⚠️ **LLM 凭据**：本包**默认不使用任何 API Key**（`llm.enabled = false`）。
> 若内网启用 LLM 通道，请在 `scripts/config.json` 的 `llm.api_key` 填写**内网推理网关**
> 的凭据，或由 Agent 侧注入环境变量；`base_url` **必须**指向内网端点，否则被出网守卫拒绝。

> 📦 **企业内网部署**：请看 [企业内网部署](#企业内网部署) 章节与 `docs/内网部署指南.md`。

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
    "enabled": false,
    "provider": "openai-compatible",
    "model": "内网推理网关模型名",
    "base_url": "http://llm-gateway.intranet.corp/v1",
    "api_key": "***",
    "allowed_hosts": ["llm-gateway.intranet.corp"],
    "require_private_network": true,
    "timeout": 300,
    "pre_redact_before_llm": true
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

> 🔒 **重要（合规）**：LLM 通道**默认关闭**且**无公网默认端点**。启用前须确认三点：
> ① `base_url` 指向企业**内网**推理网关（或已加入 `allowed_hosts` 白名单）；
> ② 送 LLM 前已启用 `pre_redact_before_llm`（身份证/银行卡/手机/固话/邮箱等唯一标识，以及账号/证件号/住址/证件影像引用做等长掩码）；
> ③ 运行后检查 manifest 的 `llm_audit` 段留痕。
> 出网守卫判定：`scripts/llm_egress.py`，详见 `docs/技术架构文档.md` §6.2–6.4。

**Agent 直处理模式的端点自检**（内网部署必做）：

```bash
# 校验 Agent 实际使用的 LLM 端点（只读，不发任何请求）
python3 scripts/endpoint_check.py --base-url http://llm-gateway.intranet.corp/v1
# 机器可读（供内网合规扫描）
python3 scripts/endpoint_check.py --base-url http://llm-gateway.intranet.corp/v1 --json
```

退出码：`0` 全部合规 / `2` 存在不合规端点 / `3` 参数或配置错误。判定口径与脚本侧
出网守卫**同源**（复用 `llm_egress.check_egress`）。详见 `docs/技术架构文档.md` §6.3.1。

## 企业内网部署

面向「内网 OpenClaw 类 Agent 装载本技能包」场景的完整交付链路。

### 1. 构建分发包（交付物）

仓库 ≠ 可交付物。构建脚本产出**最小运行集**三件套：

```bash
python3 scripts/build_package.py
# 产出 dist/doc-redact-1.0.0.zip
#      dist/doc-redact-1.0.0.zip.sha256      （验签）
#      dist/doc-redact-1.0.0.manifest.json  （逐文件 SHA256 + 依赖声明）
```

打包时自动剥离 `tests/`、`docs/`、`.github/`、`scripts/mine_*.py` 等开发资产，
并**剥离 `config.json` 中的内联真实人名库**；若未配置 `full_name_list_file`，
构建会**直接失败**（拒绝交付「看着干净但能力残缺」的包）。

### 2. 内网侧安装与自检

```bash
sha256sum -c doc-redact-1.0.0.zip.sha256
unzip doc-redact-1.0.0.zip -d /opt/agent-skills/doc-redact

# 离线安装（内网无公网 pip）
pip install --no-index --find-links=/opt/wheelhouse -r requirements.txt

# 装载后体检（内网投产门禁）
cd /opt/agent-skills/doc-redact && python3 scripts/doctor.py
```

`doctor.py` 覆盖六组检查：Python 版本、必需/可选 Python 包、外部二进制、
包内必需文件、LLM 端点合规、开发者本机路径残留。

| 退出码 | 含义 | 处置 |
|--------|------|------|
| `0` | 全部通过 | 可投产 |
| `1` | 必装项缺失 / 端点不合规 | **禁止投产**，按 `hint` 补齐 |
| `2` | 仅告警项（如 OCR/LibreOffice 缺失） | 可运行，须确认降级影响面 |
| `3` | 自检无法执行 | 排查运行环境 |

常用参数：`--json`（机器可读，供内网合规扫描）、`--strict`（告警升级为失败）、
`--base-url`（校验实际使用的 LLM 端点）。

### 3. 外部二进制（不在 Python 包体系内，须运维离线安装）

| 组件 | 用途 | 缺失影响 |
|------|------|---------|
| `tesseract` + `chi_sim` | 图片 OCR | 图片脱敏不可用 |
| `soffice` / LibreOffice | `.doc` → `.docx` 转换 | `.doc` 报错终止（防假脱敏） |

非标准路径可用 `SOFFICE_PATH` 指定；Windows 下 `tesseract` 路径由脚本自动探测。

### 4. 人名库外置口径（内网数据不出包）

配置项 `full_name_list_file` 指向行分隔的人名清单文件，**优先级高于**内联库；
也可用环境变量 `AIKIT_FULL_NAME_LIST` 直接指定文件路径，或用 `AIKIT_DATA_ROOT`
指定数据根目录。语义为 **内联 ∪ 外置**（外置优先、去重保序），外置文件缺失时
**静默回退**到内联库（不报错、不中断）。

### 5. LLM 端点合规

内网启用 LLM 通道时，`base_url` 必须为内网地址并加入 `allowed_hosts`；
判定口径与出网守卫**同源**（`scripts/llm_egress.py`），可用
`python3 scripts/endpoint_check.py --base-url <URL>` 独立校验。

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

## 退出码与降级产物

「降级但产物仍生成」是典型的**静默失真**（如 `.doc` 经备用转换器转换后表格结构丢失）。
本项目把降级事件在审计链三处同时呈现，退出码语义明确：

| 退出码 | 含义 | 处置 |
|--------|------|------|
| `0` | 全部成功且无降级 | 正常归档 |
| `1` | 存在**失败**文件（**无产物**） | 必须立即介入排查 |
| `2` | 无失败但存在**降级产物**（产物已生成，保真度下降） | 须人工复核后归档 |

- manifest 新增 `degradations` 段（`count` + `items`），Markdown 清单含「⚠️ 降级产物」章节；
- 批量报告含「⚠️ 降级产物清单（需人工复核）」章节；
- 降级台账登记内容**只含组件名、级别、原因与复核提示，不含任何文档原文**。

详见 `docs/技术架构文档.md` §8.4。

## CI 门禁

`.github/workflows/ci.yml` 提供三层强制卡口（`push` / `pull_request` on `main`）：

| 层 | 内容 |
|----|------|
| `lint` | `compileall` 语法编译 + 全仓 JSON 合法性校验 |
| `unit` | `pytest tests/`（先不含 benchmark，再完整） |
| `gate` | `python scripts/bench_metrics.py` — **八项指标门禁，唯一阈值源** |
| `install-check` | 构建分发包 → SHA256 校验 → 解压到干净目录 → 任意 CWD 跑 `doctor.py` → **真实 docx 脱敏冒烟** |

依赖安装一律按 `requirements.txt` + `requirements-dev.txt` 执行（依赖声明唯一来源），
CI 内不手写包名清单。

门禁阈值全部取自 `scripts/bench_metrics.py`（与 `quality_validator.py` 同源），
workflow 内不硬编码任何阈值数字，避免两处口径漂移。

## 项目结构

```
doc-redact-project/
├── SKILL.md             # Agent 技能包注册入口（YAML frontmatter）
├── LICENSE              # MIT + 第三方组件许可说明
├── requirements.txt     # 运行时依赖（唯一来源）
├── requirements-dev.txt # 开发/测试依赖
├── .github/workflows/   # CI 流水线（lint / unit / gate / install-check）
├── scripts/
│   ├── config.json      # 全局配置（LLM / 规则 / 批处理参数）
│   ├── pipeline.py      # 统一入口 / 批量处理 / 断点续传
│   ├── common_rules.py  # 统一脱敏规则（所有脚本共享）
│   ├── entity_detector.py  # LLM 增强检测层 + 送 LLM 前最小化预脱敏
│   ├── llm_client.py    # 多模型 LLM 客户端
│   ├── llm_egress.py    # LLM 出网端点守卫（内网投产合规）
│   ├── llm_premask.py   # 送 LLM 前预脱敏 CLI（等长掩码）
│   ├── endpoint_check.py # Agent 端点合规自检 CLI
│   ├── degradation.py   # 降级事件台账（审计链三处呈现）
│   ├── manifest.py      # 脱敏清单与报告生成
│   ├── prompts.py       # LLM Prompt 模板
│   ├── doctor.py        # 装载后安装自检（六组检查 / 退出码门禁）
│   ├── build_package.py # 分发包构建（zip + sha256 + manifest）
│   ├── redact_word.py   # Word 脱敏
│   ├── redact_excel.py  # Excel 脱敏
│   ├── redact_ppt.py    # PPT 脱敏
│   ├── redact_pdf.py    # PDF 脱敏
│   └── redact_image.py  # 图片脱敏
├── references/          # 脱敏规则与规格说明（随包交付）
├── docs/                # 项目文档（不随分发包交付）
│   ├── 技术架构文档.md
│   ├── 项目实施手册.md
│   └── 内网部署指南.md  # 内网投产部署手册
├── tests/               # 单元测试
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
先确认 `scripts/config.json` 中 `llm.enabled` 是否为 `true`（默认 `false`）、
`base_url` 是否指向内网端点、`api_key` 是否正确。公网端点会被出网守卫直接拒绝。
若由 Agent 直接执行脚本，LLM 能力由 Agent 侧提供，本包不管理凭据。

**Q: .doc 文件无法处理？**
需要安装 LibreOffice：
```bash
# macOS
brew install --cask libreoffice
# Linux
sudo apt install libreoffice
```
Windows 用户也可从 https://www.libreoffice.org/ 下载安装。

> **v1.3.2 起安全策略变更**：环境未安装 LibreOffice 时，`.doc` 文件将**直接报错终止、不生成任何输出文件**（旧版本仅告警）。这是为了防止"输出文件名带'脱敏'但内容未脱敏"的假脱敏泄漏。也可先在 Word/WPS 中将文件**另存为 .docx** 再处理。出现该报错时，原文件须按未脱敏文件管理。

**Q: 脱敏后格式丢失？**
Word/Excel/PPT 脚本直接操作 XML，不会调用 python-docx/pptx 的保存方法，格式完整保留。

**Q: OpenClaw 任务运行时被中断？**
桌面应用配置中设置 `agents.defaults.timeoutSeconds: 300`（5分钟），防止长任务被中断。

## License

MIT License（见 `LICENSE`，含第三方组件与数据许可说明）

---

## 修订记录（与 GitHub 下载版本的差异）

| 项目 | GitHub v1.0.0 | 本地 v1.2.0 | 说明 |
|------|-------------|------------|------|
| 姓名检测 | 单规则（硬编码字池） | 三规则（Rule A + Rule B + Rule C）| Rule C 支持"组长：樊霖副组长"等角色词+单字名场景 |
| name_pool | 119 字，含误脱敏字 | 154 字，干净版 | 移除 `行/为/业/务/支` 等常见字 |
| 分行/支行后缀 | 无保留 | `_branch_repl` 保留后缀 | `长汀支行` → `XX支行` |
| DATE 后处理 | 无 | Post-processing 纠正 | `应为YYYY年MM月DD日` → `应为XXXX年XX月XX日` |
| 密码脱敏 | 无 | 新增模式 | `密码456456` → `******` |
| IP 地址脱敏 | 无 | 新增模式 | `192.168.x.x` → `X.X.X.X` |
| LLM timeout | 120s | 300s | 防止大文档超时 |
| 固定电话 | 启用 | 启用（全遮 `0XX-XXXXXXXX`）| R-D 社区决策，防号码反查 |
| 组织名 ORG | 启用 | 启用（`org_suffixes` 驱动）| 按后缀精确匹配 |
| 银行卡号 | 启用 | 禁用 | 误脱敏风险高（易误伤账号/订单号）|
| 技术文档 | v1.0.0 | v1.2.0 新增Rule C + Issue 13.5/13.6/13.7 | Word XML腐败修复、DOC转换比对、XXX模糊匹配 |
| 实施手册 | v1.0.0 | v1.2.0 新增Rule C说明 + 修订记录 | 三规则体系说明 |
| Word XML处理 | 潜在文本堆积bug | v1.2.0 修复 | LLM+regex双加工问题导致w:t节点文本错位 |
| DOC比对 | 基础解码（乱码） | LibreOffice转换+模糊匹配 | Issue 13.6/13.7 修复 |
| XXX比对 | 精确位置set匹配 | 上下文签名模糊匹配 | 对文本结构变化更鲁棒 |
