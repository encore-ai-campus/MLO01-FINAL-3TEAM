"""Read company-scoped lineage from the collector and training records."""
from config.database import fetch_rows, read_snapshot


FLOW = (
    ("수집", "Discord·Gmail 문의를 원본 이벤트로 보존합니다.", "voc.bronze_messages"),
    ("표준화", "문의 본문과 채널·대화 정보를 표준화합니다.", "voc.silver_messages"),
    ("티켓", "회사별 티켓을 생성하고 분류 결과를 반영합니다.", "public.ticket"),
    ("피처", "본문·전처리 버전과 학습에 사용할 특성을 보존합니다.", "voc.feature_snapshot"),
    ("학습·평가", "확정된 정답으로 데이터셋 버전을 구성합니다.", "voc.training_dataset"),
    ("모델", "학습 실행과 등록 모델 버전을 연결합니다.", "voc.training_run · voc.model_version"),
)


def companies():
    with read_snapshot() as cursor:
        cursor.execute("""
            SELECT company_uuid::text AS id, coalesce(nullif(name,''),email_domain) AS name
            FROM public.company ORDER BY name, company_uuid
        """)
        return fetch_rows(cursor)


def event_trace(company, query, page):
    with read_snapshot() as cursor:
        if not query:
            cursor.execute("""
                SELECT ticket_number, title, created_at, source_event_id
                FROM public.ticket WHERE company_uuid=%s
                ORDER BY created_at DESC, ticket_uuid DESC LIMIT 26 OFFSET %s
            """, (company, (page - 1) * 25))
            rows = fetch_rows(cursor)
            return {"tickets": rows[:25], "has_next": len(rows) > 25}

        # Scope the source through its ticket or registered source connection before reading it.
        cursor.execute("""
            SELECT b.event_id, b.channel, b.source_message_id, b.received_at,
                   s.standardized_at, s.schema_version, s.body_version,
                   t.ticket_uuid, t.ticket_number, t.created_at AS ticket_created_at,
                   t.sentiment_status, t.category_status
            FROM voc.bronze_messages b
            LEFT JOIN voc.silver_messages s ON s.event_id=b.event_id AND s.schema_version=4
            LEFT JOIN public.ticket t ON t.source_event_id=b.event_id AND t.company_uuid=%s
            WHERE (b.event_id::text=%s OR t.ticket_number=%s)
              AND (t.ticket_uuid IS NOT NULL OR EXISTS (
                  SELECT 1 FROM voc.ticket_source_connection r
                  WHERE r.company_uuid=%s AND r.company_id=b.company_id
                    AND r.channel=b.channel AND r.connection_id=b.connection_id
                    AND r.source_channel_id=coalesce(s.source_channel_id,b.raw_payload->>'channel_id','')
              ))
            ORDER BY b.received_at DESC LIMIT 1
        """, (company, query, query.removeprefix("#").zfill(8), company))
        event = next(iter(fetch_rows(cursor)), None)
        if not event:
            return {"notice": "소속 회사에서 해당 수집 이벤트를 찾을 수 없습니다. 수집 이벤트가 없는 티켓은 추적할 수 없습니다."}

        cursor.execute("SELECT to_regclass('voc.feature_snapshot'), to_regclass('voc.training_dataset_item')")
        feature_table, item_table = cursor.fetchone()
        features, datasets = [], []
        if feature_table:
            cursor.execute("""
                SELECT feature_uuid, body_version, preprocess_version, received_at
                FROM voc.feature_snapshot WHERE company_uuid=%s AND silver_event_id=%s
                ORDER BY preprocess_version DESC, body_version DESC LIMIT 25
            """, (company, event["event_id"]))
            features = fetch_rows(cursor)
        if feature_table and item_table:
            cursor.execute("""
                SELECT DISTINCT d.dataset_version, d.purpose, d.row_count, d.finalized_at
                FROM voc.training_dataset_item i
                JOIN voc.feature_snapshot f ON f.feature_uuid=i.feature_uuid
                JOIN voc.training_dataset d ON d.dataset_uuid=i.dataset_uuid
                WHERE f.company_uuid=%s AND d.company_uuid=%s AND f.silver_event_id=%s
                ORDER BY d.dataset_version DESC LIMIT 25
            """, (company, company, event["event_id"]))
            datasets = fetch_rows(cursor)
        return {"event": event, "features": features, "datasets": datasets}


def model_history(company, page):
    with read_snapshot() as cursor:
        cursor.execute("SELECT to_regclass('voc.training_run'), to_regclass('voc.model_version')")
        if not all(cursor.fetchone()):
            return {"notice": "학습 이력 저장소가 아직 준비되지 않았습니다."}
        cursor.execute("""
            SELECT r.workflow_ref, r.base_model_uri, r.mlflow_run_id,
                   train.dataset_version AS train_version, train.sha256 AS train_sha,
                   train.row_count AS train_count, train.finalized_at,
                   evaluation.dataset_version AS eval_version, evaluation.sha256 AS eval_sha,
                   evaluation.row_count AS eval_count,
                   r.sentiment_macro_f1, r.category_macro_f1, m.model_name, m.registry_version
            FROM voc.training_run r
            JOIN voc.training_dataset train ON train.dataset_uuid=r.train_dataset_uuid
            JOIN voc.training_dataset evaluation ON evaluation.dataset_uuid=r.eval_dataset_uuid
            LEFT JOIN voc.model_version m ON m.workflow_ref=r.workflow_ref
            WHERE train.company_uuid=%s AND evaluation.company_uuid=%s
            ORDER BY train.created_at DESC, r.workflow_ref DESC LIMIT 26 OFFSET %s
        """, (company, company, (page - 1) * 25))
        rows = fetch_rows(cursor)
        return {"runs": rows[:25], "has_next": len(rows) > 25}
