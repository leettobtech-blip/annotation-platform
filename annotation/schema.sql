CREATE TABLE IF NOT EXISTS projects (
    tenant uuid NOT NULL, id uuid NOT NULL, name text NOT NULL,
    PRIMARY KEY (tenant, id)
);
CREATE TABLE IF NOT EXISTS assets (
    tenant uuid NOT NULL, project uuid NOT NULL, id uuid NOT NULL,
    kind text NOT NULL, content_type text NOT NULL, external_id text, object_version text,
    duration_ms bigint,
    PRIMARY KEY (tenant, project, id),
    FOREIGN KEY (tenant, project) REFERENCES projects
);
CREATE TABLE IF NOT EXISTS annotations (
    tenant uuid NOT NULL, project uuid NOT NULL, id uuid NOT NULL,
    asset uuid NOT NULL, author text NOT NULL, label text NOT NULL,
    start_ms bigint NOT NULL, end_ms bigint NOT NULL, note text NOT NULL,
    PRIMARY KEY (tenant, project, id),
    FOREIGN KEY (tenant, project, asset) REFERENCES assets
);
CREATE INDEX IF NOT EXISTS annotations_asset ON annotations(tenant, project, asset, id);
CREATE TABLE IF NOT EXISTS jobs (
    id uuid PRIMARY KEY, tenant uuid NOT NULL, project uuid NOT NULL,
    kind text NOT NULL, idem uuid NOT NULL, request jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued', attempts int NOT NULL DEFAULT 0,
    available timestamptz NOT NULL DEFAULT now(), lease uuid, result jsonb, error text,
    FOREIGN KEY (tenant, project) REFERENCES projects,
    UNIQUE (tenant, project, kind, idem)
);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(kind, available, id)
    WHERE status IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS chunks (
    tenant uuid NOT NULL, project uuid NOT NULL, source uuid NOT NULL,
    source_kind text NOT NULL, position int NOT NULL, body text NOT NULL, metadata jsonb NOT NULL,
    terms tsvector GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED,
    PRIMARY KEY (tenant, project, source_kind, source, position),
    FOREIGN KEY (tenant, project) REFERENCES projects
);
CREATE INDEX IF NOT EXISTS chunks_terms ON chunks USING gin(terms);
CREATE INDEX IF NOT EXISTS chunks_asset ON chunks (tenant, project, (metadata->>'asset_id'))
    WHERE metadata->>'asset_id' IS NOT NULL;
