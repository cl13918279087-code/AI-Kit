# Issue #54 D1 裁决闭环标准化 — 落地方案

> 状态：**已落地**（`main` @ `4228f5b` 之后合入）· 关联上游 Issue #54（AI-Kit）
> 目标：把「规则引擎产出灰区候选 → 人工/Agent 裁决 → 回灌定稿 → 高频裁决沉淀为规则」
> 变成一条**可复现、可归档、可度量**的标准闭环。

## 一、标准链路

```
规则引擎
  └─ detect_gray_entities()        候选清单（span + 置信度 + 命中通道）
      └─ export_auto_gray_decisions()  → decisions_pending.json
          └─ Agent / LLM 裁决         → decisions.json
              └─ read_decision_records()  （兼容列表与 {"pending": [...]} 两种格式）
                  └─ set_agent_decisions_override() 回灌
                      └─ 二轮定稿
                          └─ write_redaction_manifest()  裁决记录归档
                              └─ decision_mining.py  高频裁决 → 规则候选
```

## 二、本轮补齐的四项缺口

### 2.1 候选带 span 与命中通道

`detect_gray_entities()` 与 `export_auto_gray_decisions()` 的候选项新增：

- `span: {start, end}`——实体在原文中的字符区间；
- `channel`——命中通道标签，取值：
  `职务称谓 / 昵称形态 / 单字姓锚点 / 角色槽位 / 姓名规则 / 地址通道 /
  日期规则 / 英文名 / LLM / 正则兜底 / 未知`。

实现：`scripts/common_rules.py` → `classify_hit_channel()` / `find_entity_span()`。

### 2.2 裁决记录可归档

- `scripts/manifest.py`：`RedactionManifest` 新增 `decisions` 段、
  `add_decision()` / `add_decisions()`；
  `to_dict()` 输出 `decisions`；`to_markdown()` 新增「⚖️ 裁决记录」章节
  （实体 / 裁决 / 置信度 / 通道 / 来源）；
- `scripts/redact_all.py`：新增 `write_redaction_manifest()`，
  在 `--agent-decisions` 与 `--auto-gray`/默认单文件流程结束后自动落盘
  `<output>_manifest.json` 与 `<output>_manifest.md`。

### 2.3 高频裁决 → 规则候选入口

新增 `scripts/decision_mining.py`：

```
python3 scripts/decision_mining.py --decisions <decisions.json> --min-freq 3 --out candidates.json
```

产出四类：

| 产出 | 来源 | 去向 |
|---|---|---|
| `guard_candidates` | 高频 `skip` 裁决 | `common_word_guards`（保护表） |
| `name_candidates` | 高频 `redact` 裁决 | `full_name_list`（姓名库） |
| `conflicts` | 命中真名库 / 占位名形态者 | **禁止**入保护表 |
| `inconsistent` | 同一实体裁决不一致 | 人工复核 |

**只产候选，不自动改配置**，避免自反馈污染（裁决 → 规则 → 候选 → 裁决 的闭环震荡）。

### 2.4 指标口径统一

- 新增 `scripts/bench_metrics.py` 作为**唯一指标与阈值来源**：
  `MIN_GOLD_RECALL=0.85`、`MAX_BIZ_MISFIRE=12`、`MAX_REAL_BIZ_MISFIRE=0`、
  `MIN_AMBIG_ACCURACY=0.60`、`MIN_PRECISION=0.95`；
- `scripts/quality_validator.py --json` 委托 `bench_metrics`，
  与 CI 门禁 `tests/benchmark/test_benchmark_gate.py` **同源同口径**
  （门禁阈值常量直接从 `bench_metrics` import），杜绝口径漂移。

## 三、当前指标

| 指标 | 值 | 门槛 | 判定 |
|---|---|---|---|
| gold_recall | 100%（283/283） | ≥85% | ✅ |
| biz_misfire | 8/102（7.8%） | ≤12 | ✅ |
| real_biz_misfire | 0/1200 | 0 | ✅ |
| ambiguity_accuracy | 13/13 | ≥60% | ✅ |
| precision | 99.39% | ≥95% | ✅ |
| determinism | 100% | 100% | ✅ |
| idempotency | 100% | 100% | ✅ |

## 四、验收

`tests/test_issue54_d1_closure.py`（**11 项**全绿）覆盖：

1. 候选 span/confidence/channel 字段；
2. 通道分类正确性；
3. auto-gray 端到端；
4. manifest 裁决归档；
5. 双格式裁决文件兼容读取；
6. 裁决挖掘候选与互斥规则；
7. 质检与门禁口径一致；
8. 门禁全绿；
9. 歧义集入库；
10. **无 LLM Key 全流程可跑**（LLM 缺席时链路自动降级不崩）。

## 五、可核对清单

- 闭环实现：`scripts/redact_all.py`
- 通道分类与归档：`scripts/common_rules.py`、`scripts/manifest.py`
- 裁决挖掘：`scripts/decision_mining.py`
- 指标与门禁：`scripts/bench_metrics.py`、`scripts/quality_validator.py --json`、
  `tests/benchmark/test_benchmark_gate.py`
- 用例：`tests/test_issue54_d1_closure.py`
