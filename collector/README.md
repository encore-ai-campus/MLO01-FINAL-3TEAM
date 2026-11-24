# Discord·Gmail 상시 VOC 수집

Discord의 지정 채널에 사람이 작성한 일반 메시지와 답글을 수집한다. Gmail은 지정한 계정에서 사용자 라벨 이름이 정확히 **VOC**인 메일을 수집한다. 두 수집기 모두 테스트 표식 없이 작동한다. 실행 환경은 Docker Compose이며 호스트에는 Docker와 브라우저가 필요하다.

## 처음 실행

저장소 루트에서 다음 명령을 실행한다. 초기화는 .env에 채널 ID를 기록하고 Git에서 제외된 .local/collector에 DB 암호와 봇 토큰을 저장한다. 이미 설정한 값은 재사용한다. Discord 봇의 Message Content Intent와 해당 채널의 보기 권한을 켠다. 중단 시간의 누락분도 조회하려면 메시지 기록 보기 권한을 부여한다.

~~~bash
docker run --rm -it -v "$PWD:/project" -w /project python:3.12-slim python -m collector.prepare
docker compose build discord jobmanager
~~~

Gmail은 읽기 전용 OAuth를 쓴다. 기존 테스트의 .local/channel-probe/token.json이 있으면 바로 사용할 수 있다. 새 PC에서는 Gmail API를 활성화한 프로젝트의 데스크톱 OAuth 클라이언트 JSON을 .local/channel-probe/credentials.json에 놓고 다음 명령을 실행한다. 터미널이 출력하는 승인 주소를 같은 PC의 브라우저에서 열고 테스트 수신 사서함으로 승인한다. 승인 콜백은 호스트 루프백 포트 18765를 사용한다.

~~~bash
docker compose --profile setup run --rm --service-ports gmail-oauth
~~~

Gmail에 VOC 라벨을 만든 뒤 수집할 메일에 적용한다. 자동 적용은 Gmail 검색 옵션에서 수신 주소 등 규칙을 지정하고 **필터 만들기 → 라벨 적용 → VOC**를 선택한다. 처음 시작할 때 이미 라벨이 있는 메일도 대상으로 하며, 이후 15초마다 해당 라벨에서 추가된 메일을 조회한다. 라벨을 제거해도 이전에 적재한 원본은 보존한다. OAuth를 새로 승인해 토큰 파일을 교체하면 Gmail 컨테이너를 다시 만든다.

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
| 원본 발행 | publish | 디스크 대기열에서 원본 및 출처를 가져와 voc.raw.discord 또는 voc.raw.gmail에 발행한다. 브로커의 확인 후 대기 항목을 제거한다. |
| Bronze | db-worker, PostgreSQL | voc.bronze_messages.raw_payload에 원본 JSONB를 보존한다. 최초 적재와 voc.standardize_outbox의 발행 대기 기록을 한 트랜잭션으로 저장하고 Kafka 위치를 커밋한다. |
| 표준화 요청 | db-worker | 미발행 행에서 원본과 추적 ID를 읽어 voc.standardize.request.v1에 실어 보낸다. Kafka 확인 후 published_at을 기록한다. |
| 정제 | Flink JobManager, TaskManager | Flink SQL 작업 내 변환 함수가 채널별 원본을 공통 메시지로 바꾼다. 통과하면 voc.standardized.v1, 필수값 누락·빈 본문·해석할 수 없는 MIME 등은 voc.standardize.review.v1로 보낸다. |
| Silver | db-worker, PostgreSQL | voc.silver_messages에 표준 메시지를 한 행씩 적재한다. 분류 대기 상태는 pending이다. 검토 사유는 voc.review_events에서 원본과 연결한다. |

**메시지 식별.** 회사·채널 유형·연결 계정 또는 서버·원천 메시지 ID로 같은 event_id를 계산한다. 다른 고객이 같은 글을 써도 서로 다른 원천 메시지 ID를 가지므로 별개로 저장한다. 전달은 재시도할 수 있으며, Bronze와 Silver의 유일 키로 같은 이벤트의 중복 행을 막는다.

**JSON 파일.** .local/collector/data/raw/<discord 또는 gmail>/<연결 해시>/<원천 메시지 ID>.json으로 채널 원본도 확인할 수 있다. pending은 재시도용 디스크 대기열이다. 운영상 기준 원본은 PostgreSQL Bronze다. 원본 파일과 DB에는 고객이 작성한 내용이 들어 있으므로 로컬 접근을 제한한다.

### 공통 메시지 v1

| 공통 항목 | Discord | Gmail |
| --- | --- | --- |
| source_message_id | 메시지 id | Message.id |
| source_conversation_id | 지정 스레드는 스레드 ID, 일반 채널의 글은 그 글의 ID | threadId |
| reply_to_message_id | 명시적으로 답글을 단 대상의 message_id | 별도 값 없이 threadId로 연결 |
| title / body_text | 빈 제목과 content | Subject 및 중첩 MIME의 본문. plain을 우선하고 HTML은 텍스트로 변환하며 첨부 파일은 분리한다. |
| sender_ref / occurred_at | 작성자 ID와 Discord timestamp | From 주소와 internalDate의 UTC 시각 |
| event_id / received_at | 두 채널 공통으로 Bronze 추적 키와 최초 수신 시각 | 동일 |

Silver의 단위는 개별 메시지다. 일반 채널의 답글 관계는 별도 필드로 유지한다. 첨부만 있는 문의도 Bronze에 보관하고 분석 본문이 비면 검토로 보낸다. 감정·카테고리 모델은 이 수집 서비스와 독립적으로 분류 대기 건을 소비할 수 있다.

## 확인과 재처리

~~~bash
docker compose exec -T postgres psql -U voc -d voc -c "
SELECT b.channel, b.source_message_id, b.origin, o.published_at IS NOT NULL AS sent,
       s.schema_version, s.classification_status
FROM voc.bronze_messages b
JOIN voc.standardize_outbox o USING(event_id)
LEFT JOIN voc.silver_messages s USING(event_id)
ORDER BY b.received_at DESC LIMIT 20;"
docker compose exec -T postgres psql -U voc -d voc -c "SELECT reason, count(*) FROM voc.review_events GROUP BY reason;"
docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --list
docker compose exec -T discord python -m unittest tools.channel_probe.test_probe collector.test_collection
~~~

중단 뒤에도 PostgreSQL·Kafka·Flink의 데이터를 Docker 볼륨에서, 수집 대기 파일과 Discord 복구 커서를 호스트 바인드 폴더에서 다시 사용한다. 첫 실행 이후 Discord 기록 조회 권한이 있으면 연결 공백을 15초 간격으로 보완한다. 이때 origin은 discord_history이고 raw_payload는 REST가 실제 반환한 메시지 객체다. 실시간 discord_gateway 원본에서는 메시지가 d 안에 있다. 중단 도중 삭제되어 Discord에서 조회할 수 없는 글은 복구할 수 없다. Gmail은 현재 라벨이 보이는 메일부터 다시 검사한다.

필요한 원본 한 건만 재표준화하려면 해당 event_id의 voc.standardize_outbox.published_at을 NULL로 바꾼다. 같은 스키마 버전의 재전달은 기존 Silver 행을 보존한다. 표준 필드를 바꾸는 마이그레이션은 스키마 버전을 올려 별도로 수행한다.

DB와 Kafka는 Compose 내부 네트워크에서 접근한다. 토큰은 서비스별 읽기 전용 비밀 파일로 마운트하며 Docker 빌드 컨텍스트에는 필요한 소스 파일만 포함한다. 이 구성은 단일 브로커·단일 DB를 사용하는 로컬 프로토타입이다.
