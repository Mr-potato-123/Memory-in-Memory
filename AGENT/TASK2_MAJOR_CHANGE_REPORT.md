# TASK2 新版重构与重大改动报告

## 结论

`single_agent_mim` 已完全收敛到新版架构：SQLite 事实记忆、四 Agent、三个工作流、Failure 确定性溯源、三字段 Skill 和版本化 SkillRepository。旧 JSON Memory、旧 replay、旧 SkillBank 写接口及旧单 Prompt 已移除，不再维护双实现。!
## 为什么必须做较大改动

复核时发现旧代码虽然旧测试通过，但 train/use/evaluate 实际使用不同存储和不同 Agent 契约：

- use/evaluate 开始使用 SQLite，新 train 仍引用 JSON Memory；
- Runtime Skill 读取与 Maintenance Skill 写入不是同一格式；
- Failure 新架构存在但未接进 train；
- Skill-Maker staging/replay 存在但未接进真实 Runtime；
- SQLite connection 正常路径没有 commit；
- Construction ADD 曾可能把 decision reason 当成 Memory content；
- Access 没有持久化最终答案真正可见的 Memory；
- LoCoMo `dia_id` 跨 sample 重复，会破坏全局主键和溯源；
- provenance 返回结构中相关 Memory version 集合实际为空；
- FTS 命中后使用了已经关闭的 connection；
- 回归检查是固定 `True` 占位。

这些问题不能通过小修旧接口解决，否则会长期保留两套真相源。因此按用户确认“完全按照新版”执行了收敛式重构。

## 已删除的旧路径

- `src/mim/memory.py`
- `src/mim/replay.py`
- `src/mim/retrieval.py`
- `src/mim/retrieval/legacy.py`
- `prompts/construction.md`
- `prompts/failure.md`
- `prompts/skill_maker.md`
- schemas 中旧 `FailureCase/FailureAttribution/SkillDraft/SkillCandidate/ReplayResult`
- Failure/Skill-Maker Agent 的旧兼容方法

删除是为了防止代码人员误用，不是简单清理文件。

## 新的唯一实现

- Memory：`src/mim/storage/sqlite_store.py`
- Retrieval：`src/mim/retrieval/`
- Runtime Skill 视图：`src/mim/skills.py`
- Skill CRUD/版本：`src/mim/skill_maker/repository.py`
- use/train/evaluate：`src/mim/workflows/`
- Failure：`src/mim/failure/`
- 四个 Agent：`src/mim/agents/`

## 关键修复

1. SQLite 每个 context manager 成功后 commit、异常 rollback。
2. Construction Plan 原子保存 input/candidate/decision/version/provenance/change event。
3. ADD/UPDATE/MERGE 使用 Candidate 的真实结构、内容和 embedding。
4. UPDATE/MERGE 限定同 conversation，防止跨会话目标污染。
5. Access 保存 exact action、hits、visible context、prompt hash、final evidence。
6. LoCoMo message/evidence ID 改为 `<conversation_id>:<dia_id>`。
7. Failure Report 增加 source trace、structural break、version diff、stage outputs。
8. Failure 第一阶段先用同一份可见 Memory 做强模型充分性判断和 blind re-answer。
9. Skill-Maker 自然检索使用 Runtime 同一个 Skill retriever。
10. candidate staging 自动清理；初稿与每次 revision 落盘。
11. Access/Construction 都执行真实回归重放，不再固定通过。
12. validation 比较 Bank 版本，test 冻结 selected Bank。
13. GPT/Qwen 走 OpenAI-compatible client；Claude 可走 Anthropic client；测试走 Mock。

## 验证

执行结果：

```text
python -m compileall -q src tests    PASS
python -m pytest -q                  20 passed
python main.py smoke ...             PASS
```

测试覆盖：

- SQLite ADD/UPDATE/历史版本；
- 消息继承 lineage 与 change event；
- plan 失败整体回滚；
- FTS 搜索；
- Skill create/update/side/staging/retrieval/validator；
- Access exact answer context；
- Failure `memory_to_retrieval` 首断点；
- Failure S0-S8 stage outputs；
- LoCoMo ID 全局唯一；
- Skill-Maker forced/natural/retrieval/regression/publish。
- train/use/evaluate 三个接口均使用同一 SQLite Runtime。

## 风险与后续建议

- 当前是串行评测版，不宣称生产并发能力；
- 多跳 gold evidence 暂按一个 evidence unit，后续可拆 claim；
- 回归 case bundle 当前在训练进程内，跨进程恢复可后续实现；
- embedding 仍保存在 SQLite BLOB，数据规模放大后可抽换向量层；
- 外部真实模型完整训练会产生调用成本，本次使用 Mock 做确定性工程验收。

详细开发交接见 `single_agent_mim/docs/ARCHITECTURE_AND_HANDOFF.md`。
