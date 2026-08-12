"""Answer questions against an already-built SQLite memory, without ingesting."""
from __future__ import annotations

import argparse, json, sqlite3
from collections import defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mim.artifacts import RunDir
from mim.config import load_config
from mim.eval.locomo import load_dataset
from mim.eval.metrics import compute_f1
from mim.llm import create_client
from mim.retrieval.embedder import Embedder
from mim.skills import SkillBank
from mim.workflows.use import MiMRuntime

def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument('--config',required=True); p.add_argument('--run-id',required=True)
    p.add_argument('--output-dir',required=True); p.add_argument('--conversation-id',required=True)
    p.add_argument('--skill-bank-dir'); p.add_argument('--question-retries',type=int,default=3)
    p.add_argument('--export-only',action='store_true')
    p.add_argument('--qa-ids-file', help='Optional newline-delimited QA ids to answer')
    p.add_argument('--max-per-category', type=int, help='Keep the first N questions per category')
    a=p.parse_args(); cfg=load_config(a.config); run=RunDir(a.run_id,a.output_dir)
    convs, questions=load_dataset(cfg.dataset.path); qas=questions[a.conversation_id]
    if a.qa_ids_file:
        wanted={x.strip() for x in Path(a.qa_ids_file).read_text(encoding='utf-8-sig').splitlines() if x.strip()}
        qas=[q for q in qas if q.qa_id in wanted]
    if a.max_per_category is not None:
        if a.max_per_category < 1:
            raise ValueError('--max-per-category must be positive')
        counts=defaultdict(int)
        selected=[]
        for q in qas:
            if counts[q.category] < a.max_per_category:
                selected.append(q)
                counts[q.category] += 1
        qas=selected
    if a.export_only:
        db=sqlite3.connect(run.path/'state'/'memory.sqlite3')
        qrows={r[0]:r for r in db.execute('select qa_id,category,question,reference_answer from qa_cases')}
        rows=[]
        for r in db.execute("select access_run_id,qa_id,prediction,skill_version_ids from access_runs where status='completed'"):
            aid,qid,pred,skills=r; q=qrows[qid]
            ev=[x[0] for x in db.execute('select version_id from access_final_evidence where access_run_id=? order by evidence_index',(aid,))]
            rows.append({'conversation_id':a.conversation_id,'qa_id':qid,'category':q[1],'question':q[2],'reference':q[3],'prediction':pred or '','evidence_ids':ev,'skill_ids':json.loads(skills or '[]'),'f1':float(compute_f1(pred or '',q[3],q[1])),'runtime_tokens':0,'access_steps':0,'error':''})
        rows.sort(key=lambda x:x['qa_id']); run.write_jsonl('qa_results.jsonl',rows)
        groups=defaultdict(list)
        for r in rows: groups[str(r['category'])].append(r['f1'])
        run.write_json('summary.json',{'mode':'exported_existing_memory','conversation_id':a.conversation_id,'total_qa':len(rows),'overall_f1':sum(r['f1'] for r in rows)/len(rows) if rows else 0.0,'category_f1':{k:sum(v)/len(v) for k,v in groups.items()},'protocol_errors':0})
        print(a.conversation_id,'export',len(rows),flush=True); return 0
    model=create_client(cfg.models['runtime']); emb=Embedder(cfg.embedding.model,cfg.embedding.device,cfg.embedding.normalize,cfg.embedding.batch_size)
    bank=None; mode='base'
    if a.skill_bank_dir:
        bank=SkillBank.load_published(a.skill_bank_dir); bank.freeze(); mode='mim'
    rt=MiMRuntime(cfg,mode=mode,skill_bank=bank,run_dir=run,runtime_model=model,embedder=emb,phase='train_answer_only',strict_construction=True)
    rt.attach(a.conversation_id)
    rows=[]
    existing = run.path / 'qa_results.jsonl'
    if existing.exists():
        existing.unlink()
    for q in qas:
        access=None
        for _ in range(max(1,a.question_retries)):
            access=rt.ask(q)
            if not access.error: break
        assert access is not None
        rows.append({'conversation_id':a.conversation_id,'qa_id':q.qa_id,'category':q.category,'question':q.question,'reference':q.reference_answer,'prediction':access.answer,'evidence_ids':access.evidence_ids,'skill_ids':access.used_skill_ids,'f1':float(compute_f1(access.answer,q.reference_answer,q.category) if not access.error else 0.0),'runtime_tokens':access.total_tokens,'access_steps':access.steps,'error':access.error})
    run.write_jsonl('qa_results.jsonl',rows)
    groups=defaultdict(list)
    for r in rows: groups[str(r['category'])].append(r['f1'])
    run.write_json('summary.json',{'mode':mode,'conversation_id':a.conversation_id,'total_qa':len(rows),'overall_f1':sum(r['f1'] for r in rows)/len(rows) if rows else 0.0,'category_f1':{k:sum(v)/len(v) for k,v in groups.items()},'protocol_errors':sum(bool(r['error']) for r in rows),'avg_access_steps':sum(r['access_steps'] for r in rows)/len(rows) if rows else 0.0})
    print(a.conversation_id, mode, len(rows), sum(r['f1'] for r in rows)/len(rows), flush=True)
    return 0
if __name__=='__main__': raise SystemExit(main())
