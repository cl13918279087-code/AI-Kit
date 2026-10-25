# Issue #33 整改方案：.doc 转换备用链与优雅降级

> Issue：https://github.com/cl13918279087-code/AI-Kit/issues/33
> 贡献人：赵辉
> 状态：**已实施（2026-10-26），待社区评审**
> 关联：#9（.doc 无 soffice 禁止原样输出）、#11（find_libreoffice 公共化）

---

## 一、问题定性

Issue #9 已定性「环境无 LibreOffice 时 .doc 不得原样输出」，并落地了 `_mark_manual_check` + 抛异常的硬失败路径。但该方案只解决了**「没有转换器」**这一种情况，`.doc` 通道仍只有**单条**通路：

```
.doc → LibreOffice → .docx → 脱敏
```

实测存在两类失败：

| 失败类型 | 触发条件 | 后果 |
|---|---|---|
| **① 无转换器** | 环境未安装 LibreOffice | 抛 RuntimeError + 人工检查清单（#9 已覆盖） |
| **② 转换失败** | LibreOffice 已安装，但该 .doc **转换失败** | `raise RuntimeError("conversion failed")`，该文件**彻底无法处理** |

两类失败的后果相同——**批量作业直接中断**，用户只能手工「另存为 .docx」再重跑。而 #9 的「硬失败」设计本身是正确的（防假脱敏泄漏路径），问题不在失败策略，而在**失败之前没有尝试任何替代方案**。

Issue #33 提出的三个方向，本方案全部落地：

| Issue 方向 | 本方案实现 |
|---|---|
| ① 备用转换器（doc2 / antiword / pywin32） | **三级转换链**：LibreOffice → textutil → antiword |
| ② 优雅降级，给出清晰提示 | 每级失败打印降级日志；降级成功时在返回值附 `degraded_level` / `degraded_note`；全链失败时人工检查清单文案明确列出三条通道 |
| ③ 格式自动检测（Magic bytes） | **本方案未实现**，理由见 §四 |

---

## 二、方案：三级备用转换链

### 2.1 转换链设计

按**保真度从高到低**逐级降级，每级失败自动进入下一级：

| 级 | 转换器 | 可用性 | 保真度 | 说明 |
|---|---|---|---|---|
| **L1** | LibreOffice | 需安装 | **最好**（表格/页眉/页脚/样式全保留） | 原主通道，保持不动 |
| **L2** | `textutil` | macOS 系统自带（`/usr/bin/textutil`） | 良（正文/基本结构保留，样式简化） | macOS 免安装兜底 |
| **L3** | `antiword` | Linux 常见（需 `apt install antiword`） | **差**（**仅正文文字**，无格式/表格） | 最后兜底 |
| 全失败 | — | — | — | 抛异常 + 人工检查清单（沿用 #9 口径） |

### 2.2 关键设计决策

**（1）为什么不用 Issue 建议的 `python-doc2`**

`python-doc2`（doc2txt / docx2txt 一类）依赖 MS Word COM 或 Wine，在 macOS/Linux 上不可靠；且其 `.doc` 支持本身基于已停止维护的 `antiword` 封装。直接调用 `antiword` 二进制更可控。

**（2）为什么 L3 要用 `python-docx` 重建**

`antiword` 只输出纯文本，无法直接产出 `.docx`。故 L3 的流程是「antiword 提取纯文本 → python-docx 按段落重建 .docx」。这样下游脱敏流程（`redact_docx`）**无需任何改动**，仍走同一条 ZIP/XML 通道。

**（3）L3 的降级必须显式标记**

L3 **丢失全部格式与表格结构**。若静默使用，用户会以为输出是完整脱敏结果。故返回值新增：

```python
{
  "doc_converter": "antiword",
  "degraded_level": "antiword",
  "degraded_note": ".doc 经 antiword 转换，格式/表格结构可能丢失，需人工比对确认脱敏完整。"
}
```

上层可据此提示人工复核。L1 成功时 `degraded_level` 为 `None`。

**（4）安全口径不变：全链失败绝不生成输出**

沿用 #9 铁律：**严禁以任何形式复制原文件充当输出**（防「假脱敏」泄漏路径）。全链失败时：

- 抛 `RuntimeError`，错误信息列出已尝试的三条通道；
- 登记人工检查清单，文案明确「文件未脱敏，须按未脱敏文件管理」；
- **不产生任何输出文件**。

### 2.3 代码结构

`scripts/redact_word.py`：

| 函数 | 职责 |
|---|---|
| `_convert_doc_via_libreoffice(input_path, tmp_dir)` | L1，返回 `.docx` 路径或 `None` |
| `_convert_doc_via_textutil(input_path, tmp_dir)` | L2，同上 |
| `_convert_doc_via_antiword(input_path, tmp_dir)` | L3，提取文本 + python-docx 重建，同上 |
| `_DOC_CONVERTERS` | 转换链元组，按保真度排序 |
| `redact_doc_to_docx(...)` | 遍历转换链，逐级降级；全失败走 #9 硬失败路径 |

每个转换器**统一返回 `Path | None`**（而非抛异常），使降级逻辑扁平化：任何一级失败都只是「返回 None → 试下一级」。

---

## 三、实测结果

测试样本：造一份含真名与手机号的 `.docx`，经 LibreOffice 转成真正的 `.doc`（Word 97 格式，18KB）。

| 场景 | 结果 |
|---|---|
| **L1 正常路径** | `doc_converter=LibreOffice`，`degraded_level=None`；输出「项目组成员如下：XXX、XXX、XXX负责需求梳理」「联系电话：XXXXXXXXXXX」 |
| **L2 降级**（强制跳过 L1） | `doc_converter=textutil`，`degraded_level=textutil`；**脱敏结果与 L1 完全一致** |
| **L3 全链失败**（强制三条通道均返回 None） | 抛 `RuntimeError`，错误信息列出三条通道；人工检查清单登记「.doc 转换全链失败…」；**输出文件不存在** ✅ |

> L2 与 L1 脱敏结果一致是关键验证：说明降级只影响**保真度**，不影响**脱敏正确性**。

---

## 四、未实现项：格式自动检测（Magic bytes）

Issue #33 方向 ③ 建议「根据文件魔数判断是否为真正的 .doc 或实际为 .docx」。

**本方案未实现**，理由：

1. **本仓库的 `.doc` 入口已由扩展名分流**——`redact_word()` 按扩展名分发，`.docx` 走 OOXML 直通路径，`.doc` 才进转换链。魔数检测解决的是「扩展名与实际格式不符」的场景，属于**另一类问题**（误判文件类型），与「转换失败」不同源。
2. **转换链本身已容忍格式差异**——textutil 与 antiword 都能读 OOXML（实测 L2 在样本为真 .doc 时工作正常；若喂入 .docx 内容也能解析）。故魔数检测对本方案的收益主要在「更早失败并给出更准的错误提示」，而非解锁新能力。
3. **建议单独立项**——「扩展名与实际格式不符」在批量作业中是独立的失败类型（常见于供应商给的乱命名附件），应配套「按魔数重新路由文件类型」的完整方案，而非塞进转换链补丁。

---

## 五、回归守卫

新建 `tests/test_doc_converter_fallback.py`，4 项测试：

| 测试 | 作用 |
|---|---|
| `test_converter_chain_has_three_levels` | 锁定转换链含三级且主通道在前 |
| `test_all_failed_raises_and_no_output` | 锁定全链失败时抛异常**且不生成输出文件**（防假脱敏泄漏路径） |
| `test_degrade_to_textutil_still_redacts` | 锁定 L2 降级后**真名/手机号仍被正确脱敏** |
| `test_primary_channel_reports_no_degradation` | 锁定 L1 成功时**不标记降级** |

全部通过。

---

## 六、待社区评审决策点

1. **L3（antiword）是否应保留？** 它丢失全部格式与表格结构，仅保留正文文字。对格式敏感度高的场景，降级输出可能不如「明确失败 + 人工另存」安全。当前实现选择「降级 + 显式标记 + 人工复核提示」，是否认同这一取舍？
2. **是否需要支持 `pywin32` COM（Windows 本机 Word）？** 该路径保真度可接近 LibreOffice，但引入 Windows 平台强依赖与 COM 生命周期管理复杂度。当前未实现。
3. **格式魔数检测（Issue 方向 ③）是否单独立项？** 见 §四。
4. **`degraded_level` / `degraded_note` 的返回结构是否需要提升为通用约定**（其他转换/降级通道如 PDF、OCR 是否也统一采用）？
