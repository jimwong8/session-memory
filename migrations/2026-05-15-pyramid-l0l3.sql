-- Migration: 2026-05-15 — 金字塔 L0-L3 建表
-- 作用域：session-memory 数据库
-- 前置条件：vector 扩展已启用（由 init_db() 自动创建）

-- ============================================================
-- 1. Memory Atoms（L1：原子事实）
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_atoms (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  user_id VARCHAR(255) NOT NULL,
  project_key VARCHAR(255),
  kind VARCHAR(32) NOT NULL DEFAULT 'fact'
    CHECK (kind IN ('preference','decision','fact','constraint','goal','error_pattern','task_state','blocker')),
  title VARCHAR(500),
  content TEXT NOT NULL,
  tags TEXT[] DEFAULT '{}',
  source_message_ids UUID[] NOT NULL DEFAULT '{}',
  embedding VECTOR(384),
  confidence_score REAL DEFAULT 0.8,
  sensitivity_level VARCHAR(16) NOT NULL DEFAULT 'normal'
    CHECK (sensitivity_level IN ('normal','high')),
  dedup_signature VARCHAR(64),
  superseded_by UUID REFERENCES memory_atoms(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 业务查询索引
CREATE INDEX idx_atoms_user_kind ON memory_atoms(user_id, kind, created_at DESC);
CREATE INDEX idx_atoms_session ON memory_atoms(session_id, created_at DESC);
CREATE INDEX idx_atoms_project ON memory_atoms(project_key, created_at DESC) WHERE project_key IS NOT NULL;
CREATE INDEX idx_atoms_dedup ON memory_atoms(user_id, dedup_signature) WHERE dedup_signature IS NOT NULL;

-- 向量索引（HNSW，与 kg_entities 现有索引风格一致）
CREATE INDEX IF NOT EXISTS idx_atoms_embedding ON memory_atoms
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200);

-- 全文搜索索引（中文使用 simple 分词兜底，后续可换 zhparser）
ALTER TABLE memory_atoms ADD COLUMN IF NOT EXISTS content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED;
CREATE INDEX IF NOT EXISTS idx_atoms_tsv ON memory_atoms USING GIN (content_tsv);

-- ============================================================
-- 2. Memory Scenarios（L2：场景块）
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_scenarios (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id VARCHAR(255) NOT NULL,
  project_key VARCHAR(255),
  title VARCHAR(500) NOT NULL,
  narrative_md TEXT NOT NULL,
  atom_ids UUID[] NOT NULL DEFAULT '{}',
  session_ids UUID[] NOT NULL DEFAULT '{}',
  period_start TIMESTAMPTZ,
  period_end TIMESTAMPTZ,
  embedding VECTOR(384),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_scenarios_user ON memory_scenarios(user_id, created_at DESC);
CREATE INDEX idx_scenarios_project ON memory_scenarios(project_key, created_at DESC) WHERE project_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_scenarios_embedding ON memory_scenarios
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200);

ALTER TABLE memory_scenarios ADD COLUMN IF NOT EXISTS content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', coalesce(narrative_md, ''))) STORED;
CREATE INDEX IF NOT EXISTS idx_scenarios_tsv ON memory_scenarios USING GIN (content_tsv);

-- ============================================================
-- 3. Personas（L3：用户画像）
-- ============================================================
CREATE TABLE IF NOT EXISTS personas (
  user_id VARCHAR(255) PRIMARY KEY,
  profile_md TEXT NOT NULL DEFAULT '',
  preferences_json JSONB DEFAULT '{}'::jsonb,
  scenario_ids UUID[] DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 4. 给现有 kg_entities 加 level 字段
-- ============================================================
ALTER TABLE kg_entities ADD COLUMN IF NOT EXISTS level VARCHAR(8) NOT NULL DEFAULT 'L1'
  CHECK (level IN ('L0','L1','L2','L3'));
CREATE INDEX IF NOT EXISTS idx_kg_entities_level ON kg_entities(level);

-- ============================================================
-- 5. 给现有 sessions 加 canvas_mermaid 字段
-- ============================================================
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS canvas_mermaid TEXT;

-- ============================================================
-- 6. Persona 版本历史
-- ============================================================
CREATE TABLE IF NOT EXISTS persona_history (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id VARCHAR(255) NOT NULL REFERENCES personas(user_id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  profile_md TEXT NOT NULL,
  preferences_json JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_persona_history_user ON persona_history(user_id, version DESC);
