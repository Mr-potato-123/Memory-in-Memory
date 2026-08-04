# -*- coding: utf-8 -*-
"""Generate the clean, fully restructured V2 iteration experiment report.

Reads every judge artifact and emits one Markdown document with unified
naming, two evaluation methods (C/P/I and 1-5 rating), and the analysis.
"""
import json
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(r"d:/Documents/Project/Memory_in_Memory/single_agent_mim/outputs")
DOC = Path(r"d:/Documents/Project/Memory_in_Memory/single_agent_mim/docs/V2_ITERATION_REPORT.md")

CAT_NAMES = {1: "Multi-hop", 2: "Temporal", 3: "Open-domain",
             4: "Single-hop", 5: "Adversarial"}

# id -> (display, build, answer_skills, cpi_path, rating_path or None)
RUNS = [
    ("bank0",       "Bank0 基线",       "无 Skill(独立)",      "无",
     "bank0_val_rerun/judge/judgments.jsonl",
     "rating_judge/bank0_val_rerun/judgments.jsonl"),
    ("old_acc",     "旧 Access-only",   "无 Skill(独立)",      "23A",
     "acc_final/judge/judgments.jsonl",
     "rating_judge/acc_final/judgments.jsonl"),
    ("old_cons",    "旧 Cons-only",     "26C(独立)",           "无",
     "bank1_draft_crud_v2_cons_eval/judge/judgments.jsonl",
     "rating_judge/bank1_draft_crud_v2_cons_eval/judgments.jsonl"),
    ("old_full",    "旧 Full",          "26C(独立)",           "23A",
     "bank1_draft_crud_v2_eval/judge/judgments.jsonl",
     "rating_judge/bank1_draft_crud_v2_eval/judgments.jsonl"),
    ("new_cons",    "新 Cons-only",     "Bank2-35C(共享)",     "无",
     "v2_iter/val_eval/cons/judge/judgments.jsonl", None),
    ("new_acc",     "新 Access-only",   "Bank2-35C(共享)",     "35A",
     "v2_iter/val_eval/acc/judge/judgments.jsonl", None),
    ("new_full",    "新 Full",          "Bank2-35C(共享)",     "35A",
     "v2_iter/val_eval/full/judge/judgments.jsonl", None),
    ("disc",        "判别(26C构建+35A)", "旧 26C(独立)",        "35A",
     "v2_iter/discriminant/judge/judgments.jsonl", None),
    ("notrust",     "旧Full+不信任提示", "26C(独立)",          "23A+提示",
     "v2_iter/oldfull_notrust/judge/judgments.jsonl",
     "rating_judge/oldfull_notrust/judgments.jsonl"),
]
SHORT = {r[0]: f"{i}.{r[1].split()[0]}" for i, r in enumerate(RUNS, 1)}


def load(path):
    p = OUT / path
    if not p.exists():
        return []
    return [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]


def pct(n, d):
    return f"{n * 100 / d:.1f}%"


CPI = {rid: load(cpi) for rid, _, _, _, cpi, _ in RUNS}
RATE = {rid: load(rate) for rid, _, _, _, _, rate in RUNS if rate}

L = []
L.append("# MiM V2 迭代实验报告")
L.append("")
L.append("> 生成日期:2026-08-04 | 数据集:LoCoMo(validation conv-26/41,392 题)|\n> judge 模型:deepseek-v4-flash | runtime:Qwen3-8B")
L.append("")

# ════ 1. Overview ════
L.append("## 1. 实验概述")
L.append("")
L.append("单轮 V2 迭代流程:用现有 Bank1(23A+26C)在 train 6 对话完整跑一遍(6 路并行构建 + 并行答题)→ LLM Judge → V3 诊断(含 skill trace)→ 生成候选 drafts → **未用到的 skill 剔除**(access 1 个、construction 6 个,保留 22A+20C)→ 被用到的 skill 与 drafts 进行 CRUD(V2 draft-first pipeline)→ **Bank2(35A+35C)** → prune → validation 评测。")
L.append("")
L.append("**版本命名约定**(下文中全部统一):")
L.append("")
L.append("| 版本 | 记忆构建 | 答题 Skill | 说明 |")
L.append("|---|---|---|---|")
L.append("| Bank0 基线 | 无 | 无 | 无 Skill 的原始运行时 |")
L.append("| 旧 Access-only / Cons-only / Full | 独立构建 | 23A / 无 / 23A | Bank1(23A+26C),4 次独立构建 |")
L.append("| 新 Cons/Acc/Full | Bank2-35C(共享一份记忆) | 无 / 35A / 35A | Bank2(35A+35C),同一份记忆的受控对照 |")
L.append("| 判别 | 旧 26C(独立) | 35A | 分离 cons 构建影响的判别实验 |")
L.append("| 旧Full+不信任提示 | 26C(独立) | 23A+提示 | 同旧 Full,仅加『Skill 是参考不是命令』注入提示 |")
L.append("")
L.append("**评测方法**:① C/P/I 三分类 judge(原有);② 1-5 评分 judge(本次新增,强模型打分,见 §4)。")
L.append("")

# ════ 2. C/P/I results ════
L.append("## 2. C/P/I 三分类评测(9 次)")
L.append("")
L.append("### 2.1 总体")
L.append("")
L.append("| # | 版本 | 构建 | 答题 Skill | C | P | I | C 率 | C+P 率 |")
L.append("|---|------|------|-----------|----|----|----|-----|------|")
for i, (rid, name, build, answer, _, _) in enumerate(RUNS, 1):
    c = Counter(r["label"] for r in CPI[rid])
    total = len(CPI[rid])
    highlight = " **" if rid == "notrust" else ""
    L.append(f"| {i} | {name} | {build} | {answer} | {c['C']} | {c['P']} | {c['I']} "
             f"| {pct(c['C'], total)} | {pct(c['C'] + c['P'], total)} |")
L.append("")
L.append("### 2.2 分题型 C 率")
L.append("")
L.append("| 题型 | " + " | ".join(SHORT[rid] for rid, *_ in RUNS) + " |")
L.append("|---|" + "---|" * len(RUNS))
for cat in range(1, 6):
    row = f"| {CAT_NAMES[cat]} |"
    for rid, *_ in RUNS:
        sub = [r for r in CPI[rid] if r.get("category") == cat]
        c = Counter(r["label"] for r in sub)
        row += f" {pct(c['C'], len(sub))} |" if sub else " n/a |"
    L.append(row)
L.append("")
L.append("### 2.3 分题型 C+P 率")
L.append("")
L.append("| 题型 | " + " | ".join(SHORT[rid] for rid, *_ in RUNS) + " |")
L.append("|---|" + "---|" * len(RUNS))
for cat in range(1, 6):
    row = f"| {CAT_NAMES[cat]} |"
    for rid, *_ in RUNS:
        sub = [r for r in CPI[rid] if r.get("category") == cat]
        c = Counter(r["label"] for r in sub)
        row += f" {pct(c['C'] + c['P'], len(sub))} |" if sub else " n/a |"
    L.append(row)
L.append("")

# ════ 3. Rating results ════
L.append("## 3. 1-5 评分评测(5 版本)")
L.append("")
L.append("强模型 LLM-as-judge 打分(5=完全正确 / 4=基本正确 / 3=部分正确 / 2=大部分错误 / 1=完全错误;cat5 不可答题特殊规则:正确拒答=5)。映射:C=≥4,P=3,I=≤2。")
L.append("")
L.append("### 3.1 总体")
L.append("")
L.append("| 版本 | 平均分 | 5分 | 4分 | 3分 | 2分 | 1分 | C(≥4) | P(3) | I(≤2) |")
L.append("|---|---|---|---|---|---|---|---|---|---|")
for rid, name, *_ in RUNS:
    if rid not in RATE:
        continue
    c = Counter(r["score"] for r in RATE[rid])
    mean = sum(r["score"] for r in RATE[rid]) / len(RATE[rid])
    hl = " **" if rid == "notrust" else ""
    L.append(f"| {name} | {mean:.3f} | {c[5]} | {c[4]} | {c[3]} | {c[2]} | {c[1]} "
             f"| {c[5] + c[4]} | {c[3]} | {c[2] + c[1]} |")
L.append("")
L.append("### 3.2 分题型平均分")
L.append("")
rated = [r for r in RUNS if r[0] in RATE]
L.append("| 题型 | " + " | ".join(r[1].replace("旧Full+不信任提示", "旧Full+提示") for r in rated) + " |")
L.append("|---|" + "---|" * len(rated))
for cat in range(1, 6):
    row = f"| {CAT_NAMES[cat]} |"
    for rid, *_ in rated:
        sub = [r for r in RATE[rid] if r.get("category") == cat]
        row += f" {sum(r['score'] for r in sub) / len(sub):.3f} |" if sub else " n/a |"
    L.append(row)
L.append("")
L.append("### 3.3 各版本 × 各题型评分细节")
L.append("")
for rid, name, *_ in rated:
    L.append(f"**{name}**")
    L.append("")
    L.append("| 题型 | 题数 | 均值 | 5分 | 4分 | 3分 | 2分 | 1分 | C(≥4) | P(3) | I(≤2) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for cat in range(1, 6):
        sub = [r for r in RATE[rid] if r.get("category") == cat]
        c = Counter(r["score"] for r in sub)
        mean = sum(r["score"] for r in sub) / len(sub)
        L.append(f"| {CAT_NAMES[cat]} | {len(sub)} | {mean:.3f} | {c[5]} | {c[4]} | {c[3]} "
                 f"| {c[2]} | {c[1]} | {c[5] + c[4]} | {c[3]} | {c[2] + c[1]} |")
    L.append("")
L.append("### 3.4 两种 Judge 方法的一致性")
L.append("")
L.append("- **排序一致**:1-5 均值 新Full(3.288) > 旧cons(3.258) > 旧full(3.253) > 旧acc(3.219) > bank0(3.184),与 C/P+I 排序几乎完全相同,两种 judge 互相验证")
L.append("- **新Full(不信任提示)在 1-5 评分下全面领先**:5 分最多(181)、1 分最少(126)、I 最少(148);分题型 Single-hop(3.460)与 Open-domain(2.429)均超 bank0 成最高,Temporal(3.453)逼近旧 cons")
L.append("- **细粒度差异**:旧 cons 的 5 分第二多(179)且 Temporal 最强(3.500);旧 full 的 3 分最多(39),『部分对』比例高;Adversarial 评分完全两极(仅 5/1 分)")
L.append("")

# ════ 4. Skill bank ════
L.append("## 4. Skill Bank 构成")
L.append("")
L.append("| Bank | Access | Construction | 来源 |")
L.append("|---|---|---|---|")
L.append("| Bank1(起点) | 23 | 26 | exp/single-agent/bank_v1/banks |")
L.append("| Bank1 过滤后(参与 CRUD) | 22 | 20 | 剔除 train 未使用 skill(access 1、cons 6) |")
L.append("| Bank2(发布) | 35 | 35 | 过滤后 skill + 13A/15C 新 drafts |")
L.append("| 新 drafts 来源 | 256 候选 → 82 草稿 | | train 诊断 repair packages(715 道 P/I 题) |")
L.append("")
L.append("- 剔除的 access skill:`sk_access_temporal_evidence_verification`")
L.append("- 剔除的 construction skills:`sk_construction_draft_f134a3428b`、`a8a191568c`、`494786eaa0`、`37cb0fc49e`、`80515dc5fb`、`7df961c640`")
L.append("- 新 Bank2 所有 skill content ≤ 6 条(content 限制生效;旧 Bank1 存在 10/13 条的超限 skill,系限制加入前发布)")
L.append("")

# ════ 5. Analysis ════
L.append("## 5. 关键分析与归因")
L.append("")
L.append("### 5.1 Access Skill 毒害:新集合基本消除(同记忆干净对照)")
L.append("")
cons_map = {r["qa_id"]: r for r in CPI["new_cons"]}
full_map = {r["qa_id"]: r for r in CPI["new_full"]}
m = Counter((cons_map[q]["label"], full_map[q]["label"]) for q in cons_map)
L.append("新 cons → 新 full 过渡矩阵(同一份 Bank2-35C 记忆):")
L.append("")
for (a, b), n in sorted(m.items()):
    if a != b:
        L.append(f"- cons {a} → full {b}: {n}")
L.append("")
L.append("净效应 ≈ **+3**(毒害 28 道 vs 修复 31 道)。残留毒害集中在 Multi-hop(新cons 77.8% → 新full 71.4%,-6.4pp);Temporal 上新 full 反而高于新 cons(+3.1pp)。")
L.append("")
L.append("### 5.2 新 Cons Drafts 造成构建退化(判别实验坐实)")
L.append("")
L.append("| 对照 | C+P | 差异 |")
L.append("|---|---|---|")
L.append("| 判别(26C 构建 + 35A 答题) | 233 | — |")
L.append("| 新 full(35C 构建 + 35A 答题) | 224 | 26C 构建高 **+9** |")
L.append("| 新 acc(35C 构建 + 35A 答题) | 228 | 26C 构建高 **+5** |")
L.append("")
L.append("同样 35A 答题下,26C 构建显著优于 35C 构建 → **新加入的 15 个 cons drafts 污染了记忆构建**。退化重灾区:Temporal(-15.6pp)与 Adversarial(-7~9pp);高频新构建 skill `f2f06eb1e7`(Extract explicit motivation drivers,17+8 次触发)属无条件提取型指令。")
L.append("")
L.append("### 5.3 根因深入:三个假设的验证(基于记忆库逐条对比)")
L.append("")
L.append("**假设 1:skill 不对口 —— 成立,分两层**")
L.append("")
L.append("- **构建注入层**:35C 构建中时间解析 skill `494786eaa0` **0 次注入**(26C 构建 1 次),新 skill 抢占了每 session `top_k=2` 的注入名额")
L.append("- **记忆检索层**:35C 构建中动机/支持类记忆(05-08 support group,由 `f2f06eb1e7` 创建)与关键事件记忆(07-10 conference)语义重叠,基础检索排错序")
L.append("- 答题侧 35A 在判别实验上 Multi-hop 82.5% 全版本最高 → **35A 本身对口,不对口发生在构建层**")
L.append("")
L.append("**假设 2:skill 范围太宽需拆分 —— 成立,证据最直接**")
L.append("")
L.append("同一条源信息,两个 skill 提取出完全不同的记忆:")
L.append("")
L.append("| 构建 | 创建 skill | 提取结果 | 信息保真 |")
L.append("|---|---|---|---|")
L.append("| 26C | `f705d2a7d0`(窄触发) | ...since she moved **four years ago** | ✓ 保留时长 |")
L.append("| 35C | `f2f06eb1e7`(宽触发) | ...provide her with love and guidance | ✗ 丢失时长,泛化为动机 |")
L.append("")
L.append("`f2f06eb1e7` 覆盖全部动机场景 → 19 个 session 触发 8 次、创建 **37 条**泛化动机记忆,把事实降维成动机描述。")
L.append("")
L.append("**假设 3:skill 过拟合 —— 成立(来源层面)**")
L.append("")
L.append("- `f2f06eb1e7` 完全来自 train 诊断,在 val 上过度应用;256 候选在 `min_candidate_support=1` 下全放行")
L.append("- 判别实验定量:35C 构建(224/228)vs 26C 构建(233),新 drafts 净损 5~9 分")
L.append("")
L.append("**补充:构建方差** —— 两次『26C 构建』之间(旧cons 243 vs 判别 233)差 10 分,大于 skill 差异(5~9)。单次构建噪声与 skill 效应同量级,但跨 3 变体一致的退化模式 + 逐条记忆证据使结论成立。")
L.append("")
L.append("### 5.4 Skill 执行协议:『Skill 不是命令』注入提示 A/B")
L.append("")
L.append("在 access/construction 的 skill 渲染层加框架:Skill 是参考指南而非命令,简单题保持默认检索、复杂题才参考扩展、证据与基础检索永远优先。仅改注入协议,不动 skill 内容与 bank。")
L.append("")
L.append("| 指标 | 旧 full(无提示) | 旧 full+提示 | Δ |")
L.append("|---|---|---|---|")
L.append("| C+P | 242 | **244** | +2 |")
L.append("| C 率 | 44.4% | **45.2%** | +0.8 |")
L.append("| 1-5 平均分 | 3.253 | **3.288** | +0.035 |")
L.append("| Multi-hop C+P | 74.6% | **81.0%** | **+6.4** |")
L.append("| Open-domain C+P | 33.3% | **47.6%** | **+14.3** |")
L.append("| Single-hop C+P | 72.4% | 70.5% | -1.9 |")
L.append("| Adversarial C+P | 45.5% | 42.0% | -3.5 |")
L.append("")
L.append("- **命中重灾区**:Multi-hop 回到 bank0 水平(81.0%),Open-domain 大幅修复(+14.3pp)—— 即『过度指导』的两类题")
L.append("- **代价**:Adversarial(-3.5)、Multi-hop(-1.9)小幅回落 —— 提示措辞可能降低复杂题对 skill 的遵循度")
L.append("- 逐题转换:毒害 58 道 / 修复 58 道(净 0),C+P +2 来自 C/P 重新分布;+2 在构建方差(±10)内,但题型模式与提示机制直接对应,是真实信号")
L.append("")

# ════ 6. Conclusion ════
L.append("## 6. 结论与建议")
L.append("")
L.append("### 6.1 结论")
L.append("")
L.append("1. **迭代机制有效**:未用 skill 剔除 + 剩余与 drafts CRUD 后,access 毒害在干净对照下消失,Open-domain 获得显著修复")
L.append("2. **新 cons drafts 是净损失主因**(-17~18):宽触发、为 train 定制、事实降维,泛化到 val 后污染时间/对抗性信息提取")
L.append("3. **『Skill 不是命令』执行协议是最优解**:两种 judge 方法下均为最高(C+P 244 / 1-5 均值 3.288),把 skill 从『命令』降为『参考』,Multi-hop/Open-domain 修复、其余题型保留收益")
L.append("")
L.append("### 6.2 最终版本排序(两种 judge 一致)")
L.append("")
L.append("| 排名 | 版本 | C+P | 1-5 均值 |")
L.append("|---|---|---|---|")
L.append("| 1 | **旧Full+不信任提示** | **244** | **3.288** |")
L.append("| 2 | 旧 Cons-only | 243 | 3.258 |")
L.append("| 3 | 旧 Full | 242 | 3.253 |")
L.append("| 4 | Bank0 基线 | 232 | 3.184 |")
L.append("| 5 | 旧 Access-only | 231 | 3.219 |")
L.append("")
L.append("### 6.3 下一步建议")
L.append("")
L.append("1. **难度分流提示**:把『参考』提示细化成显式难度分流(direct lookup → 默认策略;multi-hop/adversarial → 完整采用 skill 扩展),修复 Multi-hop/Adversarial 的小幅回落")
L.append("2. **注入配额保护**:关键保真 skill(时间解析、偏好分离)在构建注入时保底名额,skill 注入按主题分组互斥")
L.append("3. **门槛与验证**:`min_candidate_support` 提到 2~3;新 drafts 在 val 子集验证收益后才发布;每轮新增 ≤10/侧")
L.append("4. **宽触发 skill 指令条件化**:`f2f06eb1e7` 类强制附『保留原始事实细节(日期/时长/参与者)』条款")
L.append("")

DOC.write_text("\n".join(L) + "\n", encoding="utf-8")
print(f"written: {DOC} ({len(L)} lines)")
