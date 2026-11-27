-- Installed with the collector's TiCats training schema.
CREATE TABLE IF NOT EXISTS voc.model_review_policy (
    company_id uuid NOT NULL REFERENCES public.company,
    scope_key text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    settings jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (company_id,scope_key)
);

-- A previously trained model can be evaluated here without inventing a local training run.
CREATE TABLE IF NOT EXISTS voc.external_model_evaluation (
    company_id uuid NOT NULL REFERENCES public.company,
    model_name text NOT NULL,
    registry_version integer NOT NULL CHECK (registry_version > 0),
    eval_dataset_id uuid NOT NULL REFERENCES voc.training_dataset,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    mlflow_run_id text NOT NULL,
    review_counts jsonb NOT NULL,
    PRIMARY KEY (company_id, model_name, registry_version)
);

-- Review domains are separate from the primary routing domain of incoming tickets.
CREATE TABLE IF NOT EXISTS voc.model_review_domain (
    code text PRIMARY KEY CHECK (code IN ('commerce','finance','food_delivery')),
    domain_id uuid NOT NULL UNIQUE REFERENCES public.domain
);
DO $$
DECLARE
    item record;
    matched_domain_id uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('voc.model_review_domain.bootstrap', 0));
    FOR item IN SELECT * FROM (VALUES
      ('commerce','커머스',ARRAY['커머스','이커머스','전자상거래','commerce']),
      ('finance','금융',ARRAY['금융','finance']),
      ('food_delivery','외식배달',ARRAY['외식배달','외식·배달','음식배달','food_delivery'])
    ) AS catalog(code,name,aliases)
    LOOP
        CONTINUE WHEN EXISTS (SELECT FROM voc.model_review_domain WHERE code=item.code);
        SELECT d.domain_id INTO matched_domain_id FROM public.domain d
        WHERE lower(d.name)=ANY(item.aliases)
        ORDER BY (d.name=item.name) DESC,d.domain_id LIMIT 1;
        IF matched_domain_id IS NULL THEN
            INSERT INTO public.domain(name) VALUES (item.name) RETURNING domain_id INTO matched_domain_id;
        END IF;
        INSERT INTO voc.model_review_domain(code,domain_id) VALUES (item.code,matched_domain_id);
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS voc.model_review_company_domains (
    company_id uuid PRIMARY KEY REFERENCES public.company ON DELETE CASCADE,
    codes text[] NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    CHECK (codes <@ ARRAY['commerce','finance','food_delivery','company_domain']::text[])
);

-- The original offline comparison belongs to one tenant and one exact test split.
CREATE TABLE IF NOT EXISTS voc.model_review_benchmark (
    company_id uuid NOT NULL REFERENCES public.company ON DELETE CASCADE,
    task_code text NOT NULL CHECK (task_code IN ('sentiment','commerce','finance','food_delivery')),
    model_code text NOT NULL CHECK (model_code IN ('klue_bert_v1','ax_encoder')),
    model_sha256 text NOT NULL CHECK (model_sha256 ~ '^[0-9a-f]{64}$'),
    report_sha256 text NOT NULL CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
    split_sha256 text NOT NULL CHECK (split_sha256 ~ '^[0-9a-f]{64}$'),
    row_count integer NOT NULL CHECK (row_count > 0),
    macro_f1 double precision NOT NULL CHECK (macro_f1 BETWEEN 0 AND 1),
    label_counts jsonb NOT NULL,
    label_order jsonb NOT NULL,
    confusion_matrix jsonb NOT NULL,
    PRIMARY KEY(company_id,task_code,model_code)
);

-- Before multi-domain review each company had one sentiment policy, keyed to its primary domain.
INSERT INTO voc.model_review_policy(company_id,scope_key,revision,settings,updated_at)
SELECT p.company_id,'sentiment',p.revision,p.settings,p.updated_at
FROM voc.model_review_policy p
JOIN public.company c ON c.company_id=p.company_id
WHERE p.scope_key='sentiment/' || coalesce(c.domain_id::text,'')
ON CONFLICT (company_id,scope_key) DO NOTHING;
