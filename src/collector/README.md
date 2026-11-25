# Discord·Gmail 상시 VOC 수집

Discord의 지정 채널에 사람이 작성한 일반 메시지와 답글을 수집한다. Gmail은 지정한 계정에서 사용자 라벨 이름이 정확히 **VOC**인 메일을 수집한다. 두 수집기 모두 테스트 표식 없이 작동한다. 실행 환경은 Docker Compose이며 호스트에는 Docker와 브라우저가 필요하다.

## 처음 실행

저장소 루트에서 다음 명령을 실행한다. 초기화는 .env에 채널 ID를 기록하고 Git에서 제외된 .local/collector에 DB 암호와 봇 토큰을 저장한다. 이미 설정한 값은 재사용한다. Discord 봇의 Message Content Intent와 해당 채널의 보기 권한을 켠다. 중단 시간의 누락분도 조회하려면 메시지 기록 보기 권한을 부여한다.

~~~bash
docker run --rm -it -v "$PWD:/project" -w /project python:3.12-slim python src/collector/prepare.py
docker compose build discord jobmanager
~~~

Gmail은 읽기 전용 OAuth를 쓴다. 기존 테스트의 .local/channel-probe/token.json이 있으면 바로 사용할 수 있다. 새 PC에서는 Gmail API를 활성화한 프로젝트의 데스크톱 OAuth 클라이언트 JSON을 .local/channel-probe/credentials.json에 놓고 다음 명령을 실행한다. 터미널이 출력하는 승인 주소를 같은 PC의 브라우저에서 열고 테스트 수신 사서함으로 승인한다. 승인 콜백은 호스트 루프백 포트 18765를 사용한다.

~~~bash
docker compose --profile setup run --rm --service-ports gmail-oauth
~~~

Gmail에 VOC 라벨을 만든 뒤 수집할 메일에 적용한다. 자동 적용은 Gmail 검색 옵션에서 수신 주소 등 규칙을 지정하고 **필터 만들기 → 라벨 적용 → VOC**를 선택한다. 첫 실행에서는 사서함 historyId를 저장한 뒤 라벨 대상 전체를 조회한다. 이후 15초마다 완료한 historyId 다음 변경을 조회한다. 새 메일과 기존 메일의 VOC 라벨 추가를 모두 확인한다. 라벨을 제거해도 이전에 적재한 원본은 보존한다. OAuth를 새로 승인해 토큰 파일을 교체하면 Gmail 컨테이너를 다시 만든다.

~~~bash
docker compose up -d --no-build
docker compose ps -a
docker compose logs --tail=20 discord gmail
~~~

init-db-topics의 종료 코드 0은 테이블 및 토픽 준비 완료를 뜻한다. discord·gmail의 healthy는 각 채널의 연결 또는 조회가 최근에 성공했다는 뜻이다. Flink 작업 상태는 로컬 브라우저에서 http://127.0.0.1:18081 로 확인한다.

## 처리와 저장

| 단계 | 실행 서비스 | 동작 |
| --- | --- | --- |
| 수집 | discord, gmail | Discord Gateway의 새 메시지 이벤트와 Gmail API의 full Message를 채널별 원형으로 받는다. 다른 채널, Discord 봇·웹훅·시스템 알림은 제외한다. Gmail 메일 본문은 VOC 라벨로 조회한 대상에서 가져온다. |
| 원본 발행 | publish | SQLite capture_outbox에서 미발행 원본을 가져와 voc.raw.discord 또는 voc.raw.gmail에 발행한다. Kafka ACK 뒤 published_at을 기록하고 대기열 본문을 비운다. 메시지 식별자는 중복 판정을 위해 남긴다. |
| Bronze | db-worker, PostgreSQL | voc.bronze_messages.raw_payload에 원본 JSONB를 보존한다. 최초 적재와 voc.standardize_outbox의 발행 대기 기록을 한 트랜잭션으로 저장하고 Kafka 위치를 커밋한다. |
| 표준화 요청 | db-worker | Bronze 원본, 최초 수신 시각, 확인된 역할과 본문 버전을 `voc.standardize.request.v4`에 발행한다. Kafka ACK 후 해당 이벤트·스키마 버전의 발행 시각을 기록한다. |
| 정제 | Flink JobManager, TaskManager | 정상 메시지는 19개 키의 JSON으로 `voc.standardized.v4`에 발행한다. 빈 본문, 잘못된 시각, 해석할 수 없는 MIME 등은 `voc.standardize.review.v4`로 보낸다. |
| Silver | db-worker, PostgreSQL | `voc.silver_messages`에 표준 필드를 컬럼별로 저장한다. `event_id`로 Bronze 원본을 찾고 `voc.review_events`에서 검토 사유를 확인한다. |
| 업무 티켓 | PostgreSQL | 확인된 고객 문의와 업무 채널이 연결되면 Silver 트랜잭션에서 고객, 티켓, 최초 문의 메시지, 모델 입력과 감성·카테고리 분석 대기 요청을 함께 저장한다. 수신 후에 업무 채널을 등록해도 기존 문의를 찾아 적재한다. |

**메시지 식별.** 회사·채널 유형·연결 계정 또는 서버·원천 메시지 ID로 같은 event_id를 계산한다. 다른 고객이 같은 글을 써도 서로 다른 원천 메시지 ID를 가지므로 별개로 저장한다. 전달은 재시도할 수 있으며, Bronze와 Silver의 유일 키로 같은 이벤트의 중복 행을 막는다.

**JSON 파일.** .local/collector/data/raw/<discord 또는 gmail>/<연결 해시>/<원천 메시지 ID>.json으로 채널 원본도 확인할 수 있다. 두 어댑터와 발행기는 같은 호스트 볼륨의 .local/collector/data/capture.sqlite3에 발행 대기열과 커서를 저장한다. 운영상 기준 원본은 PostgreSQL Bronze다. 원본 파일과 DB에는 고객이 작성한 내용이 들어 있으므로 로컬 접근을 제한한다.

### 공통 메시지 v4

| 공통 항목 | Discord | Gmail |
| --- | --- | --- |
| `schema_version`, `event_id` | 4, Bronze에서 받은 UUIDv5 | 동일 |
| `company_id`, `channel`, `connection_id` | 회사 ID, discord, 서버 ID | 회사 ID, gmail, 연결 사서함 |
| `source_message_id`, `source_conversation_id` | 메시지 ID와 실제 스레드 ID. 일반 채널의 새 글은 자기 ID, 답글은 확인된 최초 원글 ID | API 메시지 ID와 `threadId` |
| `source_channel_id`, `source_channel_name`, `reply_to_message_id` | 수신한 채널·스레드 ID, 수신 시 확인한 이름, 답글의 직접 부모 ID | 세 필드 모두 NULL |
| `sender_ref`, `sender_display_name` | 작성자 ID, 서버 별명 → 전역 이름 → 사용자명 순서의 표시 이름 | MIME `From`의 주소와 표시 이름 |
| `message_kind` | 확인한 고객 문의 또는 직원 답변, 근거가 없으면 `unknown` | 초안·자동 메일은 `other`, 보낸편지·연결 사서함 발신은 `staff_reply`, 그 밖에는 확인된 역할 또는 `unknown` |
| `title`, `body_text`, `body_version` | NULL, 메시지 본문, 최초 버전 1 | 제목 및 MIME 본문, 최초 버전 1. 평문을 우선하고 HTML만 있으면 텍스트를 추출한다. |
| `occurred_at`, `received_at` | 원문 작성 시각과 최초 수집 시각 | `internalDate`와 최초 수집 시각. 양 채널 모두 UTC 소수점 6자리, 원천 시각 누락 시 NULL |
| `source_metadata` | 빈 객체 `{}` | `label_ids`와 `to_addresses` 문자열 배열 |

Silver는 **메시지 한 건당 한 행**이다. 전체 표준 JSON은 Kafka에서 전달하고, PostgreSQL에는 19개 필드와 DB 적재 시각 `standardized_at`을 둔다. Silver에서 JSONB를 사용하는 필드는 `source_metadata`다. 원본은 Bronze의 `raw_payload`에서 조회한다. 부모 ID는 있지만 원글을 아직 확인하지 못한 Discord 답글은 `source_conversation_id = NULL`로 저장한다. 나중에 같은 회사·연결·채널에서 부모가 들어오면 후속 답글까지 연결한다.

`customer_inquiry` 한 건이 업무 티켓 하나가 된다. 같은 Gmail 스레드에서 온 후속 문의에도 새 번호를 발급한다. `voc.model_text_history`에는 `body_text`를 그대로 `model_text`로 저장하고, `voc.ticket_analysis_outbox`에는 본문 버전별로 감성과 카테고리 요청을 한 건씩 남긴다. 화면은 분석 결과가 준비되기 전부터 원문을 제공한다.

| 업무 테이블의 컬럼 | 티켓 생성 때 저장하는 값 |
| --- | --- |
| `TICKET.source_event_id`, `ticket_uuid`, `ticket_number` | Silver의 `event_id`를 원본 연결 키로 한 번만 기록한다. 업무 DB가 티켓 UUID와 회사별 고유 8자리 표시 번호를 발급한다. |
| `company_uuid`, `domain_uuid`, `channel_uuid`, `customer_uuid` | 등록된 수집 연결에서 회사·도메인·업무 채널을 확인하고, 같은 회사·채널의 `sender_ref`로 고객을 찾는다. |
| `title`, `status`, `created_at`, `updated_at` | 표시 제목은 50자 이내다. 제목이 없으면 본문의 첫 비어 있지 않은 줄에서 최대 40자를 사용한다. 표시할 문자가 없으면 ‘문의’를 사용한다. 초기 상태는 미처리, 두 시각은 최초 `received_at`이다. |
| `assigned_team_uuid`, `assignee_uuid`, `category_uuid`, `sentiment`, `embedding` | 초기값은 NULL이다. 감성·카테고리 분석 상태는 항목별 분류 대기로 시작한다. |
| `TICKET_MESSAGE.body`, `created_at` | 최초 고객 문의의 `body_text`와 `occurred_at`을 사용한다. 원천 시각이 없으면 `received_at`을 사용한다. |

#### Discord 예시

같은 회사의 테스트 서버에서 수집한 일반 메시지다. 아래 두 예시의 ID·본문·주소는 합성 데이터다.

~~~json
{
  "schema_version": 4, "event_id": "077d63e7-947e-5741-b60d-4600835449d7",
  "company_id": "encore", "channel": "discord", "connection_id": "555",
  "source_message_id": "101", "source_conversation_id": "101",
  "source_channel_id": "444", "source_channel_name": "문의", "reply_to_message_id": null,
  "message_kind": "unknown", "sender_ref": "777", "sender_display_name": "시험 고객",
  "title": null, "body_text": "배송 상태를 확인해 주세요.", "body_version": 1,
  "occurred_at": "2026-11-24T01:02:00.012000Z", "received_at": "2026-11-24T01:02:03.456789Z",
  "source_metadata": {}
}
~~~

#### Gmail 예시

수신 당시 VOC 라벨과 수신처는 알 수 있지만, 발신자의 업무 역할은 아직 확인하기 전이다.

~~~json
{
  "schema_version": 4, "event_id": "d05e49bd-410b-5c65-be41-7667bbced38f",
  "company_id": "encore", "channel": "gmail", "connection_id": "help@example.test",
  "source_message_id": "a1", "source_conversation_id": "same-thread",
  "source_channel_id": null, "source_channel_name": null, "reply_to_message_id": null,
  "message_kind": "unknown", "sender_ref": "buyer@example.test", "sender_display_name": "메일 고객",
  "title": "배송 문의", "body_text": "배송 상태를 확인해 주세요.", "body_version": 1,
  "occurred_at": "1970-01-01T00:00:01.001000Z", "received_at": "2026-11-24T01:02:03.456789Z",
  "source_metadata": {"label_ids": ["VOC", "INBOX"], "to_addresses": ["help@example.test", "archive@example.test"]}
}
~~~

## 기존 직원에게 티켓 조회 권한을 부여하고 수집 채널 연결하기

DB 관리자가 저장소 루트에서 `docker compose exec postgres psql -X -U voc -d voc`로 접속한다. 새 환경의 첫 회사·직원 등록은 [저장소 실행 가이드](../../README.md)를 따른다. 같은 psql 세션에서 조회 결과를 확인한 뒤 각 `\set`의 UUID를 실제 값으로 바꿔 실행한다.

먼저 실제 직원과 소속 회사를 확인하고 이 직원에게 `ticket.read`를 부여한다.

~~~sql
\set ON_ERROR_STOP on
SELECT s.staff_user_uuid,s.email,c.name AS company,c.domain_uuid
FROM public.staff_user s JOIN public.company c USING(company_uuid) ORDER BY s.email;
\set employee '직원_UUID'
SELECT company_uuid FROM public.staff_user WHERE staff_user_uuid=:'employee' \gset
INSERT INTO public.staff_permission(staff_user_uuid,permission_uuid)
SELECT :'employee',permission_uuid FROM public.permission WHERE code='ticket.read'
ON CONFLICT DO NOTHING;
~~~

회사의 도메인이 비어 있을 때만 다음 블록으로 도메인을 연결한다. 새 산업 도메인이 필요하면 `INSERT INTO public.domain(name) VALUES ('소매') RETURNING domain_uuid;`에서 이름을 실제 값으로 바꿔 등록한다.

~~~sql
SELECT domain_uuid,name FROM public.domain ORDER BY name;
\set domain_id '산업_도메인_UUID'
UPDATE public.company SET domain_uuid=:'domain_id'
WHERE company_uuid=:'company_uuid' AND domain_uuid IS NULL;
~~~

다음으로 원천·발신자를 대조하여 해당 회사의 실제 고객 문의 한 건을 고른다. 이 문의의 수집 경로를 같은 회사의 업무 채널에 연결한다. Gmail은 사서함, Discord는 서버와 실제 방·스레드 ID로 연결된다. 업무 채널을 처음 만들 때는 아래에서 `channel_id`를 입력하기 전에 `INSERT INTO public.channel(company_uuid,name,type) VALUES (:'company_uuid','문의 접수',:'src_channel') RETURNING channel_uuid;`를 실행한다.

~~~sql
SELECT event_id,company_id,channel,connection_id,source_channel_id,sender_ref,title,message_kind,received_at
FROM voc.silver_messages WHERE schema_version=4 ORDER BY received_at DESC LIMIT 20;
\set inquiry '확인한_이벤트_UUID'
SELECT company_id,channel,connection_id,coalesce(source_channel_id,'') AS room
FROM voc.silver_messages WHERE event_id=:'inquiry' AND schema_version=4 \gset src_
SELECT channel_uuid,name,type FROM public.channel WHERE company_uuid=:'company_uuid';
\set channel_id '업무_채널_UUID'
INSERT INTO voc.ticket_source_connection(company_id,channel,connection_id,source_channel_id,company_uuid,channel_uuid)
VALUES (:'src_company_id',:'src_channel',:'src_connection_id',:'src_room',:'company_uuid',:'channel_id')
ON CONFLICT DO NOTHING;
UPDATE voc.standardize_outbox SET verified_message_kind='customer_inquiry'
WHERE event_id=:'inquiry' AND target_schema_version=4 AND EXISTS (
    SELECT FROM voc.ticket_source_connection WHERE company_id=:'src_company_id' AND channel=:'src_channel'
      AND connection_id=:'src_connection_id' AND source_channel_id=:'src_room'
      AND company_uuid=:'company_uuid' AND channel_uuid=:'channel_id'
) RETURNING event_id;
~~~

워커가 재정제한 후 발급된 번호를 확인한다. 미등록 경로·누락된 발신자·미설정 회사 도메인은 `reason`에 표시된다. 설정을 보완한 뒤 같은 건만 재시도하려면 `SELECT voc.project_ticket(:'inquiry');`를 실행한다.

~~~sql
SELECT s.message_kind,t.ticket_number,t.status,i.reason
FROM voc.silver_messages s LEFT JOIN public.visible_ticket t ON t.source_event_id=s.event_id
LEFT JOIN voc.ticket_projection_issue i ON i.event_id=s.event_id
WHERE s.schema_version=4 AND s.event_id=:'inquiry';
~~~

## 확인과 재처리

~~~bash
docker compose exec -T postgres psql -U voc -d voc -c "
SELECT b.channel, b.source_message_id, b.origin, o.published_at IS NOT NULL AS sent,
       s.schema_version, s.body_version, s.message_kind
FROM voc.bronze_messages b
JOIN voc.standardize_outbox o ON o.event_id=b.event_id AND o.target_schema_version=4
LEFT JOIN voc.silver_messages s ON s.event_id=b.event_id AND s.schema_version=4
ORDER BY b.received_at DESC LIMIT 20;"
docker compose exec -T postgres psql -U voc -d voc -c "SELECT reason, count(*) FROM voc.review_events WHERE schema_version=4 GROUP BY reason;"
docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --list
docker compose exec -T discord python -m unittest infra.channel_probe.test_probe collector.test_collection
~~~

저장소 루트에서 `python3 src/collector/test_silver_v4_integration.py`를 실행하면 합성 Discord·Gmail 메시지로 Kafka → Flink → PostgreSQL 경로를 검증한다. 임시 Compose 프로젝트는 고유한 이름과 별도 볼륨을 쓴다. 기존 v1 DDL 파일을 `--v1-schema 파일경로`로 전달하면 v1 이관도 검증한다.

### 중단 후 재시작과 기존 설치 전환

- **Discord**는 지정 서버·채널(또는 개별 스레드)의 마지막 REST 확인 메시지 ID 다음부터 복구한다. 대상 메시지와 페이지 커서는 한 SQLite 트랜잭션으로 저장한다. 같은 메시지가 Gateway에서 먼저 들어와도 REST 커서는 이력 확인 뒤에만 전진한다. 기존 설치는 과거 파일 커서를 가져오고, 새 설치는 최초 시작 시각부터 수집한다. 기록 조회 권한이 없으면 커서를 유지하고 그 구간을 capture_issue에 기록한다. 이미 삭제된 메시지는 Discord에서 복원할 수 없다.
- **Gmail**은 사서함·라벨별로 bootstrap 또는 active 단계와 historyId를 남긴다. 최초 전체 조회 및 이력 조회 도중 종료되면 저장한 기준점부터 재조회하고, 확보한 메일은 event_id로 건너뛴다. 마지막 이력 페이지까지 확보해야 새 historyId를 커밋한다. 만료 응답(404)은 capture_issue에 기록하고 현재 라벨 전체를 다시 대조한다. 조회 전에 라벨이 사라졌거나 메일이 삭제된 경우도 사유로 기록한다.
- **발행기**는 published_at이 비어 있는 행을 다시 보낸다. Kafka ACK 직후 프로세스가 종료돼 재전송되더라도 Bronze의 고유 키가 중복 적재와 중복 정제 요청을 막는다.

기존 **파일 대기열로 운영한 설치**를 전환할 때는 PostgreSQL이 가동된 상태에서 아래 순서를 사용한다. capture-migrate는 기존 pending 파일의 원래 봉투를 가져오고, Bronze에서 확인한 식별자에만 발행 확인을 기록한다. 이 경우 published_at은 기존 적재 사실을 확인한 시각이다. 이관은 반복할 수 있으며 기존 raw·pending·커서 파일을 보존한다. 새 설치는 위의 처음 실행 절차를 사용한다.

~~~bash
docker compose build discord
docker compose stop discord gmail publish
docker compose --profile setup run --rm --no-deps capture-migrate
docker compose up -d --no-deps --no-build --force-recreate discord gmail publish
~~~

**Silver v1에서 전환할 때** 기존 DB 워커와 Flink 작업을 먼저 멈춘다. PostgreSQL·Kafka·수집 대기열의 기존 볼륨을 사용한다.

~~~bash
docker compose build db-worker jobmanager
docker compose stop db-worker taskmanager jobmanager
docker compose run --rm --no-deps init-db-topics
docker compose up -d --no-build
~~~

초기화는 과거 Silver와 요청을 `voc.silver_messages_v1`, `voc.standardize_outbox_v1`에 보존하고 **모든 Bronze 원본**에 대해 v4 요청을 한 번씩 등록한다. 이전 검토 기록도 버전 1로 보존한다. 초기화를 반복해도 이미 발행한 v4 요청의 상태는 유지된다.

한 건을 다시 정제할 때는 해당 `event_id`와 `target_schema_version = 4` 행의 `published_at`을 NULL로 바꾼다. 수신 경로와 고객을 확인한 뒤 같은 행의 `verified_message_kind`를 지정하면 해당 건만 다시 발행된다. 같은 버전의 동일 본문은 기존 행을 재사용하고, 같은 버전의 다른 본문은 검토 기록을 남긴다. 승인된 본문 정정에는 새 `body_version`을 사용한다.

DB와 Kafka는 Compose 내부 네트워크에서 접근한다. 토큰은 서비스별 읽기 전용 비밀 파일로 마운트하며 Docker 빌드 컨텍스트에는 필요한 소스 파일만 포함한다. 이 구성은 단일 브로커·단일 DB를 사용하는 로컬 프로토타입이다.
