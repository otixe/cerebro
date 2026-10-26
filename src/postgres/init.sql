-- Public service schema: schema-only; no data, owners/ACL/COMMENT/SET or named seeds.
-- Denied export tables: chunks_archive, approval_private.*, backups, sessions, project_aliases.
-- Approvals are OUT OF V1: namespace/functions/triggers omitted by authority.
-- Required archive and alias DDL supplemented below; details in executor delivery.
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

CREATE TABLE public.tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    title text NOT NULL,
    description text DEFAULT ''::text,
    project text DEFAULT ''::text,
    status text DEFAULT 'todo'::text NOT NULL,
    priority text DEFAULT 'medium'::text,
    assignee text DEFAULT ''::text,
    creator text DEFAULT ''::text,
    tags text[] DEFAULT '{}'::text[],
    parent_id uuid,
    due_date date,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    subagent text DEFAULT ''::text,
    claimed_by text DEFAULT ''::text,
    claimed_at timestamp with time zone,
    blocked_reason text DEFAULT ''::text,
    organization text,
    closed_at timestamp with time zone,
    submitted_by text DEFAULT ''::text,
    request_kind text DEFAULT 'ordinary'::text NOT NULL,
    version integer DEFAULT 0 NOT NULL,
    creator_kind text,
    action_type text,
    resource_kind text,
    resource_id text,
    scope_version integer,
    scope_canonical bytea,
    scope_hash text,
    approved_by text,
    approved_at timestamp with time zone,
    revoked_at timestamp with time zone,
    finished_by text,
    finished_at timestamp with time zone,
    replaces_request_id uuid,
    CONSTRAINT tasks_approval_approved_pair_check CHECK ((((approved_by IS NULL) AND (approved_at IS NULL)) OR ((approved_by = 'human:admin'::text) AND (approved_at IS NOT NULL)))),
    CONSTRAINT tasks_approval_not_self_check CHECK (((replaces_request_id IS NULL) OR (replaces_request_id <> id))),
    CONSTRAINT tasks_approval_payload_check CHECK ((((request_kind = 'ordinary'::text) AND (version = 0) AND (creator_kind IS NULL) AND (action_type IS NULL) AND (resource_kind IS NULL) AND (resource_id IS NULL) AND (scope_version IS NULL) AND (scope_canonical IS NULL) AND (scope_hash IS NULL) AND (approved_by IS NULL) AND (approved_at IS NULL) AND (revoked_at IS NULL) AND (finished_by IS NULL) AND (finished_at IS NULL) AND (replaces_request_id IS NULL)) OR ((request_kind = 'approval_request'::text) AND (btrim(creator) <> ''::text) AND (creator_kind = ANY (ARRAY['human'::text, 'agent'::text])) AND (action_type = ANY (ARRAY['maintenance_window'::text, 'destructive_fs_action'::text, 'other_explicit'::text])) AND (resource_kind = ANY (ARRAY['folder'::text, 'file'::text, 'service'::text, 'other'::text])) AND (btrim(resource_id) <> ''::text) AND (scope_version > 0) AND (scope_canonical IS NOT NULL) AND (scope_hash ~ '^[0-9a-f]{64}$'::text) AND (encode(public.digest(scope_canonical, 'sha256'::text), 'hex'::text) = scope_hash) AND (version >= 0) AND ((action_type <> 'destructive_fs_action'::text) OR (resource_kind = ANY (ARRAY['folder'::text, 'file'::text]))) AND ((action_type <> 'maintenance_window'::text) OR (resource_kind <> 'other'::text)) AND (status = ANY (ARRAY['pending_approval'::text, 'changes_requested'::text, 'superseded_request'::text, 'withdrawn'::text, 'aprobada'::text, 'revocada'::text, 'done'::text]))))),
    CONSTRAINT tasks_approval_state_shape_check CHECK (((request_kind = 'ordinary'::text) OR ((claimed_by = ''::text) AND (claimed_at IS NULL) AND (((status = 'pending_approval'::text) AND (assignee = 'admin'::text) AND (approved_by IS NULL) AND (approved_at IS NULL) AND (revoked_at IS NULL) AND (finished_by IS NULL) AND (finished_at IS NULL)) OR ((status = 'changes_requested'::text) AND (assignee = creator) AND (approved_by IS NULL) AND (approved_at IS NULL) AND (revoked_at IS NULL) AND (finished_by IS NULL) AND (finished_at IS NULL)) OR ((status = ANY (ARRAY['superseded_request'::text, 'withdrawn'::text])) AND (assignee = ''::text) AND (approved_by IS NULL) AND (approved_at IS NULL) AND (revoked_at IS NULL) AND (finished_by IS NULL) AND (finished_at IS NULL)) OR ((status = 'aprobada'::text) AND (assignee = creator) AND (approved_by = 'human:admin'::text) AND (approved_at IS NOT NULL) AND (revoked_at IS NULL) AND (finished_by IS NULL) AND (finished_at IS NULL)) OR ((status = 'revocada'::text) AND (assignee = creator) AND (approved_by = 'human:admin'::text) AND (approved_at IS NOT NULL) AND (revoked_at IS NOT NULL) AND (finished_by IS NULL) AND (finished_at IS NULL)) OR ((status = 'done'::text) AND (assignee = creator) AND (approved_by = 'human:admin'::text) AND (approved_at IS NOT NULL) AND (revoked_at IS NULL) AND (finished_by = creator) AND (finished_at IS NOT NULL)))))),
    CONSTRAINT tasks_request_kind_check CHECK ((request_kind = ANY (ARRAY['ordinary'::text, 'approval_request'::text])))
);

CREATE FUNCTION public.norm_project_key(s text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT lower(regexp_replace(coalesce(s, ''), '[^a-zA-Z0-9]', '', 'g'));
$$;

CREATE FUNCTION public.tasks_derive_organization() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
DECLARE
    v_org      text;
    v_canon    text;
BEGIN
    IF NEW.project IS NOT NULL AND NEW.project <> '' THEN
        SELECT org_id INTO v_org FROM projects
         WHERE id = NEW.project AND status <> 'merged';

        IF v_org IS NULL THEN
            SELECT a.project_id INTO v_canon
              FROM project_aliases a
             WHERE a.alias = norm_project_key(NEW.project);

            IF v_canon IS NOT NULL THEN
                NEW.project := v_canon;
                SELECT org_id INTO v_org FROM projects WHERE id = v_canon;
            ELSE
                INSERT INTO projects (id, org_id, name, needs_review)
                     VALUES (NEW.project, 'interno', NEW.project, true)
                ON CONFLICT (id) DO NOTHING;
                INSERT INTO project_aliases (alias, project_id)
                     VALUES (norm_project_key(NEW.project), NEW.project)
                ON CONFLICT (alias) DO NOTHING;
                v_org := 'interno';
            END IF;
        END IF;
    END IF;

    IF NEW.organization IS NULL OR NEW.organization = '' THEN
        NEW.organization := coalesce(v_org, 'interno');
    END IF;

    RETURN NEW;
END;
$$;

CREATE FUNCTION public.tasks_stamp_closed_at() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
DECLARE
    terminal_nuevo boolean := NEW.status IN ('done', 'cancelled');
    terminal_viejo boolean := (TG_OP = 'UPDATE')
                              AND OLD.status IN ('done', 'cancelled');
BEGIN
    IF terminal_nuevo THEN
        IF NEW.closed_at IS NULL
           OR (TG_OP = 'UPDATE' AND NEW.closed_at IS NOT DISTINCT FROM OLD.closed_at
               AND NOT terminal_viejo) THEN
            NEW.closed_at := now();
        END IF;
    ELSIF terminal_viejo THEN
        NEW.closed_at := NULL;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

CREATE TABLE public.agent_checkpoints (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent text NOT NULL,
    session_ref text DEFAULT ''::text NOT NULL,
    step_index integer DEFAULT 0 NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    kind text DEFAULT 'progress'::text NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.agents (
    id integer NOT NULL,
    name text NOT NULL,
    api_key_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    last_seen timestamp with time zone,
    active boolean DEFAULT true,
    team text,
    is_lead boolean DEFAULT false,
    created_by text,
    revoked_at timestamp with time zone,
    kind text DEFAULT 'agent'::text NOT NULL,
    role text DEFAULT 'member'::text NOT NULL,
    password_hash text,
    email text,
    owner_principal_id integer,
    must_change_password boolean DEFAULT false,
    execution_class text,
    CONSTRAINT agents_execution_class_check CHECK (((execution_class IS NULL) OR (execution_class = ANY (ARRAY['read-only'::text, 'writer'::text, 'executor'::text, 'auditor'::text, 'orchestrator'::text])))),
    CONSTRAINT agents_kind_check CHECK ((kind = ANY (ARRAY['human'::text, 'agent'::text]))),
    CONSTRAINT agents_role_check CHECK ((role = ANY (ARRAY['admin'::text, 'member'::text, 'guest'::text])))
);

CREATE SEQUENCE public.agents_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.agents_id_seq OWNED BY public.agents.id;

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now(),
    agent text NOT NULL,
    action text NOT NULL,
    chunk_id uuid,
    query text,
    result_count integer,
    duration_ms integer,
    success boolean DEFAULT true,
    error text
);

CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;

CREATE TABLE public.bus_deliveries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    message_id uuid NOT NULL,
    recipient text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    delivered_at timestamp with time zone,
    ack_at timestamp with time zone
);

CREATE TABLE public.bus_messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    ts timestamp with time zone DEFAULT now(),
    sender text NOT NULL,
    recipients text[] DEFAULT '{}'::text[] NOT NULL,
    performative text DEFAULT 'inform'::text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    conversation_id text DEFAULT ''::text,
    refs_task uuid,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.chunks_meta (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_path text NOT NULL,
    source_hash text NOT NULL,
    chunk_index integer NOT NULL,
    title text,
    tags text[] DEFAULT '{}'::text[],
    agent_owner text,
    sensitivity text DEFAULT 'normal'::text,
    text_preview text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    deleted_at timestamp with time zone,
    category text DEFAULT 'general'::text,
    valid_from timestamp with time zone DEFAULT now(),
    valid_until timestamp with time zone,
    superseded_by uuid,
    text_full text,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('spanish'::regconfig, ((COALESCE(title, ''::text) || ' '::text) || COALESCE(text_full, ''::text)))) STORED,
    CONSTRAINT chunks_meta_sensitivity_check CHECK ((sensitivity = ANY (ARRAY['normal'::text, 'secret'::text, 'pii'::text, 'metadata_only'::text])))
);

CREATE TABLE public.consolidation_candidates (
    id bigint NOT NULL,
    kind text NOT NULL,
    src_chunk uuid NOT NULL,
    dst_chunk uuid,
    score real,
    source_path text,
    proposed_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'proposed'::text NOT NULL,
    reason text,
    detail jsonb,
    CONSTRAINT consolidation_candidates_kind_check CHECK ((kind = ANY (ARRAY['dedup'::text, 'decay'::text]))),
    CONSTRAINT consolidation_candidates_status_check CHECK ((status = ANY (ARRAY['proposed'::text, 'applied'::text, 'dismissed'::text, 'stale'::text])))
);

CREATE SEQUENCE public.consolidation_candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.consolidation_candidates_id_seq OWNED BY public.consolidation_candidates.id;

CREATE TABLE public.correspondencia_adjuntos (
    id uuid NOT NULL,
    mensaje_id uuid NOT NULL,
    nombre text NOT NULL,
    tipo_mime text NOT NULL,
    tamano bigint NOT NULL,
    blob_hash text NOT NULL,
    subido_por text NOT NULL,
    creado_en timestamp with time zone DEFAULT now() NOT NULL,
    retirado_en timestamp with time zone,
    CONSTRAINT correspondencia_adjuntos_tamano_check CHECK ((tamano >= 0))
);

CREATE TABLE public.correspondencia_blobs (
    hash text NOT NULL,
    tamano bigint NOT NULL,
    creado_por text NOT NULL,
    creado_en timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT correspondencia_blobs_hash_check CHECK ((hash ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT correspondencia_blobs_tamano_check CHECK ((tamano >= 0))
);

CREATE TABLE public.correspondencia_entregas (
    id uuid NOT NULL,
    mensaje_id uuid NOT NULL,
    buzon text NOT NULL,
    estado text DEFAULT 'pendiente'::text NOT NULL,
    leido_en timestamp with time zone,
    archivado_en timestamp with time zone,
    via_lectura text,
    escalada_en timestamp with time zone,
    escalado_causa text,
    origen text,
    origen_estado text,
    CONSTRAINT correspondencia_entregas_check CHECK ((((estado = 'pendiente'::text) AND (archivado_en IS NULL)) OR ((estado = 'leido'::text) AND (leido_en IS NOT NULL) AND (archivado_en IS NULL)) OR ((estado = 'archivado'::text) AND (archivado_en IS NOT NULL)))),
    CONSTRAINT correspondencia_entregas_escalado_causa_check CHECK (((escalado_causa IS NULL) OR (escalado_causa ~ '^(actividad|edad)_[0-9]{1,5}h$'::text))),
    CONSTRAINT correspondencia_entregas_estado_check CHECK ((estado = ANY (ARRAY['pendiente'::text, 'leido'::text, 'archivado'::text]))),
    CONSTRAINT correspondencia_entregas_origen_archivado_check CHECK (((origen IS NULL) OR (estado = 'archivado'::text))),
    CONSTRAINT correspondencia_entregas_origen_check CHECK (((origen IS NULL) OR (origen = 'bus-legacy'::text))),
    CONSTRAINT correspondencia_entregas_origen_estado_check CHECK (((origen_estado IS NULL) OR (origen_estado = ANY (ARRAY['acked'::text, 'delivered'::text, 'pending'::text, 'sin-entrega'::text])))),
    CONSTRAINT correspondencia_entregas_origen_estado_no_null_check CHECK (((origen IS NULL) OR (origen_estado IS NOT NULL))),
    CONSTRAINT correspondencia_entregas_via_lectura_check CHECK (((via_lectura IS NULL) OR (via_lectura = ANY (ARRAY['panel'::text, 'mcp'::text, 'rest'::text]))))
);

CREATE TABLE public.correspondencia_mensajes (
    id uuid NOT NULL,
    codigo text NOT NULL,
    raiz text NOT NULL,
    remitente text NOT NULL,
    asunto text NOT NULL,
    prioridad text DEFAULT 'medio'::text NOT NULL,
    contenido text NOT NULL,
    tipo text NOT NULL,
    abierto boolean NOT NULL,
    refs_task uuid,
    ref_vault text,
    creado_en timestamp with time zone DEFAULT now() NOT NULL,
    origen text,
    origen_ref jsonb,
    CONSTRAINT correspondencia_mensajes_check CHECK (((codigo = raiz) OR (codigo ~ (('^'::text || raiz) || '\.[1-9][0-9]*$'::text)))),
    CONSTRAINT correspondencia_mensajes_origen_check CHECK (((origen IS NULL) OR (origen = 'bus-legacy'::text))),
    CONSTRAINT correspondencia_mensajes_origen_ref_check CHECK ((((origen IS NULL) AND (origen_ref IS NULL)) OR ((origen IS NOT NULL) AND (origen_ref ? 'bus_message_id'::text)))),
    CONSTRAINT correspondencia_mensajes_origen_ref_no_null_check CHECK (((origen IS NULL) OR (origen_ref IS NOT NULL))),
    CONSTRAINT correspondencia_mensajes_prioridad_check CHECK ((prioridad = ANY (ARRAY['urgente'::text, 'medio'::text, 'bajo'::text]))),
    CONSTRAINT correspondencia_mensajes_raiz_check CHECK ((raiz ~ '^[0-9a-f]{16}$'::text)),
    CONSTRAINT correspondencia_mensajes_tipo_check CHECK ((tipo = ANY (ARRAY['inform'::text, 'request'::text])))
);

CREATE TABLE public.correspondencia_reacciones (
    id uuid NOT NULL,
    mensaje_id uuid NOT NULL,
    buzon text NOT NULL,
    emoji text NOT NULL,
    es_ok boolean DEFAULT false NOT NULL,
    creado_en timestamp with time zone DEFAULT now() NOT NULL,
    retirado_en timestamp with time zone
);

CREATE TABLE public.index_log (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now(),
    source_path text NOT NULL,
    action text NOT NULL,
    chunks_count integer,
    duration_ms integer,
    error text
);

CREATE SEQUENCE public.index_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.index_log_id_seq OWNED BY public.index_log.id;

CREATE TABLE public.organizations (
    id text NOT NULL,
    name text NOT NULL,
    color text DEFAULT ''::text,
    sort_order integer DEFAULT 100,
    active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.projects (
    id text NOT NULL,
    org_id text NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'active'::text,
    sort_order integer DEFAULT 100,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    merged_into text,
    needs_review boolean DEFAULT false
);

CREATE TABLE public.retrieval_eval_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    k integer NOT NULL,
    n_queries integer DEFAULT 0 NOT NULL,
    "precision" real,
    recall real,
    mrr real,
    triggered_by text,
    detail jsonb,
    run_at timestamp with time zone DEFAULT now() NOT NULL,
    specificity real,
    n_negative integer DEFAULT 0
);

CREATE TABLE public.retrieval_evalset (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    query text NOT NULL,
    expected_sources jsonb DEFAULT '[]'::jsonb NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    kind text DEFAULT 'positive'::text NOT NULL,
    CONSTRAINT retrieval_evalset_kind_chk CHECK ((kind = ANY (ARRAY['positive'::text, 'negative'::text]))),
    CONSTRAINT retrieval_evalset_kind_coherente CHECK ((((kind = 'negative'::text) AND (jsonb_array_length(expected_sources) = 0)) OR ((kind = 'positive'::text) AND (jsonb_array_length(expected_sources) > 0))))
);

CREATE TABLE public.scope_grants (
    id integer NOT NULL,
    principal_id integer NOT NULL,
    path_prefix text NOT NULL,
    can_write boolean DEFAULT false,
    granted_by integer,
    created_at timestamp with time zone DEFAULT now(),
    revoked_at timestamp with time zone
);

CREATE SEQUENCE public.scope_grants_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.scope_grants_id_seq OWNED BY public.scope_grants.id;

CREATE TABLE public.semantic_edges (
    src_chunk uuid NOT NULL,
    dst_chunk uuid NOT NULL,
    src_source text NOT NULL,
    dst_source text NOT NULL,
    score real NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.semantic_rebuild_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    triggered_by text,
    threshold real,
    top_n integer,
    max_chunks integer,
    chunks_total integer,
    chunks_processed integer DEFAULT 0 NOT NULL,
    edges_written integer DEFAULT 0 NOT NULL,
    error text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone
);

CREATE TABLE public.skill_proposals (
    id bigint NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    rationale text,
    evidence jsonb DEFAULT '[]'::jsonb NOT NULL,
    corroboration integer DEFAULT 0 NOT NULL,
    related_skill text,
    avg_sim real,
    proposed_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'proposed'::text NOT NULL,
    CONSTRAINT skill_proposals_kind_check CHECK ((kind = ANY (ARRAY['new'::text, 'update'::text]))),
    CONSTRAINT skill_proposals_status_check CHECK ((status = ANY (ARRAY['proposed'::text, 'accepted'::text, 'dismissed'::text])))
);

CREATE SEQUENCE public.skill_proposals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.skill_proposals_id_seq OWNED BY public.skill_proposals.id;

CREATE TABLE public.task_activity (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    agent text NOT NULL,
    kind text NOT NULL,
    from_status text,
    to_status text,
    body text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.task_checkpoints (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    step_index integer NOT NULL,
    label text DEFAULT ''::text,
    state jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.task_deps (
    task_id uuid NOT NULL,
    depends_on_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);

CREATE VIEW public.v_recent_searches AS
 SELECT agent,
    count(*) AS searches,
    max(ts) AS last_search
   FROM public.audit_log
  WHERE ((action = 'search'::text) AND (ts > (now() - '7 days'::interval)))
  GROUP BY agent
  ORDER BY (count(*)) DESC;

-- Supplement: archive exact from host init.sql; aliases minimum from code queries.
CREATE TABLE public.chunks_archive (
  LIKE public.chunks_meta INCLUDING ALL,
  archived_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE public.project_aliases (alias text PRIMARY KEY, project_id text NOT NULL);

CREATE VIEW public.v_stats AS
 SELECT ( SELECT count(*) AS count
           FROM public.chunks_meta
          WHERE (chunks_meta.deleted_at IS NULL)) AS active_chunks,
    ( SELECT count(*) AS count
           FROM public.chunks_meta
          WHERE (chunks_meta.deleted_at IS NOT NULL)) AS soft_deleted_chunks,
    ( SELECT count(*) AS count
           FROM public.chunks_archive) AS archived_chunks,
    ( SELECT count(DISTINCT chunks_meta.source_path) AS count
           FROM public.chunks_meta
          WHERE (chunks_meta.deleted_at IS NULL)) AS active_sources,
    ( SELECT count(*) AS count
           FROM public.agents
          WHERE agents.active) AS active_agents;

CREATE VIEW public.v_tareas_posible_duplicado AS
 WITH abiertas AS (
         SELECT tasks.id,
            tasks.title,
            tasks.assignee,
            tasks.project,
            tasks.status,
            tasks.parent_id,
            ARRAY( SELECT DISTINCT w.w
                   FROM unnest(string_to_array(lower(regexp_replace(tasks.title, '[^a-zA-Z0-9áéíóúñ ]'::text, ' '::text, 'g'::text)), ' '::text)) w(w)
                  WHERE (length(w.w) > 4)) AS palabras
           FROM public.tasks
          WHERE (tasks.status <> ALL (ARRAY['done'::text, 'cancelled'::text]))
        )
 SELECT a.id AS id_a,
    b.id AS id_b,
    a.title AS titulo_a,
    b.title AS titulo_b,
    a.assignee AS asignado_a,
    b.assignee AS asignado_b,
    cardinality(ARRAY( SELECT unnest(a.palabras) AS unnest
        INTERSECT
         SELECT unnest(b.palabras) AS unnest)) AS palabras_comunes
   FROM (abiertas a
     JOIN abiertas b ON ((a.id < b.id)))
  WHERE ((cardinality(ARRAY( SELECT unnest(a.palabras) AS unnest
        INTERSECT
         SELECT unnest(b.palabras) AS unnest)) >= 3) AND (a.id IS DISTINCT FROM b.parent_id) AND (b.id IS DISTINCT FROM a.parent_id) AND ((a.parent_id IS NULL) OR (b.parent_id IS NULL) OR (a.parent_id <> b.parent_id)));

ALTER TABLE ONLY public.agents ALTER COLUMN id SET DEFAULT nextval('public.agents_id_seq'::regclass);

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);

ALTER TABLE ONLY public.consolidation_candidates ALTER COLUMN id SET DEFAULT nextval('public.consolidation_candidates_id_seq'::regclass);

ALTER TABLE ONLY public.index_log ALTER COLUMN id SET DEFAULT nextval('public.index_log_id_seq'::regclass);

ALTER TABLE ONLY public.scope_grants ALTER COLUMN id SET DEFAULT nextval('public.scope_grants_id_seq'::regclass);

ALTER TABLE ONLY public.skill_proposals ALTER COLUMN id SET DEFAULT nextval('public.skill_proposals_id_seq'::regclass);

ALTER TABLE ONLY public.agent_checkpoints
    ADD CONSTRAINT agent_checkpoints_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_name_key UNIQUE (name);

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.bus_deliveries
    ADD CONSTRAINT bus_deliveries_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.bus_messages
    ADD CONSTRAINT bus_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.chunks_meta
    ADD CONSTRAINT chunks_meta_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.chunks_meta
    ADD CONSTRAINT chunks_meta_source_path_chunk_index_key UNIQUE (source_path, chunk_index);

ALTER TABLE ONLY public.consolidation_candidates
    ADD CONSTRAINT consolidation_candidates_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.correspondencia_adjuntos
    ADD CONSTRAINT correspondencia_adjuntos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.correspondencia_blobs
    ADD CONSTRAINT correspondencia_blobs_pkey PRIMARY KEY (hash);

ALTER TABLE ONLY public.correspondencia_entregas
    ADD CONSTRAINT correspondencia_entregas_mensaje_id_buzon_key UNIQUE (mensaje_id, buzon);

ALTER TABLE ONLY public.correspondencia_entregas
    ADD CONSTRAINT correspondencia_entregas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.correspondencia_mensajes
    ADD CONSTRAINT correspondencia_mensajes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.correspondencia_reacciones
    ADD CONSTRAINT correspondencia_reacciones_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.retrieval_eval_runs
    ADD CONSTRAINT retrieval_eval_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.retrieval_evalset
    ADD CONSTRAINT retrieval_evalset_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.scope_grants
    ADD CONSTRAINT scope_grants_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.semantic_edges
    ADD CONSTRAINT semantic_edges_pkey PRIMARY KEY (src_chunk, dst_chunk);

ALTER TABLE ONLY public.semantic_rebuild_jobs
    ADD CONSTRAINT semantic_rebuild_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.skill_proposals
    ADD CONSTRAINT skill_proposals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.task_activity
    ADD CONSTRAINT task_activity_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.task_checkpoints
    ADD CONSTRAINT task_checkpoints_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.task_deps
    ADD CONSTRAINT task_deps_pkey PRIMARY KEY (task_id, depends_on_id);

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_approval_replaces_unique UNIQUE (replaces_request_id);

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);

CREATE INDEX idx_agent_ckpt_agent ON public.agent_checkpoints USING btree (agent, created_at DESC);

CREATE INDEX idx_agent_ckpt_session ON public.agent_checkpoints USING btree (agent, session_ref, step_index);

CREATE INDEX idx_audit_action ON public.audit_log USING btree (action);

CREATE INDEX idx_audit_agent ON public.audit_log USING btree (agent);

CREATE INDEX idx_audit_ts ON public.audit_log USING btree (ts DESC);

CREATE INDEX idx_bus_deliv_recipient ON public.bus_deliveries USING btree (recipient, status);

CREATE INDEX idx_bus_msg_conversation ON public.bus_messages USING btree (conversation_id, created_at);

CREATE INDEX idx_bus_msg_ts ON public.bus_messages USING btree (ts);

CREATE INDEX idx_chunks_deleted ON public.chunks_meta USING btree (deleted_at);

CREATE INDEX idx_chunks_meta_category ON public.chunks_meta USING btree (category);

CREATE INDEX idx_chunks_sensitivity ON public.chunks_meta USING btree (sensitivity);

CREATE INDEX idx_chunks_source ON public.chunks_meta USING btree (source_path);

CREATE INDEX idx_chunks_tags ON public.chunks_meta USING gin (tags);

CREATE INDEX idx_chunks_tsv ON public.chunks_meta USING gin (tsv);

CREATE INDEX idx_chunks_updated ON public.chunks_meta USING btree (updated_at);

CREATE INDEX idx_chunks_valid_until ON public.chunks_meta USING btree (valid_until);

CREATE INDEX idx_consol_kind ON public.consolidation_candidates USING btree (kind, status);

CREATE INDEX idx_consol_score ON public.consolidation_candidates USING btree (score DESC);

CREATE INDEX idx_consol_source ON public.consolidation_candidates USING btree (source_path);

CREATE INDEX idx_consol_status ON public.consolidation_candidates USING btree (status);

CREATE INDEX idx_correspondencia_adjuntos_activos ON public.correspondencia_adjuntos USING btree (mensaje_id, creado_en, id) WHERE (retirado_en IS NULL);

CREATE INDEX idx_correspondencia_adjuntos_blob ON public.correspondencia_adjuntos USING btree (blob_hash);

CREATE INDEX idx_correspondencia_adjuntos_mensaje ON public.correspondencia_adjuntos USING btree (mensaje_id);

CREATE INDEX idx_correspondencia_entregas_buzon_estado ON public.correspondencia_entregas USING btree (buzon, estado);

CREATE UNIQUE INDEX idx_correspondencia_mensajes_bus_id ON public.correspondencia_mensajes USING btree (((origen_ref ->> 'bus_message_id'::text))) WHERE (origen = 'bus-legacy'::text);

CREATE UNIQUE INDEX idx_correspondencia_mensajes_codigo ON public.correspondencia_mensajes USING btree (codigo);

CREATE INDEX idx_correspondencia_mensajes_creado_en ON public.correspondencia_mensajes USING btree (creado_en);

CREATE INDEX idx_correspondencia_mensajes_origen ON public.correspondencia_mensajes USING btree (origen) WHERE (origen IS NOT NULL);

CREATE INDEX idx_correspondencia_mensajes_raiz ON public.correspondencia_mensajes USING btree (raiz);

CREATE UNIQUE INDEX idx_correspondencia_ok_activo ON public.correspondencia_reacciones USING btree (mensaje_id, buzon) WHERE ((es_ok = true) AND (retirado_en IS NULL));

CREATE INDEX idx_index_log_path ON public.index_log USING btree (source_path);

CREATE INDEX idx_index_log_ts ON public.index_log USING btree (ts DESC);

CREATE INDEX idx_projects_org ON public.projects USING btree (org_id);

CREATE INDEX idx_reveval_created ON public.retrieval_evalset USING btree (created_at);

CREATE INDEX idx_revrun_runat ON public.retrieval_eval_runs USING btree (run_at DESC);

CREATE INDEX idx_semedge_dst ON public.semantic_edges USING btree (dst_source, score DESC);

CREATE INDEX idx_semedge_score ON public.semantic_edges USING btree (score DESC);

CREATE INDEX idx_semedge_src ON public.semantic_edges USING btree (src_source, score DESC);

CREATE INDEX idx_semjob_started ON public.semantic_rebuild_jobs USING btree (started_at DESC);

CREATE INDEX idx_semjob_status ON public.semantic_rebuild_jobs USING btree (status);

CREATE INDEX idx_skillprop_kind ON public.skill_proposals USING btree (kind, status);

CREATE INDEX idx_skillprop_status ON public.skill_proposals USING btree (status);

CREATE INDEX idx_task_activity_task ON public.task_activity USING btree (task_id, created_at);

CREATE INDEX idx_task_ckpt_task ON public.task_checkpoints USING btree (task_id, step_index);

CREATE INDEX idx_task_deps_depends_on ON public.task_deps USING btree (depends_on_id);

CREATE INDEX idx_tasks_assignee ON public.tasks USING btree (assignee);

CREATE INDEX idx_tasks_closed_at ON public.tasks USING btree (closed_at);

CREATE INDEX idx_tasks_org_project ON public.tasks USING btree (organization, project);

CREATE INDEX idx_tasks_organization ON public.tasks USING btree (organization);

CREATE INDEX idx_tasks_parent ON public.tasks USING btree (parent_id);

CREATE INDEX idx_tasks_project ON public.tasks USING btree (project);

CREATE INDEX idx_tasks_status ON public.tasks USING btree (status);

CREATE INDEX idx_tasks_submitted_by ON public.tasks USING btree (submitted_by);

CREATE INDEX scope_grants_principal_id_idx ON public.scope_grants USING btree (principal_id) WHERE (revoked_at IS NULL);

CREATE UNIQUE INDEX uq_consol_current_logical_identity ON public.consolidation_candidates USING btree (kind, (
CASE
    WHEN (kind = 'dedup'::text) THEN LEAST(src_chunk, dst_chunk)
    ELSE src_chunk
END), (
CASE
    WHEN (kind = 'dedup'::text) THEN GREATEST(src_chunk, dst_chunk)
    ELSE dst_chunk
END)) NULLS NOT DISTINCT WHERE (status = ANY (ARRAY['proposed'::text, 'stale'::text]));

CREATE TRIGGER trg_chunks_meta_touch BEFORE UPDATE ON public.chunks_meta FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

CREATE TRIGGER trg_tasks_derive_organization BEFORE INSERT OR UPDATE OF project, organization ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.tasks_derive_organization();

CREATE TRIGGER trg_tasks_stamp_closed_at BEFORE INSERT OR UPDATE OF status, closed_at ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.tasks_stamp_closed_at();

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_owner_principal_id_fkey FOREIGN KEY (owner_principal_id) REFERENCES public.agents(id);

ALTER TABLE ONLY public.bus_deliveries
    ADD CONSTRAINT bus_deliveries_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.bus_messages(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.correspondencia_adjuntos
    ADD CONSTRAINT correspondencia_adjuntos_blob_fk FOREIGN KEY (blob_hash) REFERENCES public.correspondencia_blobs(hash);

ALTER TABLE ONLY public.correspondencia_adjuntos
    ADD CONSTRAINT correspondencia_adjuntos_mensaje_fk FOREIGN KEY (mensaje_id) REFERENCES public.correspondencia_mensajes(id);

ALTER TABLE ONLY public.correspondencia_entregas
    ADD CONSTRAINT correspondencia_entregas_mensaje_id_fkey FOREIGN KEY (mensaje_id) REFERENCES public.correspondencia_mensajes(id);

ALTER TABLE ONLY public.correspondencia_mensajes
    ADD CONSTRAINT correspondencia_mensajes_refs_task_fkey FOREIGN KEY (refs_task) REFERENCES public.tasks(id);

ALTER TABLE ONLY public.correspondencia_reacciones
    ADD CONSTRAINT correspondencia_reacciones_mensaje_id_fkey FOREIGN KEY (mensaje_id) REFERENCES public.correspondencia_mensajes(id);

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id);

ALTER TABLE ONLY public.scope_grants
    ADD CONSTRAINT scope_grants_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES public.agents(id);

ALTER TABLE ONLY public.scope_grants
    ADD CONSTRAINT scope_grants_principal_id_fkey FOREIGN KEY (principal_id) REFERENCES public.agents(id);

ALTER TABLE ONLY public.task_activity
    ADD CONSTRAINT task_activity_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.task_checkpoints
    ADD CONSTRAINT task_checkpoints_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.task_deps
    ADD CONSTRAINT task_deps_depends_on_id_fkey FOREIGN KEY (depends_on_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.task_deps
    ADD CONSTRAINT task_deps_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_approval_replaces_fk FOREIGN KEY (replaces_request_id) REFERENCES public.tasks(id) ON DELETE RESTRICT;

-- Seed mínimo del core (fresh install): tasks_derive_organization() asigna 'interno'
-- a proyectos nuevos; sin esta fila, una instalación de cero da FK projects_org_id_fkey.
INSERT INTO public.organizations (id, name) VALUES ('interno', 'Internal') ON CONFLICT (id) DO NOTHING;

-- Sinapsis (login humano)
-- Sessions of the Sinapsis web panel. Schema-only, taken from the live schema
-- (catalog read of public.sessions); token hashes only, never plaintext tokens.

CREATE TABLE public.sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    principal_id integer NOT NULL,
    token_hash text NOT NULL,
    method text DEFAULT 'password'::text,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT sessions_method_check CHECK ((method = ANY (ARRAY['password'::text, 'passkey'::text])))
);

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);

CREATE INDEX sessions_token_hash_idx ON public.sessions USING btree (token_hash) WHERE (revoked_at IS NULL);

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_principal_id_fkey FOREIGN KEY (principal_id) REFERENCES public.agents(id);
