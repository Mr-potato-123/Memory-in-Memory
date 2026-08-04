-- MiM SQLite Schema v2
-- Memory versions, construction commits, FTS5, provenance edges,
-- access traces, QA cases, failure cases.
-- Runtime DB: outputs/<run_id>/state/memory.sqlite3
-- Failure reports are immutable JSON artifacts under outputs/<run_id>/failures.

PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

-- ================================================================
-- Runtime Tables
-- ================================================================

-- ── Conversations ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    split_name      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Sessions ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    session_index    INTEGER NOT NULL,
    occurred_at      TEXT,
    content_hash     TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id),
    UNIQUE (conversation_id, session_index)
);

-- ── Messages ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS messages (
    message_id       TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    turn_index       INTEGER NOT NULL,
    role             TEXT NOT NULL,
    speaker          TEXT,
    content          TEXT NOT NULL,
    occurred_at      TEXT,
    content_hash     TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    UNIQUE (session_id, turn_index)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_session
ON messages(conversation_id, session_id, turn_index);

-- ── Construction Commits ───────────────────────────────────

CREATE TABLE IF NOT EXISTS construction_commits (
    commit_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id    TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    base_commit_id     INTEGER,
    run_id             TEXT NOT NULL,
    status             TEXT NOT NULL,       -- pending | committed | failed
    runtime_model      TEXT NOT NULL,
    prompt_hash        TEXT NOT NULL,
    skill_version_ids  TEXT NOT NULL,       -- JSON array
    skill_trace_json   TEXT NOT NULL DEFAULT '{}',
    plan_json          TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at       TEXT,
    error_message      TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_commits_conversation
ON construction_commits(conversation_id, commit_id);

-- ── Construction Inputs (which messages were processed) ────

CREATE TABLE IF NOT EXISTS construction_inputs (
    commit_id   INTEGER NOT NULL,
    message_id  TEXT NOT NULL,
    PRIMARY KEY (commit_id, message_id),
    FOREIGN KEY (commit_id) REFERENCES construction_commits(commit_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_construction_inputs_message
ON construction_inputs(message_id, commit_id);

-- ── Memory Candidates ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_candidates (
    candidate_id       TEXT PRIMARY KEY,
    commit_id          INTEGER NOT NULL,
    conversation_id    TEXT NOT NULL,
    memory_kind        TEXT NOT NULL,
    subject            TEXT NOT NULL,
    predicate          TEXT,
    object_text        TEXT,
    content            TEXT NOT NULL,
    world_start        TEXT,
    world_end          TEXT,
    entities_json      TEXT NOT NULL,
    keywords_json      TEXT NOT NULL,
    importance         REAL NOT NULL DEFAULT 0.5,
    confidence         REAL NOT NULL DEFAULT 0.5,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (commit_id) REFERENCES construction_commits(commit_id)
);

-- ── Candidate ↔ Message edges ─────────────────────────────

CREATE TABLE IF NOT EXISTS candidate_message_edges (
    candidate_id  TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    relation      TEXT NOT NULL DEFAULT 'direct_support',
    PRIMARY KEY (candidate_id, message_id, relation),
    FOREIGN KEY (candidate_id) REFERENCES memory_candidates(candidate_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_candidate_message
ON candidate_message_edges(message_id, candidate_id);

-- ── Construction Decisions ────────────────────────────────

CREATE TABLE IF NOT EXISTS construction_decisions (
    decision_id          TEXT PRIMARY KEY,
    commit_id            INTEGER NOT NULL,
    candidate_id         TEXT NOT NULL,
    decision_index       INTEGER NOT NULL,
    action               TEXT NOT NULL,    -- ADD | UPDATE | MERGE | SKIP
    target_memory_id     TEXT,
    update_type          TEXT,             -- add | state_change | correction | enrichment | merge
    result_version_id    TEXT,
    reason               TEXT NOT NULL,
    validation_status    TEXT NOT NULL DEFAULT 'accepted',
    validation_errors    TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (commit_id) REFERENCES construction_commits(commit_id),
    FOREIGN KEY (candidate_id) REFERENCES memory_candidates(candidate_id),
    UNIQUE (commit_id, decision_index)
);

CREATE INDEX IF NOT EXISTS idx_decisions_candidate
ON construction_decisions(candidate_id, action);

-- ── Memory Versions ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_versions (
    row_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id               TEXT NOT NULL UNIQUE,
    memory_id                TEXT NOT NULL,
    version_no               INTEGER NOT NULL,
    conversation_id          TEXT NOT NULL,

    memory_kind              TEXT NOT NULL,
    subject                  TEXT NOT NULL,
    predicate                TEXT,
    object_text              TEXT,
    content                  TEXT NOT NULL,

    world_start              TEXT,
    world_end                TEXT,
    recorded_at              TEXT NOT NULL,

    system_from_commit       INTEGER NOT NULL,
    system_to_commit         INTEGER,
    close_reason             TEXT,

    source_message_ids       TEXT NOT NULL DEFAULT '[]',
    entities_json            TEXT NOT NULL DEFAULT '[]',
    keywords_json            TEXT NOT NULL DEFAULT '[]',
    related_memory_ids       TEXT NOT NULL DEFAULT '[]',

    importance               REAL NOT NULL DEFAULT 0.5,
    confidence               REAL NOT NULL DEFAULT 0.5,

    content_hash             TEXT NOT NULL,
    embedding_blob           BLOB NOT NULL,
    embedding_dim            INTEGER NOT NULL,
    embedding_model          TEXT NOT NULL,

    parent_version_id        TEXT,
    update_type              TEXT NOT NULL DEFAULT 'add',
    created_by_skill_ids     TEXT NOT NULL DEFAULT '[]',

    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id),
    UNIQUE (memory_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_memory_conversation_current
ON memory_versions(conversation_id, system_to_commit);

CREATE INDEX IF NOT EXISTS idx_memory_subject_predicate
ON memory_versions(conversation_id, subject, predicate);

CREATE INDEX IF NOT EXISTS idx_memory_world_time
ON memory_versions(conversation_id, world_start, world_end);

CREATE INDEX IF NOT EXISTS idx_memory_hash
ON memory_versions(conversation_id, content_hash);

-- ── Memory Version ↔ Message direct edges ────────────────

CREATE TABLE IF NOT EXISTS memory_version_message_edges (
    version_id   TEXT NOT NULL,
    message_id   TEXT NOT NULL,
    relation     TEXT NOT NULL DEFAULT 'direct_support',
    PRIMARY KEY (version_id, message_id, relation),
    FOREIGN KEY (version_id) REFERENCES memory_versions(version_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_message_edge
ON memory_version_message_edges(message_id, version_id);

-- ── Memory Version Parent edges (multi-parent for MERGE) ──

CREATE TABLE IF NOT EXISTS memory_version_parent_edges (
    child_version_id   TEXT NOT NULL,
    parent_version_id  TEXT NOT NULL,
    relation           TEXT NOT NULL,  -- state_change | correction | enrichment | merge
    PRIMARY KEY (child_version_id, parent_version_id, relation),
    FOREIGN KEY (child_version_id) REFERENCES memory_versions(version_id),
    FOREIGN KEY (parent_version_id) REFERENCES memory_versions(version_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_parent
ON memory_version_parent_edges(parent_version_id, child_version_id);

-- ── Materialized Lineage Closure ──────────────────────────

CREATE TABLE IF NOT EXISTS memory_lineage_messages (
    version_id         TEXT NOT NULL,
    message_id         TEXT NOT NULL,
    origin_version_id  TEXT NOT NULL,
    min_depth          INTEGER NOT NULL,
    PRIMARY KEY (version_id, message_id, origin_version_id),
    FOREIGN KEY (version_id) REFERENCES memory_versions(version_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    FOREIGN KEY (origin_version_id) REFERENCES memory_versions(version_id)
);

CREATE INDEX IF NOT EXISTS idx_lineage_message
ON memory_lineage_messages(message_id, version_id);

-- ── Memory Change Events ─────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_change_events (
    change_id            TEXT PRIMARY KEY,
    commit_id            INTEGER NOT NULL,
    decision_id          TEXT NOT NULL,
    operation            TEXT NOT NULL,
    new_version_id       TEXT,
    changed_fields_json  TEXT NOT NULL DEFAULT '{}',
    direct_message_ids   TEXT NOT NULL DEFAULT '[]',
    affected_message_ids TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (commit_id) REFERENCES construction_commits(commit_id),
    FOREIGN KEY (decision_id) REFERENCES construction_decisions(decision_id)
);

CREATE TABLE IF NOT EXISTS memory_change_parents (
    change_id          TEXT NOT NULL,
    parent_version_id  TEXT NOT NULL,
    PRIMARY KEY (change_id, parent_version_id),
    FOREIGN KEY (change_id) REFERENCES memory_change_events(change_id),
    FOREIGN KEY (parent_version_id) REFERENCES memory_versions(version_id)
);

-- ── FTS5 ──────────────────────────────────────────────────

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    version_id UNINDEXED,
    conversation_id UNINDEXED,
    content,
    subject,
    predicate,
    object_text,
    keywords,
    tokenize = 'porter unicode61'
);

-- ================================================================
-- Access Trace Tables
-- ================================================================

-- ── Access Runs ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS access_runs (
    access_run_id        TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    conversation_id      TEXT NOT NULL,
    qa_id                TEXT NOT NULL,
    snapshot_commit_id   INTEGER NOT NULL,
    question             TEXT NOT NULL,
    prediction           TEXT,
    skill_version_ids    TEXT NOT NULL DEFAULT '[]',
    skill_trace_json     TEXT NOT NULL DEFAULT '{}',
    answer_prompt_hash   TEXT,
    status               TEXT NOT NULL DEFAULT 'pending',
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_access_run_qa
ON access_runs(run_id, conversation_id, qa_id);

-- ── Access Actions ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS access_actions (
    action_id       TEXT PRIMARY KEY,
    access_run_id   TEXT NOT NULL,
    step_index      INTEGER NOT NULL,
    action_type     TEXT NOT NULL,  -- search_memory | inspect_memory | answer
    request_json    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (access_run_id) REFERENCES access_runs(access_run_id),
    UNIQUE (access_run_id, step_index)
);

-- ── Retrieval Hits ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS access_retrieval_hits (
    action_id          TEXT NOT NULL,
    version_id         TEXT NOT NULL,
    raw_rank           INTEGER NOT NULL,
    final_rank         INTEGER,
    semantic_rank      INTEGER,
    keyword_rank       INTEGER,
    structured_rank    INTEGER,
    fused_score        REAL,
    returned_to_agent  INTEGER NOT NULL DEFAULT 1,
    kept_in_workspace  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (action_id, version_id),
    FOREIGN KEY (action_id) REFERENCES access_actions(action_id),
    FOREIGN KEY (version_id) REFERENCES memory_versions(version_id)
);

CREATE INDEX IF NOT EXISTS idx_access_hit_version
ON access_retrieval_hits(version_id, action_id);

-- ── Final Answer-Visible Context ──────────────────────────

CREATE TABLE IF NOT EXISTS access_answer_context (
    access_run_id   TEXT NOT NULL,
    version_id      TEXT NOT NULL,
    context_index   INTEGER NOT NULL,
    rendered_text   TEXT NOT NULL,
    token_count     INTEGER,
    PRIMARY KEY (access_run_id, context_index),
    UNIQUE (access_run_id, version_id),
    FOREIGN KEY (access_run_id) REFERENCES access_runs(access_run_id),
    FOREIGN KEY (version_id) REFERENCES memory_versions(version_id)
);

-- ── Final Evidence ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS access_final_evidence (
    access_run_id  TEXT NOT NULL,
    version_id     TEXT NOT NULL,
    evidence_index INTEGER NOT NULL,
    PRIMARY KEY (access_run_id, version_id),
    FOREIGN KEY (access_run_id) REFERENCES access_runs(access_run_id),
    FOREIGN KEY (version_id) REFERENCES memory_versions(version_id)
);

-- ================================================================
-- QA Cases (dataset reference)
-- ================================================================

CREATE TABLE IF NOT EXISTS qa_cases (
    qa_id              TEXT PRIMARY KEY,
    conversation_id    TEXT NOT NULL,
    category           INTEGER,
    question           TEXT NOT NULL,
    reference_answer   TEXT NOT NULL,
    metadata_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS qa_gold_sources (
    qa_id       TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    PRIMARY KEY (qa_id, message_id),
    FOREIGN KEY (qa_id) REFERENCES qa_cases(qa_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id)
);
