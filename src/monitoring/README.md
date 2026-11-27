# VoC 모니터링

Grafana는 파이프라인, AI 추론, 카나리 배포 화면을 제공한다. 카나리 배포 화면은 팀 Prometheus에서 `job="voc-live-model"`의 준비 상태, 감성 호출 비율과 모델 연산 P95를 조회한다.

### 회사별 모델 운영 기록

관리자 웹에 `VOC_GRAFANA_URL`을 지정하면 모델 교체 검토 화면에서 해당 회사와 업무가 선택된 [모델 운영 기록](http://127.0.0.1:13000/d/voc-model-guard/model-guard)을 열 수 있다. Grafana 로그인이 필요하다. 이 대시보드는 운영자가 쓰며, 회사 필터는 표시 범위를 선택한다. 최근 30분의 실측 HTTP p95, 7일간의 호출당 청구 비용 집계, 실제 후보 배정률, 복귀 시각 및 수집 ID별 분류 이력을 제공한다. 비용은 모델 엔드포인트가 응답 헤더 `X-Inference-Cost-KRW`로 해당 호출의 청구 금액을 제공한 경우에 기록한다. 금액이 누락된 구간은 미수집으로 표시한다.

로컬에서 실제 체크포인트로 분류하려면 기존 Compose에 `src/workers/classification/compose.classification.yaml`을 추가한다. `VOC_CLASSIFICATION_COMPANY`에는 수집 경로가 연결된 회사 UUID, `VOC_STABLE_MODEL_DIR`과 `VOC_CANARY_MODEL_DIR`에는 체크포인트 디렉터리, `VOC_DOMAIN_MAP_FILE`에는 업무 도메인 UUID와 라벨을 연결한 JSON 파일을 지정한다. 두 버전 이름은 `STABLE_MODEL_VERSION`과 `CANARY_MODEL_VERSION`에 지정한다. 아래 명령은 저장소 루트에서 실행한다.

```sh
docker compose -f compose.yaml -f src/monitoring/compose.monitoring.yaml -f src/workers/classification/compose.classification.yaml up -d --build publish db-worker jobmanager taskmanager classification-worker canary-model grafana
```

감성과 세부유형의 후보 배정 비율은 각각 `SENTIMENT_CANARY_PERCENT`와 `CATEGORY_CANARY_PERCENT`로 정한다. 세부유형을 켤 때는 후보 모델이 해당 분류를 지원하도록 `CANARY_ANALYSIS_KINDS=sentiment,category`와 도메인 매핑을 함께 지정한다. 두 배정률의 기본값은 0이다.

워커는 회사·업무·후보 버전마다 최근 30분에 서로 다른 문의 3건이 연속 실패하면 후보 배정을 0%로 고정한다. 성공한 후보 호출은 이전 실패 연속 기록을 끊고, 같은 문의의 재시도는 한 건으로 센다. 실제 서빙 버전을 회사 기준과 연결하면 최소 호출량 이후의 후보 오류율·p95 상한도 적용한다. 복귀 후 새 호출과 대기 중인 재시도는 기존 모델을 사용한다. 이미 전송된 호출은 실제 시도 이력에 남는다. 같은 후보를 재시작해도 차단은 유지하며, 새 후보를 배포할 때는 고유한 릴리스 버전을 지정한다. 합성 재생은 기본적으로 운영 판정에서 제외한다. 다른 회사의 업무와 분리한 로컬 검증 워커에서만 `VOC_GUARD_REPLAYS=1`로 재생 장애 시험을 할 수 있다.

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

채널별 수집 위치에는 Gmail과 Discord 행을 항상 표시한다. 연결별로 행을 유지하고 저장된 위치를 표시한다. 선택한 기간에 상태 기록이 없으면 수집 상태는 `미수집`, 수집 단계는 `-`로 표시한다.

정제 요청은 본문 버전이 바뀌거나 메시지 종류가 승인되면 다시 등록될 수 있다. Kafka 전달 DB 시각은 전달 확인을 저장한 트랜잭션의 시각이다. Silver 시각은 최초 DB 저장 시점이다. 이 구간에는 Kafka 소비와 DB 저장 시간이 함께 들어간다.

단계별 처리 시간은 Kafka 전달 시각과 Silver 저장 시각을 각 구간의 완료 시각으로 쓴다. Silver 저장 전에도 Kafka 전달 표본을 센다. 완료 표본이 없으면 건수는 `0`, P95는 `-`로 표시한다.

`Flink 정제 성공`은 선택한 기간에 처음 Silver에 저장한 원문 중 현재 고객 문의로 분류된 원문을 센다. Flink 작업 상태는 조회 종료 시점의 상태다. 실패·복원 체크포인트 수는 Flink의 누적값이다.

마지막 체크포인트 복원 시각이 있으면 해당 시각을 표시한다. 시각이 없고 Flink가 복원 횟수 0을 보고하면 `0건`으로 표시한다. 나머지는 `미수집`으로 표시한다.

전체 처리 시간과 30초 목표는 선택한 기간의 수집 기록을 확인한다. 기록이 없으면 P95는 `-`, 건수는 `0`, 완료율은 `0%`로 표시한다. 수집 기록이 있으면 분류·임베딩의 최종 완료 시각을 확인할 수 없어 `미수집`으로 표시한다.

`Flink 정제 처리 대기`는 발행한 정제 요청에 해당하는 Silver 저장 기록을 확인하지 못한 상태다. 티켓 생성 보류 기록은 원문 대조에서 별도로 집계한다.

Kafka 지연은 커밋 오프셋과 보관 중인 로그 범위로 계산한다. 커밋 오프셋이 없는 파티션은 지연 값을 비워 둔다. 검토 기록이 가진 Kafka 파티션·오프셋을 단계 상세에 표시한다. 개별 처리의 재시도 횟수는 `미수집`으로 표시한다.

## AI 추론 데이터 연결

AI 추론 화면은 분류 워커의 작업과 시도 이력을 `request_id`로 연결한다. 실제 응답 모델과 적용된 감성·카테고리를 티켓 번호별로 표시한다. 합성 검증 문의는 재생으로 집계한다. 분류 테이블을 나중에 설치했다면 `monitor-db`를 다시 실행해 읽기 뷰를 연결한다. [모니터링 스키마](schema.sql)

실채널 PoC에서는 `voc-live-ml` 안의 Prometheus Agent가 두 ClusterIP 서비스의 `/metrics`를 15초마다 읽고 팀 Prometheus의 `/api/v1/write`로 전송한다. 에이전트의 Helm 차트는 [chart](../infra/canary-demo/klue/monitoring/chart)에 있다. `remoteWriteUrl`에는 클러스터에서 접근 가능한 내부 수신 주소를 지정한다. 수신 측 Prometheus는 `--web.enable-remote-write-receiver`를 사용한다. 모델의 수집 결과는 팀 Prometheus에서 `up{job="voc-live-model"}`로 확인한다.

로컬 k3d에서 팀 Compose로 전송할 때는 [수신기 Compose](compose.model-metrics.yaml)를 함께 사용한다. Docker 네트워크에서 수신기에 할당할 빈 IP를 `VOC_MONITOR_BRIDGE_IP`, 송신 노드의 IP 또는 CIDR을 `VOC_MONITOR_NODE_CIDR`로 지정한다. 기본 k3d 네트워크 이름은 `k3d-voc-canary-demo`다. 저장소 루트에서 실행한다.

```sh
docker compose -f compose.yaml -f src/monitoring/compose.monitoring.yaml -f src/monitoring/compose.model-metrics.yaml up -d prometheus model-metrics-receiver
helm upgrade --install voc-model-agent src/infra/canary-demo/klue/monitoring/chart -n voc-live-ml \
  --set-string remoteWriteUrl="http://${VOC_MONITOR_BRIDGE_IP}:9201/api/v1/write"
```

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
