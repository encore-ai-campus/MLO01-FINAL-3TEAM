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


def omit_prometheus_fields(config):
    config["transformations"] = [{
        "id": "organize", "options": {
            "excludeByName": {name: True for name in ("Time", "__name__", "instance", "job")},
            "renameByName": {"channel": "채널", "source": "연결", "position": "수집 위치", "phase": "상태",
                             "group": "소비 그룹", "topic": "토픽", "partition": "파티션", "Value": "측정값"},
        },
    }]
    return config


def stage_query():
    return f"""
WITH capture AS (
    SELECT count(*) AS count FROM voc_monitor.diagnostics WHERE stage='capture' AND {period('observed_at')}
), silver AS (
    SELECT count(*) AS count FROM voc_monitor.diagnostics
    WHERE stage='flink' AND activity='Silver DB 저장' AND {period('observed_at')}
)
SELECT CASE WHEN i.bronze>0 THEN '기록된 수집 ' || c.count || '건' ELSE '미수집' END AS capture,
       CASE WHEN i.bronze>0 THEN '현재 원문 ' || i.bronze || '건' ELSE '미수집' END AS bronze,
       CASE WHEN s.count>0 THEN 'Silver 저장 ' || s.count || '건' ELSE '미수집' END AS flink,
       CASE WHEN i.bronze>0 THEN '현재 원문 연결 ' || i.tickets || '건' ELSE '미수집' END AS ticket,
       CASE WHEN i.queued IS NOT NULL THEN '현재 분류 대기 ' || i.queued || '건' ELSE '미수집' END AS compute,
       '미수집' AS ready
FROM voc_monitor.inventory i CROSS JOIN capture c CROSS JOIN silver s
"""


def pipeline_canvas():
    colors = ("#E4F2FD", "#E0F5F6", "#EDEBFD", "#F9EFDE", "#F7E9F3", "#E4F3E9")
    config = panel(1, "문의 처리 흐름", 0, 0, 24, 5, stage_query(), kind="canvas",
                   description="단계를 눌러 DB에 저장된 수집·Kafka 전달·검토 기록을 확인합니다.")
    elements = []
    for index, (stage, label) in enumerate(STEPS.items()):
        left = round(1.1 + index * 16.6, 2)
        right = round(100 - left - 14.8, 2)
        links = []
        if stage != "ready":
            links.append({"title": label + " 상세", "url": f"/d/voc-pipeline-stage/pipeline-stage?var-step={stage}&"
                          + "${__url_time_range}", "targetBlank": False, "oneClick": True})
        label_element = {
            "name": label, "type": "text", "constraint": {"horizontal": "scale", "vertical": "top"},
            "placement": {"left": left, "right": right, "top": 28, "height": 57},
            "config": {"text": {"fixed": label, "mode": "fixed"}, "color": {"fixed": "#24364D"},
                       "align": "center", "valign": "middle", "size": 19},
            "background": {"color": {"fixed": colors[index]}},
            "border": {"color": {"fixed": colors[index]}, "width": 1, "radius": 10},
            "links": links,
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
             "links": links},
        ])
    config["options"] = {
        "inlineEditing": False, "panZoom": False, "zoomToContent": False,
        "root": {"name": "문의 처리", "type": "frame", "elements": elements},
        "showAdvancedTypes": False, "tooltip": {"mode": "none", "disableForOneClick": True},
    }
    return config


def dashboard(name, panels, *, template=None, title=None, **options):
    uid = "voc-" + name
    return {
        "uid": uid, "title": title or PAGES[name], "tags": ["VoC"], "schemaVersion": 41, "version": 1,
        "editable": False, "refresh": "15s", "timezone": "Asia/Seoul",
        "time": {"from": "now-10m", "to": "now"}, "panels": panels,
        "links": [
            {"type": "link", "title": title, "url": "/d/voc-" + page + "/" + page, "includeVars": False,
             "keepTime": True, "tooltip": title}
            for page, title in PAGES.items()
        ],
        "templating": {"list": template or []}, **options,
    }


def collector_positions():
    fallback = ('label_replace(vector(-1), "channel", "gmail", "", "") '
                'or label_replace(vector(-1), "channel", "discord", "", "")')
    config = prom(16, "채널별 수집 위치", 0, 27, 24, 6,
                  f'voc_collector_position or on(channel) ({fallback})',
                  description="SQLite에 기록된 연결별 위치와 가장 최근 채널 상태를 읽습니다.")
    config["targets"].append({
        **config["targets"][0], "refId": "B",
        "expr": f'max by(channel) (voc_collector_ready) or on(channel) ({fallback})',
    })
    config["transformations"] = [
        {"id": "joinByField", "options": {"byField": "channel", "mode": "outerTabular"}},
        {"id": "organize", "options": {
            "excludeByName": {name: True for name in ("Time", "Time 1", "Time 2", "__name__", "job", "instance",
                                                      "Value #A")},
            "renameByName": {"channel": "채널", "source": "연결", "position": "수집 위치",
                             "phase": "수집 단계", "Value #B": "수집 상태"},
        }},
    ]
    config["fieldConfig"]["overrides"] = [{
        "matcher": {"id": "byName", "options": "Value #B"},
        "properties": [{"id": "mappings", "value": [{"type": "value", "options": {
            "-1": {"text": "미수집"}, "0": {"text": "준비 상태 아님"}, "1": {"text": "수집 중"},
        }}]}],
    }]
    return config


PIPELINES = [
    pipeline_canvas(),
    panel(8, "단계별 처리 시간", 0, 8, 12, 7, f"""
WITH samples(stage, seconds) AS (
    SELECT 'publish',extract(epoch FROM published_at-queued_at)
    FROM voc_monitor.standardization WHERE {period('published_at')} AND published_at>=queued_at
    UNION ALL
    SELECT 'silver',extract(epoch FROM standardized_at-published_at)
    FROM voc_monitor.standardization WHERE {period('standardized_at')} AND standardized_at>=published_at
)
SELECT label AS "측정 구간",count(seconds) AS "완료 표본",
       round(percentile_cont(0.95) WITHIN GROUP(ORDER BY seconds)::numeric,2) AS "P95 (초)"
FROM (VALUES ('publish','요청 대기 → Kafka 전달'),('silver','Kafka 전달 → Silver 저장')) AS stages(stage,label)
LEFT JOIN samples USING(stage)
GROUP BY stages.stage,label
ORDER BY stages.stage
""", empty="-", description="각 구간의 완료 시각을 기준으로 집계합니다. Kafka 전달 기록은 발행 트랜잭션의 시각입니다."),
    panel(9, "전체 처리 시간과 30초 목표", 12, 8, 12, 7, """
SELECT label AS "지표", '미수집' AS "측정값"
FROM (VALUES ('전체 시간 P95 (초)'),('30초 이내 완료 (건)'),('목표 미달 (건)'),('대상 (건)'),('30초 내 완료율 (%)')) v(label)
""", description="문의 등록 시각과 최종 결과 저장 시각을 연결하는 기록이 필요합니다."),
    panel(10, "원문과 티켓 대조", 0, 15, 12, 6, """
SELECT label AS "대조 상태",CASE WHEN observed THEN value END AS "건수"
FROM (
    SELECT count(*)>0 AS observed,
       count(*) FILTER(WHERE reconciliation='source_without_ticket') AS missing_ticket,
       count(*) FILTER(WHERE reconciliation='ticket_without_source') AS missing_source,
       coalesce(sum(extra_tickets),0) AS duplicates,
       count(*) FILTER(WHERE reconciliation='pending') AS pending,
       count(*) FILTER(WHERE reconciliation='unconfirmed_delivery') AS unconfirmed,
       count(*) FILTER(WHERE reconciliation='projection_issue') AS projection_issues
    FROM voc_monitor.source_reconciliation
) metrics CROSS JOIN LATERAL (VALUES ('티켓 없는 문의 원문',missing_ticket),('원문 없는 수집 티켓',missing_source),
    ('중복 발행 티켓',duplicates),('정제 처리 요청 대기',pending),('Flink 정제 처리 대기',unconfirmed),
    ('티켓 생성 보류',projection_issues)) AS v(label,value)
""", description="수집 원문을 업무 티켓과 대조합니다. Flink 정제 처리 대기는 발행 이후 현재 Silver 저장을 확인하지 못한 건입니다."),
    panel(11, "AI 모델 분석 대기", 12, 15, 12, 6, """
SELECT label AS "단계", q.pending AS "대기 작업",
       CASE WHEN q.pending=0 THEN '-' ELSE q.oldest_seconds::integer::text END AS "가장 오래된 대기 (초)"
FROM (VALUES ('classification','분류 요청'),('embedding','임베딩')) AS s(stage,label)
LEFT JOIN voc_monitor.queue_backlog q USING(stage)
""", description="발행 전인 감성·카테고리 작업을 셉니다. 한 티켓에서 두 작업이 생길 수 있습니다. 임베딩 대기 기록은 아직 연결되지 않았습니다."),
    prom(14, "Flink 마지막 성공 체크포인트", 0, 21, 6, 6, "voc_flink_last_checkpoint_seconds * 1000",
         kind="stat", unit="dateTimeAsLocal"),
    prom(15, "Flink 마지막 체크포인트 복원", 6, 21, 6, 6, "voc_flink_last_restore_seconds * 1000",
         kind="stat", unit="dateTimeAsLocal"),
    omit_prometheus_fields(prom(19, "Flink 작업·체크포인트 상태", 12, 21, 12, 6,
                                'voc_flink_job_state or voc_flink_checkpoints',
                                description="작업 상태와 성공·실패·복원 체크포인트 수를 표시합니다.")),
    collector_positions(),
    omit_prometheus_fields(prom(18, "Kafka 소비 지연", 0, 33, 24, 8, "voc_kafka_consumer_lag")),
    panel(20, "분류·임베딩 모델 실패 및 복구 기록", 0, 41, 24, 8, """
SELECT '미수집' AS "상태"
""", description="실패 시각과 복구 결과를 같은 작업에 연결하는 모델 기록이 필요합니다."),
    panel(23, "대조가 필요한 원본 키", 0, 49, 24, 8, """
SELECT event_id AS "원본 키",channel AS "채널",ticket_count AS "티켓",extra_tickets AS "중복 발행 티켓",
       CASE reconciliation WHEN 'source_without_ticket' THEN '티켓 없는 원문'
        WHEN 'ticket_without_source' THEN '원문 없는 티켓'
        WHEN 'unconfirmed_delivery' THEN 'Flink 정제 처리 대기'
        WHEN 'projection_issue' THEN '티켓 생성 보류' ELSE '대조 완료' END AS "대조 상태"
FROM voc_monitor.source_reconciliation
WHERE reconciliation IN ('source_without_ticket','ticket_without_source','unconfirmed_delivery','projection_issue')
   OR extra_tickets>0
ORDER BY received_at DESC NULLS FIRST LIMIT 100
"""),
]

for pipeline_panel in PIPELINES[1:]:
    pipeline_panel["gridPos"]["y"] -= 3


STAGE_DETAIL = panel(21, "단계 상세 · ${step:text}", 0, 0, 24, 18, f"""
SELECT "이벤트 시각","이벤트 종류","원본 이벤트 ID","Kafka 토픽","파티션","오프셋","재시도 횟수","상세"
FROM (
    SELECT observed_at AS "이벤트 시각",activity AS "이벤트 종류",event_id AS "원본 이벤트 ID",
           topic AS "Kafka 토픽",partition_id AS "파티션",kafka_offset AS "오프셋",
           NULL::bigint AS "재시도 횟수",reason AS "상세"
    FROM voc_monitor.diagnostics
    WHERE stage=${{step:sqlstring}} AND {period('observed_at')}
) events
UNION ALL SELECT NULL,'미수집',NULL,NULL,NULL,NULL,NULL,NULL
WHERE NOT EXISTS (SELECT FROM voc_monitor.diagnostics WHERE stage=${{step:sqlstring}} AND {period('observed_at')})
ORDER BY 1 DESC LIMIT 150
""", description="선택 단계의 저장 시각과 검토 기록을 표시합니다. 확인할 수 있는 Kafka 오프셋은 검토 기록에서 제공합니다.")

filter_variable = {
    "name": "step", "type": "custom", "label": "단계 상세", "hide": 2,
    "query": ",".join(f"{label} : {key}" for key, label in STEPS.items() if key != "ready"),
    "current": {"text": STEPS["capture"], "value": "capture"},
}


def inference_query(rollout=None):
    api = "classification" if rollout else "embedding"
    route = f"AND rollout='{rollout}' AND model_name IS NOT NULL AND model_version IS NOT NULL" if rollout else ""
    return f"""
WITH samples AS (
  SELECT * FROM voc_monitor.inference WHERE api='{api}' {route} AND traffic='operational'
  AND ended_at IS NOT NULL AND {period('started_at')}
)
SELECT nullif(count(*),0) AS "호출",
       round(count(*) FILTER(WHERE error IS NOT NULL)*100.0/nullif(count(*),0),2) AS "오류율 (%)",
       round(percentile_cont(0.95) WITHIN GROUP(ORDER BY response_ms)::numeric,1) AS "응답 P95 (ms)",
       round(percentile_cont(0.95) WITHIN GROUP(ORDER BY model_ms)::numeric,1) AS "모델 연산 P95 (ms)"
FROM samples
"""


INFERENCE = [
    text(1, "모델 호출과 품질",
         "현재 모델 계측을 기다리고 있습니다. 응답시간은 호출자가, 연산시간은 모델 API가 측정합니다.",
         y=0, h=3),
    panel(2, "Stable 분류", 0, 3, 8, 6, inference_query("stable")),
    panel(3, "Canary 분류", 8, 3, 8, 6, inference_query("canary")),
    panel(4, "임베딩 API", 16, 3, 8, 6, inference_query()),
    panel(5, "실제 모델 버전별 요청", 0, 9, 24, 8, f"""
WITH stats AS (
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
FROM voc_monitor.inference WHERE {period('started_at')}
GROUP BY api,rollout,model_name,model_version,traffic
)
SELECT * FROM stats
UNION ALL SELECT '미수집',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL WHERE NOT EXISTS(SELECT FROM stats)
ORDER BY 1,5,2,3,4
""", description="응답의 모델 버전과 실제 배포 구분에 따라 집계합니다. 오류로 버전을 확인하지 못한 요청은 별도 행에 표시합니다."),
    panel(6, "고정 평가셋 Macro-F1", 0, 17, 24, 7, f"""
WITH latest AS (
  SELECT run_id FROM voc_monitor.evaluation WHERE evaluated_at<={TIME_TO}
  GROUP BY run_id HAVING count(DISTINCT rollout)=2 ORDER BY max(evaluated_at) DESC LIMIT 1
)
SELECT initcap(e.rollout) AS "배포", e.model_name AS "모델",e.model_version AS "버전",e.dataset_version AS "고정 평가셋",
       round(max(macro_f1) FILTER(WHERE kind='sentiment')::numeric,4) AS "감성 Macro-F1",
       round(max(macro_f1) FILTER(WHERE kind='category')::numeric,4) AS "카테고리 Macro-F1",
       max(samples) FILTER(WHERE kind='sentiment') AS "감성 표본",
       max(samples) FILTER(WHERE kind='category') AS "카테고리 표본"
FROM latest JOIN voc_monitor.evaluation e USING(run_id) GROUP BY e.rollout,e.model_name,e.model_version,e.dataset_version
UNION ALL SELECT 'Stable',NULL,NULL,'미수집',NULL,NULL,NULL,NULL WHERE NOT EXISTS(SELECT FROM latest)
UNION ALL SELECT 'Canary',NULL,NULL,'미수집',NULL,NULL,NULL,NULL WHERE NOT EXISTS(SELECT FROM latest)
ORDER BY 1 DESC
""", description="사람이 따로 검수한 동일 평가셋에서 두 모델을 실행한 가장 최근 결과입니다."),
    panel(7, "상담사 수정률", 0, 24, 24, 7, f"""
WITH measurement AS (
    SELECT EXISTS(
        SELECT FROM voc_monitor.answer_completion WHERE {period('completed_at')}) AS tracked
), active AS (
    SELECT DISTINCT rollout,model_name,model_version FROM voc_monitor.inference
    WHERE rollout IS NOT NULL AND model_name IS NOT NULL AND model_version IS NOT NULL
      AND api='classification' AND traffic='operational' AND ended_at IS NOT NULL AND {period('started_at')}
), modifications AS (
    SELECT rollout,model_name,model_version,kind,count(*) AS shown,
       count(*) FILTER(WHERE displayed_value IS DISTINCT FROM final_value) AS changed
    FROM voc_monitor.modification WHERE {period('completed_at')}
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
    panel(8, "분류 API 연결 설정", 0, 31, 12, 4, """SELECT '미수집' AS "상태" """),
    panel(9, "임베딩 API 연결 설정", 12, 31, 12, 4, """SELECT '미수집' AS "상태" """),
]

if __name__ == "__main__":
    payloads = [
        dashboard("pipeline", PIPELINES),
        dashboard("pipeline-stage", [STAGE_DETAIL], template=[filter_variable], title="문의 처리 단계 상세"),
        dashboard("inference", INFERENCE),
        dashboard("deployment", [text(1, "", "## 준비 중", y=0, h=8)], refresh=""),
    ]
    for document in payloads:
        (ROOT / (document["uid"].removeprefix("voc-") + ".json")).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
