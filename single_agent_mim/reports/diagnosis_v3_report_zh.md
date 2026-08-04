# 诊断 V3 运行报告

> 生成日期：2026-07-30 | 模型：DeepSeek V4 Pro | 诊断框架：三阶段流水线 (Judge → Answer → Access + Cons)

---

## 1. 运行概览

| 项目 | 数值 |
|------|------|
| Judge 判定模型 | `deepseek-v4-pro` |
| Judge 提示词版本 | `locomo_semantic_judge_v2` |
| Judge 输入文件 | `outputs/judge/deepseek_v4_pro_locomo_judge_v2/judgments.jsonl` |
| Judge 总判定数 | 1200 |
| **符合条件 P/I 数** | **688**（P=212, I=476） |
| 诊断模型 | `deepseek-v4-pro`（使用 `maintenance` 配置） |
| 源运行数 | 6（conv-30, conv-42, conv-43, conv-44, conv-48, conv-49） |
| 输出根目录 | `outputs/diagnosis/deepseek_v4_pro_diag_v3` |

---

## 2. 各组件完成情况

### 2.1 Answer Failure 阶段

| 指标 | 数值 |
|------|------|
| 处理总数 | 688 |
| 成功完成 | 521 |
| 模型错误 | 167 |
| **ANSWER_FAILURE（阳性）** | **113** |
| NO_ANSWER_FAILURE（阴性） | 575 |
| 生成 repair package | 否（record-only） |

### 2.2 Access Failure 阶段

| 指标 | 数值 |
|------|------|
| 处理总数 | 688 |
| 成功完成 | 516 |
| 模型错误 | 172 |
| **ACCESS_FAILURE（阳性）** | **130** |
| NO_ACCESS_FAILURE（阴性） | 558 |
| **修复包数量** | **111** |

### 2.3 Cons Failure 阶段

| 指标 | 数值 |
|------|------|
| 处理总数 | 688 |
| 成功完成 | 340 |
| 模型错误 | 183 |
| 数据错误 | 165 |
| **CONS_FAILURE（阳性）** | **229** |
| NO_CONS_FAILURE（阴性） | 459 |
| **修复包数量** | **224** |
| Stage-A 筛选候选数 | 213 |
| Stage-B 回溯存在 | 224/224 |

---

## 3. Cons Failure 子类型分布

| 子类型 | 数量 | 说明 |
|--------|------|------|
| `extraction` | 202 | 原始消息中有信息但未被提取为记忆候选 |
| `decision` | 15 | 候选存在但决策阶段被错误拒绝 |
| `update` | 4 | 记忆更新错误（含 before/after 版本） |
| `ingestion` | 3 | 消息未能进入 construction 流水线 |

---

## 4. 结构完整性确认

### 4.1 目录布局

```
outputs/diagnosis/deepseek_v4_pro_diag_v3/
├── answer_failure/
│   ├── answer_failures.jsonl    ✅ 113 条阳性记录
│   ├── progress.jsonl           ✅ 688 条
│   ├── errors.jsonl             ✅ 167 条模型错误
│   ├── summary.json             ✅
│   └── manifest.json            ✅
├── access_failure/
│   ├── packages/                ✅ 111 个修复包
│   ├── progress.jsonl           ✅ 688 条
│   ├── errors.jsonl             ✅ 172 条错误
│   ├── summary.json             ✅
│   └── manifest.json            ✅
└── cons_failure/
    ├── packages/                ✅ 224 个修复包
    ├── progress.jsonl           ✅ 688 条
    ├── errors.jsonl             ✅ 183 条错误
    ├── summary.json             ✅
    └── manifest.json            ✅
```

### 4.2 关键确认

| 检查项 | 结果 |
|--------|------|
| Answer 无 `packages` 目录 | ✅ 通过 |
| Answer 无 Skill 路由 | ✅ 通过 |
| Access 无原始消息文本 | ✅ 通过（5/5 抽样） |
| Access 无历史版本/父链接/before-after | ✅ 通过（5/5 抽样） |
| Access 无生成查询/关键词/过滤条件 | ✅ 通过（5/5 抽样） |
| Cons 无运行时搜索结果 | ✅ 通过（5/5 抽样） |
| Cons Stage-A 筛选保留 | ✅ 通过（5/5 抽样） |
| Cons Stage-B 回溯存在 | ✅ 通过（224/224） |
| Cons 原始证据支持参考答案 | ✅ 通过（5/5 抽样） |
| Cons 仅选择最早错误 | ✅ 通过（5/5 抽样） |
| Cons 更新错误含 before/after 版本 | ✅ 通过 |
| Cons 原因字段完整（事实、期望、首错、影响） | ✅ 通过 |
| 无 `skills` 目录 | ✅ 通过 |
| 无 `combined` 目录 | ✅ 通过 |
| 无 `both_failures` 目录 | ✅ 通过 |

---

## 5. 质量审计抽样

### 5.1 Access 修复包抽样（5 个）

| 文件 | 判定 | 备注 |
|------|------|------|
| `conv-48_qa_0048_access_failure.json` | ✅ | 仅含当前版本 ID，无原始消息，无历史数据 |
| `conv-49_qa_0082_access_failure.json` | ✅ | 结构完整，`missing_useful_current_version_ids` 正确识别 |
| `conv-42_qa_0055_access_failure.json` | ✅ | 9 条 claims，缺失分析准确 |
| `conv-43_qa_0030_access_failure.json` | ✅ | 4 条 claims，部分命中部分缺失 |
| `conv-49_qa_0123_access_failure.json` | ✅ | 单 claim，缺失 ID 清晰标注 |

### 5.2 Cons 修复包抽样（5 个）

| 文件 | 判定 | 子类型 | 备注 |
|------|------|--------|------|
| `conv-48_qa_0072_cons_failure.json` | ✅ | `ingestion` | Stage-A 正确识别缺失，Stage-B 回溯至 commit 24 |
| `conv-48_qa_0006_cons_failure.json` | ✅ | `extraction` | 双消息源，首错定位在 extraction 阶段 |
| `conv-43_qa_0171_cons_failure.json` | ✅ | `extraction` | 单消息源，证据充足 |
| `conv-43_qa_0065_cons_failure.json` | ✅ | `extraction` | 多 claim 复杂场景，Star Wars 缺失分析准确 |
| `conv-30_qa_0056_cons_failure.json` | ✅ | `extraction` | 隐喻表述（dancing）的提取遗漏，分析合理 |

---

## 6. 运行配置与并发

| 配置项 | 值 |
|--------|-----|
| Smoke 测试 workers | 1（单线程验证） |
| Answer 阶段 workers | 8 |
| Access 阶段 workers | 4 |
| Cons 阶段 workers | 4 |
| Access + Cons 并行峰值 | 8（各 4 workers 同时运行） |
| 单 worker 最大重试次数 | 3 |
| DeepSeek API 端点 | `https://api.deepseek.com` |

---

## 7. 重试与恢复

- 本次运行无中断，未使用 `--resume` 功能
- Answer 阶段：167 次模型错误均通过内置 3 次重试机制处理
- Access 阶段：172 次模型错误均通过重试处理
- Cons 阶段：183 次模型错误 + 165 次数据错误均通过重试处理
- 无人工干预恢复操作

---

## 8. 时间线

| 阶段 | 开始 | 结束 | 耗时 |
|------|------|------|------|
| Smoke 测试 | 17:18 | 17:21 | ~3 分钟 |
| Answer 阶段 | 17:22 | 18:41 | ~79 分钟 |
| Access 阶段 | 18:51 | 20:25 | ~94 分钟 |
| Cons 阶段 | 18:51 | 20:47 | ~116 分钟 |

> 注：Access 与 Cons 并行运行（18:51 同时启动），Cons 因两阶段流水线耗时更长。

---

## 9. 总结

本次诊断运行使用 DeepSeek V4 Pro 对 688 个 Judge P/I 项进行了三阶段诊断，所有阶段均成功完成。关键发现：

- **Answer Failure**：113 例（16.4%），系统纯记录，不产生修复包
- **Access Failure**：130 例（18.9%），生成 111 个修复包——Access 阶段的检索未找到全部支撑当前记忆版本
- **Cons Failure**：229 例（33.3%），生成 224 个修复包——Construction 阶段以提取遗漏（extraction）为主（202/229，88.2%）

输出结构、数据清洁度、跨组件隔离性均通过完整审计。未生成 Skill、combined 包或交叉污染。
