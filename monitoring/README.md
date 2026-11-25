# VoC 모니터링

Grafana는 **파이프라인**, **AI 추론**, **카나리 배포** 세 화면을 제공한다. 카나리 배포는 `준비 중`을 표시한다.

## 이 PC에서 열기

저장소 루트에서 실행한다. 미리보기는 별도 Compose 프로젝트 `voc-monitoring-review`와 `.local/monitoring-review` 데이터를 쓴다.

~~~bash
./scripts/monitoring-review lite --build
~~~

- Grafana [파이프라인](http://127.0.0.1:13000/d/voc-pipeline/pipeline) · [AI 추론](http://127.0.0.1:13000/d/voc-inference/inference) · [카나리 배포](http://127.0.0.1:13000/d/voc-deployment/deployment)
- Prometheus [수집 대상](http://127.0.0.1:19090/targets)

Lite 모드는 PostgreSQL·Grafana·Prometheus와 지표 수집기만 시작한다. 운영 데이터가 없는 미리보기는 `미수집`을 표시한다.
Grafana의 로컬 접속은 조회 전용이며 PostgreSQL 보고 뷰는 원문·고객 계정·인증값을 제공하지 않는다.

전체 로컬 파이프라인을 테스트하려면 아래 명령을 쓴다. Gmail·Discord 외부 연동은 두 명령 모두 실행하지 않는다.

~~~bash
./scripts/monitoring-review full --build
./scripts/monitoring-review compose ps -a
~~~

전체 모드에서는 Flink를 [18181 포트](http://127.0.0.1:18181), 티켓 업무 화면을 [18182 포트](http://127.0.0.1:18182)에서 연다.
명명된 Compose 프로젝트에 있는 서비스를 종료할 때는 `./scripts/monitoring-review compose stop`을 쓴다. 데이터는 유지한다.

### Docker 테스트 환경

`voc-monitoring-test` 프로젝트는 별도 DB와 테스트 계정을 사용한다. 샘플 문의는 실제 Kafka와 Flink를 통과한다.
테스트가 모델 응답과 회신을 흉내 내므로, 팀 모델과 채널 계정 없이 상담 흐름을 확인할 수 있다.

~~~bash
./scripts/monitoring-test start
./scripts/monitoring-test seed
./scripts/monitoring-test password
~~~

- [티켓 목록](http://127.0.0.1:28082/tickets/)에서 `agent@voc-test.invalid`와 위 명령에 나온 비밀번호로 로그인한다.
- [파이프라인](http://127.0.0.1:23000/d/voc-pipeline/pipeline) · [AI 추론](http://127.0.0.1:23000/d/voc-inference/inference) · [Flink](http://127.0.0.1:28081/)을 열어 처리 흐름을 확인한다.
- `seed`를 실행할 때마다 새 문의가 들어온다. 분류·임베딩을 마친 뒤 상담사 화면에서 답변을 보낼 수 있다.
- `seed`가 출력하는 `batch` 값으로 티켓 제목을 검색한다. 오래된 배치의 지표는 Grafana에서 조회 기간을 넓혀 확인한다.

| 제목 끝 | 상담사 화면에서 할 일 | 재학습 후보 |
| --- | --- | --- |
| `KEEP` | 감성과 카테고리를 그대로 두고 답변을 보낸다. | 확인 후 유지 두 개 |
| `CHANGE` | 카테고리를 결제 문의로 저장한 뒤 답변을 보낸다. | 감성 확인 후 유지, 카테고리 직접 수정 |
| `RETURN` | 카테고리를 결제 문의로 저장했다가 배송 문의로 되돌리고 답변을 보낸다. | 확인 후 유지 두 개 |
| `FAIL` | 분류 실패로 보이는 문의의 카테고리를 비워 두고 답변을 보낸다. | 감성 확인 후 유지 한 개 |

~~~bash
./scripts/monitoring-test candidates
./scripts/monitoring-test status
./scripts/monitoring-test stop
~~~

`candidates`는 후보와 근거를 JSON 줄로 출력한다. `stop`은 테스트 서비스만 멈추고 볼륨은 보존한다.
테스트 DB는 모든 시각을 `timestamptz`로 저장하며 조회 세션과 로그의 시간대는 UTC다.
상담사 화면은 운영과 같은 한국 시간으로 표시한다. 실제 채팅 종료 화면은 별도 서비스가 연결될 때 확인할 수 있다.

### 기존 수집 프로젝트에 연결

기존 프로젝트에 이 브랜치를 적용한 뒤, 기존 Compose 프로젝트 이름과 볼륨 설정으로 실행한다.
아래 명령은 기존 PostgreSQL과 수집 경로를 사용한다. 별도 포트를 사용하므로 이 PC의 미리보기도 열어 둘 수 있다.

~~~bash
(
  umask 077
  mkdir -p .local/monitoring
  for key in reader-password grafana-password; do
    test -s ".local/monitoring/$key" || openssl rand -hex 32 > ".local/monitoring/$key"
  done
)
VOC_GRAFANA_PORT=13001 VOC_PROMETHEUS_PORT=19091 \
  docker compose -f compose.yaml -f compose.monitoring.yaml up -d --build \
  grafana db-worker publish taskmanager classify embed ticket-desk
~~~

연결 후 [기존 프로젝트의 Grafana](http://127.0.0.1:13001/d/voc-pipeline/pipeline)에서 확인한다. Gmail·Discord 인증과 수집기 시작 절차는 [채널 수집 가이드](../src/collector/README.md)를 따른다.

## 화면에서 확인할 값

| 화면 | 확인할 값 |
| --- | --- |
| 파이프라인 | 기본 최근 10분이다. 유입 12분 전·완료 2분 전인 문의는 유입 0건·완료 1건이다. |
| 단계 상세 | 단계 상자를 누르면 해당 단계의 Kafka 전달·실패·재시도 기록을 확인할 수 있다. 유입·완료 건수는 흐름도에서 확인한다. |
| 원문과 티켓 대조 | 저장 원문과 실제 티켓을 양방향으로 비교한다. 같은 원본에서 티켓이 3건 생기면 `중복 발행 티켓`은 2건이다. |
| 정제·분석 대기 | 정제 요청이 Kafka 발행이나 Flink의 소비를 기다리면 `정제 처리 요청 대기`로 집계한다. Flink 처리 여부를 확인하지 못한 원문은 `Flink 정제 처리 대기`로 표시한다. 분류·임베딩 대기는 `AI 모델 분석 대기` 표에서 확인한다. |
| 수집과 복구 | `채널별 수집 위치`에서 수집 상태와 마지막 위치를 확인한다. Flink의 마지막 성공·복원 시각과 작업·체크포인트 상태는 한 행에 표시한다. |
| 모델 실패 및 복구 | `분류·임베딩 모델 실패 및 복구 기록`에서 모델 종류, 실패 사유, 실패와 복구 시각을 확인한다. |
| 전체 30초 목표 | 원본 등록부터 결과 저장까지 측정한다. 30초가 지나도 분석이 끝나지 않은 문의는 목표 미달이다. |
| AI 추론 | API 응답과 연산시간을 표시한다. 분류 응답이 보고한 실제 배포와 버전으로 분류하고, 임베딩은 별도로 표시한다. |
| 모델 품질 | 고정 평가셋의 Macro-F1과 운영 상담사의 항목별 수정률을 표시한다. 상담사가 답변하기 전에 본 예측만 집계한다. |

단계의 실제 처리 이력이나 서비스 동작 기록이 있는 기간에만 `0건`을 표시한다. 값이나 연결이 아직 없으면 `미수집`이다.
답변완료 이력을 수집하는 상태에서 조회 기간 해당 버전의 표시 표본이 없으면 수정률은 `표본 없음`이다.
이전 DB의 보존 티켓과 원문은 현재 대조에 포함한다. 이전 단계 완료 시각과 과거 모델 버전은 소급 입력하지 않는다.

Git에 포함된 Canvas를 수정할 때 `monitoring/build_dashboards.py`를 편집하고 다시 생성한다.

~~~bash
python3 monitoring/build_dashboards.py
~~~

## 팀 모델 연결

| 환경 변수 | 값 |
| --- | --- |
| `VOC_CLASSIFIER_STABLE_URL` | Stable 분류 API의 POST 주소 |
| `VOC_CLASSIFIER_CANARY_URL` | Canary 분류 API의 POST 주소 |
| `VOC_CLASSIFIER_ROUTER_URL` | 첫 시도를 처리할 분류 라우터의 POST 주소 |
| `VOC_CANARY_PERCENT` | 라우터가 없을 때 Canary로 보낼 비율. 기본값은 0 |
| `VOC_EMBEDDING_URL` | 별도 임베딩 API의 POST 주소 |
| `VOC_MODEL_API_TOKEN` | 필요할 때만 쓰는 모델 API Bearer 토큰 |

분류 API는 `title`, `text`, `domain_id`, `kinds`를 JSON으로 받는다. 요청 받은 항목의 결과를 `sentiment`, `category_id`로 반환한다.
감성 값은 긍정·중립·부정·매우 부정 중 하나다. 카테고리 값은 해당 도메인에 등록한 UUID이며 `미분류`도 실제 등록 UUID를 쓴다.

응답에 `model_name`, `model_version`, `rollout`을 포함한다. `rollout` 값은 `stable` 또는 `canary`다.
응답 헤더 `X-Model-Name`, `X-Model-Version`, `X-Model-Rollout`로 같은 정보를 줄 수 있다.
서버가 직접 측정한 모델 연산시간은 `model_ms`로 보낸다. 실패 응답의 헤더에도 실제 버전이 확인되면 포함할 수 있다.

임베딩 API는 `title`, `text`를 받고, 유한한 수의 배열 `embedding`과 `model_name`, `model_version`을 돌려준다.
임베딩 워커는 알려진 발신자 이름·주소와 일반적인 이메일·전화번호·주민등록번호 패턴을 가린다.
벡터와 버전은 티켓에 함께 저장한다. 이미 저장한 기존 벡터는 유지하며, 누락 벡터만 별도로 채운다.

두 워커는 병렬로 실행한다. 분류는 Stable 주소를 설정해야 출발한다. 재시도는 Stable로 직접 보낸다.
Stable 주소나 임베딩 주소가 비어 있으면 해당 작업을 대기 상태로 둔다. Worker 중단으로 결과가 불명확한 시도는 완료 호출 집계에서 제외한다.
모델 연결 후 미리보기에서는 `./scripts/monitoring-review full --build`로 적용한다.
기존 수집 프로젝트는 `docker compose up -d --build classify embed`로 분석 워커를 적용한다.
정상 수집한 시간 범위의 무발생은 `0건`이다. Stable만 설정한 경우 Canary 무발생은 `미수집`이다.

## 평가와 재생

사람이 독립 검수한 정답 파일을 `.local/monitoring-review/eval`에 두고 아래 명령을 쓴다.

~~~bash
./scripts/monitoring-review compose run --rm classify evaluate /eval/reviewed.json
./scripts/monitoring-review compose run --rm classify replay 원본-event-UUID
./scripts/monitoring-review compose run --rm embed retry-embedding 원본-event-UUID
~~~

평가 JSON의 구조는 다음과 같다. 검수 근거에는 검수 작업이나 승인한 기록의 참조를 적는다.

~~~json
{
  "dataset_version": "검수셋-버전",
  "samples": [
    {
      "id": "입력-고유-ID",
      "domain_id": "도메인-UUID",
      "title": "검수한 입력 제목",
      "text": "검수한 입력 본문",
      "labels": {
        "sentiment": {
          "id": "항목별-정답-ID",
          "value": "중립",
          "reviewer": "검수자-참조",
          "evidence": "검수-근거-참조",
          "origin": "independent_review"
        }
      }
    }
  ]
}
~~~

카테고리 정답은 `labels.category`에 기록하고 `value`에 등록한 카테고리 UUID를 넣는다. 실제 검수된 항목만 등록한다.
같은 버전에 다른 내용을 덮어쓸 수 없다. 두 모델이 완주하면 각 항목에 점수를 기록한다. 검수 정답이 없는 항목은 `미수집`이다.
평가와 Canary 재생은 별도 호출로 보관한다. 티켓, 운영 원본 수, 상담사 수정률 집계에는 반영하지 않는다.

## 답변완료 연결

`ticket.reply` 권한이 있는 직원은 공개 티켓의 분류값을 수정하거나 미입력 상태로 남길 수 있다.
티켓 스크롤 화면에 실제로 보이는 분류값은 현재 직원에 귀속한다. 새로고침과 반복 조회가 최초값을 바꾸지 않는다.

회신 API가 준비되면 티켓 서비스에 `VOC_REPLY_URL`을 설정한다. 인증이 필요할 때만 `VOC_REPLY_TOKEN`을 추가한다.
상세 화면에서 답변을 제출하면 원래 문의의 채널·계정·메시지·대화 참조와 답변 본문을 JSON으로 보낸다.
연결된 어댑터는 같은 `Idempotency-Key` 요청을 한 번만 발송해야 한다. 확인 결과는 `delivery_confirmed=true`와 같은 `provider_delivery_id`를 돌려준다.
확인된 경우에만 티켓 메시지, 발송 확인 시각, 답변완료 스냅샷을 함께 저장한다. 미확인 후 다시 제출하면 같은 키와 같은 본문을 보낸다.
기존 발송기가 따로 있으면 현재 직원과 티켓의 발송 확인 메시지를 `ticket_message`에 저장한다.
`delivery_status='delivered'`와 `sent_at`을 기록하면 상세에서 답변완료를 연결할 수 있다.

## 재학습 후보

Gmail·Discord는 발송을 확인한 답변완료에서 감성과 카테고리를 따로 저장한다.
완료 상담사가 본 운영 예측이 최종값과 같으면 출처는 `상담사 확인 후 유지`다.
예측값으로 되돌렸더라도 완료 상담사가 그 예측을 봤으면 같은 출처를 쓴다.
상담사가 직접 수정·지정한 값은 모델 예측이 없어도 후보에 들어간다.
티켓의 후보는 해당 버전의 모델 입력, 완료 답변, 상담사, 근거 시각과 예측 시도를 연결한다.
후보는 운영 수정률과 별도다. 고정 평가셋 정답은 독립 검수한 `independent_review`만 받는다.

기존 DB는 연결 대상 Compose 프로젝트에서 스키마를 먼저 적용한다. 기존 완료 건은 별도 명령으로 확인한다.

~~~bash
docker compose run --rm --no-deps --build init-db-topics schema
docker compose up -d --build classify embed ticket-desk
company_uuid='조회할 회사 UUID'
docker compose exec ticket-desk python manage.py training_candidates "$company_uuid" --limit 100
docker compose exec ticket-desk python manage.py backfill_training_candidates "$company_uuid"
~~~

`training_candidates`는 회사별 후보의 식별자, 분류값, 근거를 JSON 줄로 출력한다.
티켓 입력은 `voc.model_text_history`의 회사·원본·전처리 버전·본문 버전으로 찾는다.
과거 후보 명령은 기본으로 최대 100개의 근거 있는 미저장 항목 수를 보여 준다.
확인된 답변완료에 당시 예측 표시 또는 직접 지정 근거와 입력 버전을 연결할 수 있을 때 집계한다.
대상 회사와 건수를 확인한 뒤 명령에 `--apply`를 붙이면 저장한다.

### 채팅 서비스 연결

채팅 서버에서 실제 대화를 종료할 때 `apps.chat.training.record_chat_end(session_token, event)`를 호출한다.
`ChatClosed`는 안정적인 종료 UUID, 대화 UUID, 회사 UUID, 도메인, 종료 상담사, 종료 시각을 담는다.
`ChatInput`의 UUID, 버전, SHA-256은 최초 고객 메시지의 확정 입력 스냅샷을 가리킨다.
채팅 서비스는 해당 스냅샷과 예측 요청의 연결을 보존하고 UUID와 버전으로 다시 조회할 수 있어야 한다.
`labels`에 항목별 최종값과 저장한 운영 예측, 실제 화면 표시, 마지막 직접 지정 근거를 전달한다.
채팅 서버가 채팅 종료 권한을 확인하고 저장한 기록을 사용한다.
어댑터는 직원 세션과 종료 상담사·회사·도메인을 비교한다.
같은 종료 이벤트는 한 번 저장한다. 같은 종료 UUID에 내용이 달라지면 충돌로 처리한다.
현재 브랜치는 서버 어댑터를 제공한다. 실제 채팅 화면과 팀 서비스 연결은 연결 작업이 필요하다.

## 저장·복구와 검증

수집 커서는 SQLite가 확정한 마지막 위치다. 대기열에서 복구할 수 있는 위치이며 Kafka 저장 확인은 별도다.
Grafana는 정확한 커서를 연결별 최신값 한 행에서 읽는다. Prometheus는 연결별 마지막 갱신 시각을 수집한다.
Flink Kafka 출력은 완료된 체크포인트의 트랜잭션을 사용한다. 원본별 최초 단계 시각은 중복 전달과 재처리에서 보존한다.
Flink 자동 작업 복구는 실행 중인 JobManager가 담당한다. 로컬 전체 재기동은 유지한 체크포인트 또는 Kafka 그룹 위치로 재개할 수 있다.
구형 Flink SQL 작업의 체크포인트는 새 Java 작업의 구조와 다르다. 구형 작업에서 처리한 Kafka 그룹 위치를 새 작업이 읽는다.
Kafka 위치가 남아 있는 기존 DB에서는 정제 결과와 티켓을 중복 생성하지 않는다.

~~~bash
./scripts/monitoring-review compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U voc -d voc < src/collector/test_ticket_projection.sql
curl -fsS http://127.0.0.1:13000/api/search
curl -fsS 'http://127.0.0.1:19090/api/v1/query?query=up'
./scripts/monitoring-review compose run --rm --no-deps -e VOC_RUN_MONITORING_TESTS=1 --entrypoint python metrics -m unittest collector.test_evaluation
~~~

Django 티켓 테스트는 임시 DB에서 회신 미연결·미확인·재시도·성공 및 실제 표시 확인을 검사한다.
`VOC_TICKET_TEST_ADMIN_DSN`을 임시 DB 생성이 가능한 테스트 인스턴스로 지정하고 `src/backend`에서 실행한다.

~~~bash
python manage.py test tests.test_tickets tests.test_training_candidates --verbosity 2
~~~
