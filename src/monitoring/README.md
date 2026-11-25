# VoC 모니터링

Grafana는 파이프라인, AI 추론, 카나리 배포 화면을 제공한다. 파이프라인의 단계 상자를 누르면 같은 기간의 기록을 확인할 수 있다. 카나리 배포는 `준비 중`으로 표시한다.

## 기존 수집 프로젝트에서 실행

기존 Compose 프로젝트가 PostgreSQL 초기화를 마친 상태에서 저장소 루트에서 실행한다. 수집기가 별도 프로젝트 이름을 쓰면 모니터링 명령에도 같은 이름을 지정한다.

~~~bash
(
  umask 077
  mkdir -p .local/monitoring
  chmod 700 .local/monitoring
  for key in reader-password grafana-password; do
    test -s ".local/monitoring/$key" || openssl rand -hex 32 > ".local/monitoring/$key"
    chmod 644 ".local/monitoring/$key"
  done
)
docker compose -f compose.yaml -f src/monitoring/compose.monitoring.yaml up -d --build grafana
~~~

- Grafana [파이프라인](http://127.0.0.1:13000/d/voc-pipeline/pipeline) · [AI 추론](http://127.0.0.1:13000/d/voc-inference/inference) · [카나리 배포](http://127.0.0.1:13000/d/voc-deployment/deployment)
- Prometheus [수집 대상](http://127.0.0.1:19090/targets)

Grafana 초기 로그인 이름은 `admin`이다. 초기 비밀번호는 `.local/monitoring/grafana-password`에 있다. 정해진 포트가 이미 사용 중이면 `VOC_GRAFANA_PORT`, `VOC_PROMETHEUS_PORT`를 지정한다.

`monitor-db`는 모니터링 전용 스키마 `voc_monitor`와 조회 계정 `voc_monitor_reader`를 구성한다. Grafana의 DB 계정은 모니터링 뷰만 읽는다. 지표 수집기는 Kafka 조회, Flink REST 조회와 수집 상태 볼륨의 읽기 전용 마운트를 사용한다.

독립 테스트 Compose 프로젝트를 시작할 때도 원본 Compose의 `DISCORD_CHANNEL_ID` 보간이 필요하다. 모니터링 서비스만 시작하는 테스트 프로젝트에서는 `DISCORD_CHANNEL_ID=0`을 지정한다. 포트와 비밀번호 파일 경로는 다른 프로젝트와 구분한다.

## 파이프라인 화면

기본 시간 범위는 현재 시각까지 최근 10분이다. 화면은 15초마다 갱신한다.

| 화면 | 확인 가능한 기록 |
| --- | --- |
| 수집 | 보존 원문의 수집 시각, Gmail·Discord 상태, 연결별 SQLite 위치 |
| 원문 저장 | 현재 원문 수, 정제 요청 등록과 Kafka 전달 기록 |
| Flink 정제 | 최초 Silver 저장, 검토 이벤트, Flink 작업 상태와 체크포인트 |
| 티켓 저장 | 현재 원본에 연결된 티켓 수와 보류 기록. CSV 등록은 대조 범위에서 제외한다. |
| 분류·임베딩 | 감성·카테고리 대기 작업을 센다. 대기가 없으면 `0`, 가장 오래된 대기는 `-`로 표시한다. 임베딩 대기는 `미수집`으로 표시한다. |
| 결과 저장 | 연결된 최종 결과 시각이 없어 `미수집`을 표시한다. |

채널별 수집 위치에는 선택한 기간에 상태가 기록된 채널만 표시한다. 위치 기록이 없는 채널은 수집 상태만 표시한다.

정제 요청은 본문 버전이 바뀌거나 메시지 종류가 승인되면 다시 등록될 수 있다. Kafka 전달 DB 시각은 전달 확인을 저장한 트랜잭션의 시각이다. Silver 시각은 최초 DB 저장 시점이다. 이 구간에는 Kafka 소비와 DB 저장 시간이 함께 들어간다.

단계별 처리 시간은 Kafka 전달 시각과 Silver 저장 시각을 각 구간의 완료 시각으로 쓴다. Silver 저장 전에도 Kafka 전달 표본을 센다. 완료 표본이 없으면 건수는 `0`, P95는 `-`로 표시한다.

`Flink 정제 성공`은 선택한 기간에 처음 Silver에 저장한 원문 중 현재 고객 문의로 분류된 원문을 센다. Flink 작업 상태는 조회 종료 시점의 상태다. 실패·복원 체크포인트 수는 Flink의 누적값이다.

마지막 체크포인트 복원 시각이 있으면 해당 시각을 표시한다. 시각이 없고 Flink가 복원 횟수 0을 보고하면 `0건`으로 표시한다. 나머지는 `미수집`으로 표시한다.

전체 처리 시간과 30초 목표는 선택한 기간의 수집 기록을 확인한다. 기록이 없으면 P95는 `-`, 건수는 `0`, 완료율은 `0%`로 표시한다. 수집 기록이 있으면 분류·임베딩의 최종 완료 시각을 확인할 수 없어 `미수집`으로 표시한다.

`Flink 정제 처리 대기`는 발행한 정제 요청에 해당하는 Silver 저장 기록을 확인하지 못한 상태다. 티켓 생성 보류 기록은 원문 대조에서 별도로 집계한다.

Kafka 지연은 커밋 오프셋과 보관 중인 로그 범위로 계산한다. 커밋 오프셋이 없는 파티션은 지연 값을 비워 둔다. 검토 기록이 가진 Kafka 파티션·오프셋을 단계 상세에 표시한다. 개별 처리의 재시도 횟수는 `미수집`으로 표시한다.

## AI 추론 데이터 연결

AI 추론 화면은 외부의 모델 측정 기록을 기다리는 동안 `미수집`을 표시한다. 다음 읽기 뷰의 구조는 [schema.sql](schema.sql)에 있다.

| 읽기 뷰 | 외부 기록에 필요한 값 |
| --- | --- |
| `inference` | 분류·임베딩 API, 운영·재생·고정 평가 구분, 실제 배포 경로와 버전, 시작·종료 시각, 응답시간, 모델 연산시간, 오류 |
| `evaluation` | 동일 검수 평가셋으로 실행한 두 배포의 모델 버전, 감성·카테고리별 Macro-F1과 표본 수 |
| `modification` | 답변한 직원이 본 예측과 답변완료 시점의 감성·카테고리 값, 모델 버전과 완료 시각 |
| `answer_completion` | 수정률의 수집 여부를 판단할 답변완료 시각 |

AI 호출 오류율의 분모는 완료 호출이다. 미완료는 별도로 표시한다. 응답시간은 호출자가 측정하며 연산시간은 모델이 보고한 값만 쓴다. 품질 평가와 재생 요청은 운영 오류율과 응답시간에서 제외한다.

## 확인

~~~bash
python3 src/monitoring/build_dashboards.py
uv run --no-project --python 3.12 --with confluent-kafka==2.16.2 python -m unittest discover -s src/monitoring
curl -fsS http://127.0.0.1:13000/api/health
curl -fsS 'http://127.0.0.1:19090/api/v1/query?query=up'
~~~

접속 상태와 보고 권한을 확인하려면 해당 테스트 프로젝트에서 아래 명령을 실행한다.

~~~bash
docker compose -f compose.yaml -f src/monitoring/compose.monitoring.yaml exec -T postgres \
  psql -X -U voc -d voc -v ON_ERROR_STOP=1 < src/monitoring/smoke.sql
~~~
