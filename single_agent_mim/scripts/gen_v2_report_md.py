# -*- coding: utf-8 -*-
"""Generate the V2 iteration experiment report as Markdown from judge artifacts."""
import json
from collections import Counter
from pathlib import Path

OUT = Path(r"d:/Documents/Project/Memory_in_Memory/single_agent_mim/outputs")
DOC = Path(r"d:/Documents/Project/Memory_in_Memory/single_agent_mim/docs/V2_ITERATION_REPORT.md")

# (id, display name, memory build, answer skills, judgments path)
RUNS = [
    ("bank0",      "Bank0 基线",        "无 Skill(独立构建)",     "无",        "bank0_val_rerun/judge/judgments.jsonl"),
    ("old_acc",    "旧 Access-only",    "无 Skill(独立构建)",     "23A",       "acc_final/judge/judgments.jsonl"),
    ("old_cons",   "旧 Cons-only",      "26C(独立构建)",          "无",        "bank1_draft_crud_v2_cons_eval/judge/judgments.jsonl"),
    ("old_full",   "旧 Full",           "26C(独立构建)",          "23A",       "bank1_draft_crud_v2_eval/judge/judgments.jsonl"),
    ("new_cons",   "新 Cons-only",      "Bank2-35C(共享)",        "无",        "v2_iter/val_eval/cons/judge/judgments.jsonl"),
    ("new_acc",    "新 Access-only",    "Bank2-35C(共享)",        "35A",       "v2_iter/val_eval/acc/judge/judgments.jsonl"),
    ("new_full",   "新 Full",           "Bank2-35C(共享)",        "35A",       "v2_iter/val_eval/full/judge/judgments.jsonl"),
    ("discriminant", "判别(26C构建+35A答题)", "旧 26C(独立构建)",  "35A",       "v2_iter/discriminant/judge/judgments.jsonl"),
]
CAT_NAMES = {1: "Single-hop", 2: "Temporal", 3: "Open-domain", 4: "Multi-hop", 5: "Adversarial"}

def load(path):
    return [json.loads(l) for l in OUT.joinpath(path).read_text(encoding="utf-8").splitlines()]

data = {rid: load(p) for rid, _, _, _, p in RUNS}

def pct(n, d):
    return f"{n * 100 / d:.1f}%"

lines = []
lines.append("# MiM V2 迭代实验报告\n")
lines.append(f"生成日期:2026-08-04\n")
lines.append("## 实验概述\n")
lines.append("单轮 V2 迭代流程:用现有 Bank1(23A+26C)在 train 6 对话(conv-30/42/43/44/48/49)完整跑一遍(6 路并行构建 + 并行答题)→ LLM Judge → V3 诊断(含 skill trace)→ 生成候选 drafts → **未用到的 skill 剔除**(access 1 个、construction 6 个,保留 22A+20C)→ 被用到的 skill 与 drafts 进行 CRUD(V2 draft-first pipeline)→ **Bank2(35A+35C)** → prune → validation 三变体评测。\n")
lines.append("主要流程脚本:`scripts/run_mim_v2_single.py`、`scripts/run_train_iter.py`、`scripts/run_candidates_from_diagnosis.py`、`scripts/prune_memory.py`。产物:`outputs/v2_iter/`。\n")
lines.append("> 注:旧四变体为各自独立构建(记忆不同),新三变体共享同一份 Bank2 构建并 prune 的记忆(受控对照)。判别实验为 26C 构建 + 35A 答题,用于分离 cons 构建影响。\n")

# ── Overall table ──
lines.append("## 总体结果(C 率 / C+P 率)\n")
lines.append("| # | 评测 | 记忆构建 | 答题 Skill | C | P | I | C 率 | C+P 率 |")
lines.append("|---|------|----------|------------|----|----|----|-----|------|")
for i, (rid, name, build, answer, _) in enumerate(RUNS, 1):
    rows_ = data[rid]
    c = Counter(r["label"] for r in rows_)
    total = len(rows_)
    lines.append(f"| {i} | {name} | {build} | {answer} | {c['C']} | {c['P']} | {c['I']} | {pct(c['C'], total)} | {pct(c['C']+c['P'], total)} |")

# ── Per category ──
def cat_rows(rows_, cat):
    return [r for r in rows_ if r.get("category") == cat]

SHORT = {"bank0": "bank0", "old_acc": "旧acc", "old_cons": "旧cons",
         "old_full": "旧full", "new_cons": "新cons", "new_acc": "新acc",
         "new_full": "新full", "discriminant": "判别"}
for metric, header in (("C", "C 率"), ("C+P", "C+P 率")):
    lines.append(f"\n## 分题型 {header}\n")
    lines.append(f"| 题型 | 题数 | " + " | ".join(SHORT[rid] for rid, _, _, _, _ in RUNS) + " |")
    lines.append("|---|" + "---|" * (len(RUNS) + 1))
    for cat in range(1, 6):
        total = len(cat_rows(data["bank0"], cat))
        row = f"| {CAT_NAMES[cat]} | {total} |"
        for rid, _, _, _, _ in RUNS:
            sub = cat_rows(data[rid], cat)
            if not sub:
                row += " n/a |"
                continue
            c = Counter(r["label"] for r in sub)
            n = c["C"] + c["P"] if metric == "C+P" else c["C"]
            row += f" {pct(n, len(sub))} |"
        lines.append(row)

# ── Skill usage summary ──
lines.append("\n## Skill Bank 构成\n")
lines.append("| Bank | Access | Construction | 来源 |")
lines.append("|---|---|---|---|")
lines.append("| Bank1(起点) | 23 | 26 | exp/single-agent/bank1_draft_crud_v2/banks |")
lines.append("| Bank1 过滤后(参与 CRUD) | 22 | 20 | 剔除 train 未使用 skill(access 1、cons 6) |")
lines.append("| Bank2(发布) | 35 | 35 | 过滤后 skill + 13A/15C 新 drafts |")
lines.append("| 新 drafts 来源 | 256 候选 → 82 草稿 | | train 诊断 repair packages(715 道 P/I 题) |")
lines.append("")
lines.append("剔除的 access skill:`sk_access_temporal_evidence_verification`")
lines.append("")
lines.append("剔除的 construction skills:`sk_construction_draft_f134a3428b`、`a8a191568c`、`494786eaa0`、`37cb0fc49e`、`80515dc5fb`、`7df961c640`\n")

# ── Analysis ──
lines.append("## 关键分析\n")
lines.append("### 1. Access Skill 毒害:新集合基本消除(同记忆干净对照)\n")
cons_map = {r["qa_id"]: r for r in data["new_cons"]}
full_map = {r["qa_id"]: r for r in data["new_full"]}
m = Counter((cons_map[q]["label"], full_map[q]["label"]) for q in cons_map)
lines.append("新 cons → 新 full 过渡矩阵(同一份记忆):\n")
for (a, b), n in sorted(m.items()):
    if a != b:
        lines.append(f"- cons {a} → full {b}: {n}")
lines.append("")
lines.append("净效应 ≈ **+3**(毒害 28 道 vs 修复 31 道)。对比旧实验(独立构建,39 毒害 / 38 修复),本轮干净对照下 access 注入几乎无净伤害。残留毒害集中在 **Single-hop(-6.4pp)**;Temporal 上新 full 反而高于新 cons(+3.1pp)。\n")
lines.append("### 2. 新 Cons Drafts 造成构建退化(判别实验坐实)\n")
lines.append("| 对照 | C+P | 差异 |")
lines.append("|---|---|---|")
lines.append("| 判别(26C 构建 + 35A 答题) | 233 | — |")
lines.append("| 新 full(35C 构建 + 35A 答题) | 224 | 26C 构建高 **+9** |")
lines.append("| 新 acc(35C 构建 + 35A 答题) | 228 | 26C 构建高 **+5** |")
lines.append("")
lines.append("同样 35A 答题下,26C 构建显著优于 35C 构建 → **新加入的 15 个 cons drafts 污染了记忆构建**。退化重灾区:Temporal(-15.6pp,59.4→43.8)与 Adversarial(-7~9pp);而 Single-hop 上新 cons 构建反而改善(+3.2pp)。高频新构建 skill:`f2f06eb1e7`(Extract explicit motivation drivers,17+8 次触发),属无条件提取型指令。\n")
lines.append("### 3. 新 Access Drafts 的收益\n")
lines.append("- **Open-domain**:新 full 42.9% vs 旧 full 33.3%(**+9.6pp**),新 acc 47.6% vs 旧 42.9%(+4.7pp)")
lines.append("- **Multi-hop**:持平(69~72%)")
lines.append("- 13 个新 access drafts 主要修复开放域/间接证据类问题\n")
lines.append("### 4. 其他\n")
lines.append("- **Memory pruning**:train prune 后未影响评测(val 记忆全部被引用,删 0 条)")
lines.append("- **Skill content 限制**:新 Bank2 所有 skill content ≤ 6 条(旧 Bank1 有 10 条/13 条的超限 skill,系限制加入前发布)")
lines.append("- **skill 池膨胀**:42 → 70(+67%),`min_candidate_support=1` 门槛放水,是候选数量膨胀的直接原因\n")

lines.append("## 结论\n")
lines.append("1. **迭代机制有效**:未用 skill 剔除 + 剩余与 drafts CRUD 后,access 毒害在干净对照下消失,Open-domain 获得显著修复")
lines.append("2. **新 cons drafts 是本次净损失的主因**(-17~18):为 train 失败模式定制的指令泛化到 val 后污染时间/对抗性信息提取")
lines.append("3. **下一步建议**:① 提高 `min_candidate_support` 限制每轮新增 skill 数量;② 只采纳在 val 上有收益的 cons drafts;③ 将无条件提取型指令改为条件触发;④ 用 26C 构建 + 35A 答题组合作为短期回归基线\n")

DOC.write_text("\n".join(lines), encoding="utf-8")
print(f"written: {DOC}")
print(f"lines: {len(lines)}")
