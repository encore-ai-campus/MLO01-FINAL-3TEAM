"""Generate the versioned Grafana dashboards and keep their queries readable."""
import json
from pathlib import Path

ROOT = Path(__file__).with_name("grafana") / "dashboards"
PAGES = {"pipeline": "파이프라인", "inference": "AI 추론", "deployment": "카나리 배포"}
STEPS = {
    "capture": "수집",
    "bronze": "원문 저장",
    "flink": "Flink 정제",
    "ticket": "티켓 저장",
    "compute": "분류·임베딩",
    "ready": "결과 저장",
}
SQL = {"type": "grafana-postgresql-datasource", "uid": "voc-postgres"}
PROM = {"type": "prometheus", "uid": "voc-prometheus"}
TIME_FROM, TIME_TO = "$__timeFrom()::timestamptz", "$__timeTo()::timestamptz"


def period(column):
    return f"{column} BETWEEN {TIME_FROM} AND {TIME_TO}"


def panel(panel_id, heading, x, y, w, h, query=None, *, kind="table", datasource=SQL,
          description=None, unit=None, links=None, empty="미수집"):
    definition = {"color": {"mode": "fixed", "fixedColor": "blue"}, "noValue": empty}
    if unit:
        definition["unit"] = unit
    config = {
        "id": panel_id, "type": kind, "title": heading,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": definition, "overrides": []},
    }
    if description:
        config["description"] = description
    if links:
        config["links"] = links
        definition["links"] = links
    if query is not None:
        config["datasource"] = datasource
        if datasource == SQL:
            target = {"refId": "A", "rawQuery": True, "rawSql": query, "format": "table", "editorMode": "code"}
        else:
            target = {"refId": "A", "expr": query, "instant": True, "range": False, "format": "table"}
        config["targets"] = [target]
    if kind == "stat":
        config["options"] = {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
            "orientation": "auto", "textMode": "value_and_name", "colorMode": "value",
            "graphMode": "none", "justifyMode": "center", "wideLayout": True,
        }
    elif kind == "table":
        config["options"] = {"showHeader": True, "cellHeight": "sm"}
    elif kind == "text":
        config["options"] = {"mode": "markdown", "content": query}
    return config


def text(panel_id, heading, body, *, y, h=4):
    config = panel(panel_id, heading, 0, y, 24, h, kind="text")
    config["options"]["content"] = body
    return config


def prom(panel_id, heading, x, y, w, h, query, **options):
    return panel(panel_id, heading, x, y, w, h, query, datasource=PROM, **options)


def show_states(config, values):
    config["fieldConfig"]["defaults"]["mappings"] = [{
        "type": "value", "options": {str(key): {"text": name} for key, name in values.items()}
    }]
    return config


def omit_prometheus_fields(config):
    config["transformations"] = [{
        "id": "organize", "options": {
            "excludeByName": {name: True for name in ("Time", "__name__", "instance", "job")},
            "renameByName": {"channel": "채널", "source": "연결", "position": "수집 위치", "phase": "상태",
                             "group": "소비 그룹", "topic": "토픽", "partition": "파티션", "Value": "측정값"},
        },
    }]
    return config


def active(stage):
    return (
        f"EXISTS(SELECT FROM monitor.availability WHERE step='{stage}' "
        f"AND seen_from<={TIME_TO} AND seen_until>={TIME_FROM})"
    )


def stage_query():
    return f"""
WITH events AS (
    SELECT stage,count(*) FILTER(WHERE {period('started_at')}) AS entered,
           count(*) FILTER(WHERE {period('succeeded_at')}) AS completed
    FROM monitor.stage WHERE {period('started_at')} OR {period('succeeded_at')}
    GROUP BY stage
), counts AS (
    SELECT s.stage,e.entered,e.completed,
        e.entered IS NOT NULL OR CASE
            WHEN s.stage IN ('compute','ready') THEN {active('classification')} AND {active('embedding')}
            ELSE EXISTS (SELECT FROM monitor.availability a WHERE a.step=s.stage
                         AND seen_from<={TIME_TO} AND seen_until>={TIME_FROM})
        END AS observed
    FROM unnest(ARRAY['capture','bronze','flink','ticket','compute','ready']) s(stage) LEFT JOIN events e USING(stage)
)
SELECT {", ".join(
    f"max(CASE WHEN stage='{stage}' THEN CASE WHEN observed THEN "
    "'유입 ' || coalesce(entered,0)::text || '건  ·  완료 ' || coalesce(completed,0)::text || '건' ELSE '미수집' END END)"
    f' AS "{stage}"' for stage in STEPS
)}
FROM counts
"""


def pipeline_canvas():
    colors = ("#E4F2FD", "#E0F5F6", "#EDEBFD", "#F9EFDE", "#F7E9F3", "#E4F3E9")
    config = panel(1, "문의 처리 흐름", 0, 0, 24, 5, stage_query(), kind="canvas",
                   description="단계를 누르면 해당 단계의 유입, 완료, 전달 및 재시도 내역으로 이동합니다.")
    elements = []
    for index, (stage, label) in enumerate(STEPS.items()):
        left = round(1.1 + index * 16.6, 2)
        right = round(100 - left - 14.8, 2)
        link = {"title": label + " 상세", "url": f"/d/voc-pipeline/pipeline?var-step={stage}&viewPanel=21&"
                + "${__url_time_range}", "targetBlank": False, "oneClick": True}
        label_element = {
            "name": label, "type": "text", "constraint": {"horizontal": "scale", "vertical": "top"},
            "placement": {"left": left, "right": right, "top": 28, "height": 57},
            "config": {"text": {"fixed": label, "mode": "fixed"}, "color": {"fixed": "#24364D"},
                       "align": "center", "valign": "middle", "size": 19},
            "background": {"color": {"fixed": colors[index]}},
            "border": {"color": {"fixed": colors[index]}, "width": 1, "radius": 10},
            "links": [link],
        }
        if index < len(STEPS) - 1:
            label_element["connections"] = [{
                "source": {"x": 1, "y": 0}, "target": {"x": -1, "y": 0},
                "targetName": list(STEPS.values())[index+1], "path": "straight",
                "color": {"fixed": "#687D98"}, "size": {"fixed": 2}, "direction": {"fixed": "forward"},
            }]
        elements.extend([
            label_element,
            {"name": stage + "-count", "type": "metric-value",
             "constraint": {"horizontal": "scale", "vertical": "top"},
             "placement": {"left": left, "right": right, "top": 94, "height": 30},
             "config": {"text": {"field": stage, "mode": "field"}, "color": {"fixed": "#4B607A"},
                        "align": "center", "valign": "middle", "size": 14},
             "links": [link]},
        ])
    config["options"] = {
        "inlineEditing": False, "panZoom": False, "zoomToContent": False,
        "root": {"name": "문의 처리", "type": "frame", "elements": elements},
        "showAdvancedTypes": False, "tooltip": {"mode": "none", "disableForOneClick": True},
    }
    return config


def dashboard(name, panels, *, template=None, **options):
    uid = "voc-" + name
    return {
        "uid": uid, "title": PAGES[name], "tags": ["VoC"], "schemaVersion": 41, "version": 1,
        "editable": False, "refresh": "15s", "timezone": "Asia/Seoul",
        "time": {"from": "now-10m", "to": "now"}, "panels": panels,
        "links": [
            {"type": "link", "title": title, "url": "/d/voc-" + page + "/" + page, "includeVars": False,
             "keepTime": True, "tooltip": title}
            for page, title in PAGES.items()
        ],
        "templating": {"list": template or []}, **options,
    }


PIPELINES = [
    pipeline_canvas(),
    panel(8, "단계별 처리 시간", 0, 8, 12, 7, f"""
SELECT CASE stage {' '.join(f"WHEN '{step}' THEN '{label}'" for step, label in STEPS.items())} END AS "단계",
       count(*) AS "완료 표본",
       round(percentile_cont(0.95) WITHIN GROUP (
           ORDER BY extract(epoch FROM (succeeded_at-started_at)))::numeric, 2) AS "P95 (초)"
FROM monitor.stage
WHERE {period('succeeded_at')} AND started_at<=succeeded_at
GROUP BY stage ORDER BY array_position(ARRAY['capture','bronze','flink','ticket','compute','ready'],stage)
""", description="같은 문의에서 최초 유입과 최초 완료의 차이를 측정합니다."),
    panel(9, "전체 처리 시간과 30초 목표", 12, 8, 12, 7, f"""
WITH completed AS (
  SELECT extract(epoch FROM ready_at-registered_at) AS seconds FROM monitor.normal_inquiry
  WHERE {period('ready_at')} AND ready_at>=registered_at
), cohort AS (
  SELECT *, ready_at >= registered_at AND ready_at<=least(registered_at+interval '30 seconds',{TIME_TO}) AS hit
  FROM monitor.normal_inquiry WHERE {period('registered_at')}
   AND (registered_at<={TIME_TO}-interval '30 seconds' OR ready_at BETWEEN registered_at AND {TIME_TO})
)
SELECT label AS "지표",value AS "측정값"
FROM (
  SELECT (SELECT round(percentile_cont(0.95) WITHIN GROUP(ORDER BY seconds)::numeric,2) FROM completed) AS p95,
         CASE WHEN count(*)>0 OR {active('bronze')} THEN count(*) FILTER(WHERE hit) END AS hits,
         CASE WHEN count(*)>0 OR {active('bronze')} THEN count(*) FILTER(WHERE NOT coalesce(hit,false)) END AS misses,
         CASE WHEN count(*)>0 OR {active('bronze')} THEN count(*) END AS total,
         round(count(*) FILTER(WHERE hit)*100.0/nullif(count(*),0),1) AS completion
  FROM cohort
) metrics CROSS JOIN LATERAL (VALUES ('전체 시간 P95 (초)',p95),('30초 이내 완료 (건)',hits),
  ('목표 미달 (건)',misses),('대상 (건)',total),('30초 내 완료율 (%)',completion)) AS v(label,value)
""", description="원본 공급자 등록부터 결과 저장까지 잽니다. 등록 시각을 모르는 문의는 제외합니다."),
    panel(10, "원문과 티켓 대조", 0, 15, 12, 6, f"""
SELECT label AS "대조 상태",CASE WHEN observed THEN value END AS "건수"
FROM (
    SELECT count(*)>0 OR {active('bronze')} AS observed,
       count(*) FILTER(WHERE reconciliation='source_without_ticket') AS missing_ticket,
       count(*) FILTER(WHERE reconciliation='ticket_without_source') AS missing_source,
       coalesce(sum(extra_tickets),0) AS duplicates,
       count(*) FILTER(WHERE reconciliation='pending') AS pending,
       count(*) FILTER(WHERE reconciliation='unconfirmed_delivery') AS unconfirmed,
       count(*) FILTER(WHERE reconciliation='failed') AS failed
    FROM monitor.source_reconciliation
) metrics CROSS JOIN LATERAL (VALUES ('티켓 없는 원문',missing_ticket),('원문 없는 티켓',missing_source),
    ('초과 티켓',duplicates),('처리 대기',pending),('전달 확인 중',unconfirmed),('실패 기록 있음',failed)) AS v(label,value)
""", description="현재 전체 원문과 티켓을 비교합니다. 대기·실패 기록은 누락 의심에서 제외합니다."),
    panel(11, "발행·분석 대기", 12, 15, 12, 6, f"""
SELECT label AS "단계", CASE WHEN q.pending>0 OR (stage='flink' AND {active('bronze')})
    OR (stage='classification' AND {active('classification')}) OR (stage='embedding' AND {active('embedding')})
    THEN coalesce(q.pending,0) END AS "대기 건수", q.oldest_seconds::integer AS "가장 오래된 대기 (초)"
FROM (VALUES ('flink','원문 저장 후 Kafka 발행'),('classification','분류'),('embedding','임베딩')) AS s(stage,label)
LEFT JOIN monitor.queue_backlog q USING(stage)
"""),
    *[show_states(prom(12+i, title, i*4, 21, 4, 4, f'voc_collector_ready{{channel="{channel}"}} or on() vector(-1)', kind="stat"),
                  {-1: "미수집", 0: "중단", 1: "수집 중"})
      for i, (channel, title) in enumerate((("gmail","Gmail 수집"),("discord","Discord 수집")))],
    prom(14, "마지막 성공 체크포인트", 8, 21, 8, 4, "voc_flink_last_checkpoint_seconds * 1000",
         kind="stat", unit="dateTimeAsLocal"),
    prom(15, "마지막 체크포인트 복원", 16, 21, 8, 4, "voc_flink_last_restore_seconds * 1000",
         kind="stat", unit="dateTimeAsLocal"),
    panel(16, "채널별 수집 위치", 0, 25, 12, 6, """
SELECT label AS "채널",connection_hash AS "연결",coalesce(position,'미수집') AS "수집 위치",
       CASE phase WHEN 'active' THEN '수집 중' WHEN 'bootstrap' THEN '초기 수집' END AS "상태",
       updated_at AS "마지막 갱신"
FROM (VALUES ('gmail','Gmail'),('discord','Discord')) AS channels(channel,label)
LEFT JOIN monitor.collector_position USING(channel)
""", description="채널별 수집 위치와 마지막 갱신을 확인합니다. 연결은 식별용 해시로 표시합니다."),
    omit_prometheus_fields(prom(17, "수집 발행 대기", 12, 25, 12, 6, "voc_capture_pending")),
    omit_prometheus_fields(prom(18, "Kafka 소비 지연", 0, 31, 12, 8, "voc_kafka_consumer_lag")),
    omit_prometheus_fields(prom(19, "Flink 작업·체크포인트 상태", 12, 31, 12, 8,
                                'voc_flink_job_state or voc_flink_checkpoints',
                                description="작업 상태와 성공·실패·복원 체크포인트 수를 표시합니다.")),
    panel(20, "미해결·복구 실패 기록", 0, 39, 24, 8, f"""
SELECT stage AS "단계",event_id AS "원본 키",reason AS "사유",failed_at AS "실패",
       recovered_at AS "복구",CASE WHEN recovered_at IS NULL THEN '미해결' ELSE '복구' END AS "상태"
FROM monitor.failure
WHERE {period('failed_at')} OR {period('recovered_at')} OR recovered_at IS NULL
ORDER BY failed_at DESC LIMIT 100
"""),
    panel(21, "단계 상세 · ${step:text}", 0, 47, 24, 12, f"""
WITH attempts AS (
  SELECT *,lag(error) OVER ordered AS previous_error,
         lag(request_partition) OVER ordered AS previous_partition,lag(request_offset) OVER ordered AS previous_offset
  FROM monitor.flink_attempt WHERE started_at<={TIME_TO}
    AND ${{step:sqlstring}} IN ('all','flink')
  WINDOW ordered AS (PARTITION BY event_id ORDER BY started_at,attempt_id)
), retries AS (
  SELECT event_id,'flink' AS stage,count(*) AS retries,max(started_at) AS last_at
  FROM attempts
  WHERE previous_error IS NOT NULL OR (request_partition,request_offset)=(previous_partition,previous_offset)
  GROUP BY event_id
  UNION ALL
  SELECT event_id,stage,retries,last_at FROM monitor.analysis_retries WHERE ${{step:sqlstring}} IN ('all','compute')
), detail AS (
  SELECT started_at AS event_at,'유입' AS activity,stage,event_id,
         NULL::text AS topic,NULL::integer AS partition_id,NULL::bigint AS kafka_offset,NULL::bigint AS retries
  FROM monitor.stage WHERE {period('started_at')}
  UNION ALL
  SELECT succeeded_at,'완료',stage,event_id,NULL,NULL,NULL,NULL
  FROM monitor.stage WHERE {period('succeeded_at')}
  UNION ALL
  SELECT recorded_at,'Kafka 전달',stage,event_id,topic,partition_id,kafka_offset,NULL
  FROM monitor.kafka_delivery WHERE {period('recorded_at')} AND stage IN ('capture','bronze','flink')
  UNION ALL
  SELECT failed_at,'실패',stage,event_id,NULL,NULL,NULL,NULL
  FROM monitor.failure WHERE {period('failed_at')} AND event_id IS NOT NULL AND stage IN ('capture','bronze','ticket')
  UNION ALL
  SELECT last_at,'재시도',stage,event_id,NULL,NULL,NULL,retries FROM retries
  WHERE {period('last_at')}
)
SELECT event_at AS "이벤트 시각",activity AS "이벤트 종류",
       CASE stage {" ".join(f"WHEN '{stage}' THEN '{label}'" for stage, label in STEPS.items())}
           WHEN 'classification' THEN '분류' WHEN 'embedding' THEN '임베딩' END AS "단계",
       event_id AS "원본 이벤트 ID",topic AS "Kafka 토픽",partition_id AS "파티션",kafka_offset AS "오프셋",
       retries AS "재시도 횟수"
FROM detail
WHERE ${{step:sqlstring}} IN ('all',stage) OR (${{step:sqlstring}}='compute' AND stage IN ('classification','embedding'))
ORDER BY event_at DESC LIMIT 150
""", description="선택한 단계의 이벤트를 최근 150건까지 표시합니다. 원본 이벤트 ID는 단계마다 같은 원본 문의를 찾는 데 쓰입니다.",
          empty="—"),
    panel(23, "대조가 필요한 원본 키", 0, 59, 24, 8, """
SELECT event_id AS "원본 키",channel AS "채널",ticket_count AS "티켓",extra_tickets AS "초과",
       CASE reconciliation WHEN 'source_without_ticket' THEN '티켓 없는 원문'
        WHEN 'ticket_without_source' THEN '원문 없는 티켓'
        WHEN 'failed' THEN '실패 기록 있음' WHEN 'unconfirmed_delivery' THEN '전달 확인 중' ELSE '대조 완료' END AS "대조 상태"
FROM monitor.source_reconciliation
WHERE reconciliation IN ('source_without_ticket','ticket_without_source','failed','unconfirmed_delivery') OR extra_tickets>0
ORDER BY received_at DESC NULLS FIRST LIMIT 100
"""),
]

for pipeline_panel in PIPELINES[1:]:
    pipeline_panel["gridPos"]["y"] -= 3

filter_variable = {
    "name": "step", "type": "custom", "label": "단계 상세",
    "query": "전체 : all," + ",".join(f"{label} : {key}" for key, label in STEPS.items()),
    "current": {"text": "전체", "value": "all"},
}


def inference_query(rollout=None):
    api = "classification" if rollout else "embedding"
    route = f"AND rollout='{rollout}' AND model_name IS NOT NULL AND model_version IS NOT NULL" if rollout else ""
    return f"""
WITH samples AS (
  SELECT * FROM monitor.inference WHERE api='{api}' {route} AND traffic='operational'
  AND ended_at IS NOT NULL AND {period('started_at')}
)
SELECT CASE WHEN count(*)>0 OR {active(rollout + '_model' if rollout else 'embedding')} THEN count(*) END AS "호출",
       round(count(*) FILTER(WHERE error IS NOT NULL)*100.0/nullif(count(*),0),2) AS "오류율 (%)",
       round(percentile_cont(0.95) WITHIN GROUP(ORDER BY response_ms)::numeric,1) AS "응답 P95 (ms)",
       round(percentile_cont(0.95) WITHIN GROUP(ORDER BY model_ms)::numeric,1) AS "모델 연산 P95 (ms)"
FROM samples
"""


INFERENCE = [
    text(1, "모델 호출과 품질",
         "응답시간은 호출자가 응답이나 실패를 확인할 때까지 잽니다. 모델 연산시간은 모델 API가 보고한 값입니다.",
         y=0, h=3),
    panel(2, "Stable 분류", 0, 3, 8, 6, inference_query("stable")),
    panel(3, "Canary 분류", 8, 3, 8, 6, inference_query("canary")),
    panel(4, "임베딩 API", 16, 3, 8, 6, inference_query()),
    panel(5, "실제 모델 버전별 요청", 0, 9, 24, 8, f"""
SELECT CASE api WHEN 'embedding' THEN '임베딩' ELSE '분류' END AS "API",
       CASE WHEN api='embedding' THEN '임베딩' WHEN model_name IS NULL OR model_version IS NULL OR rollout IS NULL
            THEN '버전 미확인' ELSE initcap(rollout) END AS "응답 경로",
       coalesce(model_name,'미확인') AS "모델",coalesce(model_version,'미확인') AS "모델 버전",
       CASE traffic WHEN 'operational' THEN '운영' WHEN 'replay' THEN '재생' ELSE '고정 평가' END AS "요청 출처",
       count(*) FILTER(WHERE ended_at IS NOT NULL) AS "호출",
       count(*) FILTER(WHERE error IS NOT NULL AND ended_at IS NOT NULL) AS "오류",
       count(*) FILTER(WHERE ended_at IS NULL) AS "결과 미확인",
       round(count(*) FILTER(WHERE error IS NOT NULL AND ended_at IS NOT NULL)*100.0
           /nullif(count(*) FILTER(WHERE ended_at IS NOT NULL),0),2) AS "오류율 (%)",
       round(avg(response_ms)::numeric,1) AS "응답 평균 (ms)",
       round(percentile_cont(0.95) WITHIN GROUP(ORDER BY response_ms)::numeric,1) AS "응답 P95 (ms)",
       round(avg(model_ms)::numeric,1) AS "연산 평균 (ms)",
       round(percentile_cont(0.95) WITHIN GROUP(ORDER BY model_ms)::numeric,1) AS "연산 P95 (ms)"
FROM monitor.inference WHERE {period('started_at')}
GROUP BY api,rollout,model_name,model_version,traffic
ORDER BY api,traffic,rollout,model_name,model_version
""", description="응답의 모델 버전과 실제 배포 구분에 따라 집계합니다. 오류로 버전을 확인하지 못한 요청은 별도 행에 표시합니다."),
    panel(6, "고정 평가셋 Macro-F1", 0, 17, 24, 7, f"""
WITH latest AS (
  SELECT run_id FROM monitor.evaluation WHERE evaluated_at<={TIME_TO}
  GROUP BY run_id HAVING count(DISTINCT rollout)=2 ORDER BY max(evaluated_at) DESC LIMIT 1
)
SELECT initcap(e.rollout) AS "배포", e.model_name AS "모델",e.model_version AS "버전",e.dataset_version AS "고정 평가셋",
       round(max(macro_f1) FILTER(WHERE kind='sentiment')::numeric,4) AS "감성 Macro-F1",
       round(max(macro_f1) FILTER(WHERE kind='category')::numeric,4) AS "카테고리 Macro-F1",
       max(samples) FILTER(WHERE kind='sentiment') AS "감성 표본",
       max(samples) FILTER(WHERE kind='category') AS "카테고리 표본"
FROM latest JOIN monitor.evaluation e USING(run_id) GROUP BY e.rollout,e.model_name,e.model_version,e.dataset_version
UNION ALL SELECT 'Stable',NULL,NULL,'미수집',NULL,NULL,NULL,NULL WHERE NOT EXISTS(SELECT FROM latest)
UNION ALL SELECT 'Canary',NULL,NULL,'미수집',NULL,NULL,NULL,NULL WHERE NOT EXISTS(SELECT FROM latest)
ORDER BY 1 DESC
""", description="사람이 따로 검수한 동일 평가셋에서 두 모델을 실행한 가장 최근 결과입니다."),
    panel(7, "상담사 수정률", 0, 24, 24, 7, f"""
WITH measurement AS (
    SELECT {active('answer_completion')} OR EXISTS(
        SELECT FROM monitor.answer_completion WHERE {period('completed_at')}) AS tracked
), active AS (
    SELECT DISTINCT rollout,model_name,model_version FROM monitor.inference
    WHERE rollout IS NOT NULL AND model_name IS NOT NULL AND model_version IS NOT NULL
      AND api='classification' AND traffic='operational' AND ended_at IS NOT NULL AND {period('started_at')}
), modifications AS (
    SELECT rollout,model_name,model_version,kind,count(*) AS shown,
       count(*) FILTER(WHERE displayed_value IS DISTINCT FROM final_value) AS changed
    FROM monitor.modification WHERE {period('completed_at')}
    GROUP BY rollout,model_name,model_version,kind
), combinations AS (
    SELECT a.*,kind FROM active a CROSS JOIN (VALUES ('sentiment'),('category')) AS k(kind)
    UNION
    SELECT DISTINCT rollout,model_name,model_version,kind FROM modifications
)
SELECT initcap(c.rollout) AS "배포",c.model_name AS "모델",c.model_version AS "버전",
       CASE c.kind WHEN 'sentiment' THEN '감성' ELSE '카테고리' END AS "항목",
       CASE WHEN m.shown IS NOT NULL OR measurement.tracked THEN coalesce(m.changed,0) END AS "수정",
       CASE WHEN m.shown IS NOT NULL OR measurement.tracked THEN coalesce(m.shown,0) END AS "표본",
       CASE WHEN m.shown IS NOT NULL THEN round(m.changed*100.0/m.shown,2)::text || '%'
            WHEN measurement.tracked THEN '표본 없음' ELSE '미수집' END AS "수정률"
FROM combinations c LEFT JOIN modifications m USING(rollout,model_name,model_version,kind)
CROSS JOIN measurement
UNION ALL SELECT NULL,NULL,NULL,label,CASE WHEN measurement.tracked THEN 0 END,CASE WHEN measurement.tracked THEN 0 END,
    CASE WHEN measurement.tracked THEN '표본 없음' ELSE '미수집' END
FROM (VALUES ('감성'),('카테고리')) v(label) CROSS JOIN measurement
WHERE NOT EXISTS(SELECT FROM combinations)
ORDER BY 1 DESC,2,3,4
""", description="답변한 상담사가 처음 본 예측과 실제 답변완료 당시 최종값을 비교합니다."),
    show_states(prom(8, "분류 API 연결 설정", 0, 31, 12, 4,
                     'voc_model_configured{api="classification"}', kind="stat"), {0: "미수집", 1: "설정됨"}),
    show_states(prom(9, "임베딩 API 연결 설정", 12, 31, 12, 4,
                     'voc_model_configured{api="embedding"}', kind="stat"), {0: "미수집", 1: "설정됨"}),
]

if __name__ == "__main__":
    payloads = [
        dashboard("pipeline", PIPELINES, template=[filter_variable]),
        dashboard("inference", INFERENCE),
        dashboard("deployment", [text(1, "", "## 준비 중", y=0, h=8)], refresh=""),
    ]
    for document in payloads:
        (ROOT / (document["uid"].removeprefix("voc-") + ".json")).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        )
