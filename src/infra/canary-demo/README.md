# Mac에서 감성 모델 카나리 시연하기

Mac의 Docker에서 전용 k3d 클러스터 `voc-canary-demo`를 실행한다. 감성은 Stable과 Canary에 나누고 카테고리는 Stable이 처리한다. 포함된 모델은 버전 구분과 배포 검증을 위해 항상 감성 `중립`, 카테고리 코드 `delivery`를 반환한다.

실제 체크포인트와 상담사 무작위 배정 시연은 [KLUE 모델 실행 절차](klue/README.md)를 따른다.

## 실행

Docker 엔진을 시작하고 `k3d`, `kubectl`, `helm`, Python 3.9 이상을 준비한다. 저장소 루트에서 실행한다. 첫 설치는 Argo CD와 컨테이너 이미지를 다운로드한다.

```sh
bash src/infra/canary-demo/up.sh
python3 src/infra/canary-demo/demo.py verify
export KUBECONFIG="$PWD/.local/canary/kubeconfig"
python3 src/infra/canary-demo/observability/verify.py
```

첫 명령은 전용 PostgreSQL과 Git 미러를 준비하고 초기 감성 배분율을 0%로 설정한다. 두 번째 명령은 실행마다 새 식별자로 합성 티켓 50건을 적재하고, 감성 50건과 카테고리 50건이 업무 DB에 반영될 때까지 확인한다. 전용 DB에는 시험 이력이 쌓인다. 실행이 끝나면 감성 배분율은 다시 0%다. 점검을 중단한 경우 아래 `set-weight 0`으로 복구할 수 있다.

| 확인할 내용 | 방법 |
| --- | --- |
| Argo CD가 적용한 커밋과 앱 상태 | `kubectl -n argocd get application voc-classification -o wide` |
| 이번 실행의 건수·최장 시간·Git 커밋 | `cat .local/canary/last-run.json` |
| 버전별 처리량·오류·p95 응답시간·누적 호출 수 | `kubectl -n voc-ml port-forward --address 127.0.0.1 svc/canary-grafana 18300:3000` 실행 후 [Grafana 대시보드](http://127.0.0.1:18300/d/voc-canary-demo) 열기 |
| 상담사 로그인과 실제 티켓 화면 | [전용 Django 실행 및 로그인](desk/README.md). `desk.py setup` 후 `desk.py verify` 또는 `desk.py open`을 실행한다. |

Grafana의 관측 데이터는 Prometheus Pod의 임시 볼륨에 최대 6시간 보관한다. 대시보드에서 실행 시각을 포함한 범위를 선택한다. Kubernetes API와 두 화면의 포트 포워딩은 Mac의 루프백 주소를 사용한다. 시연을 마친 뒤 `k3d cluster stop voc-canary-demo`로 리소스를 반환할 수 있다. 다음 시연 때 `up.sh`가 같은 클러스터와 PostgreSQL 볼륨을 다시 사용한다.

## 티켓 한 건이 처리되는 과정

1. 기존 Flink 파이프라인이 Silver v4에 고객 문의를 저장하면 PostgreSQL 트리거가 티켓과 `voc.model_text_history`, `voc.ticket_analysis_outbox`를 같은 트랜잭션에 기록한다. 시연 도구는 이 트리거의 입력인 Silver 레코드와 연결된 Bronze 레코드를 합성한다. Discord·Gmail 수집과 Flink 실행은 별도 파이프라인이다.
2. 워커는 지정한 회사의 outbox에서 감성·카테고리 작업을 읽고, 해당 본문 버전의 `model_text`(Silver `body_text`)를 가져온다. 감성은 회사·이벤트 ID의 해시를 0~99로 나눈 값이 설정 비율보다 작으면 Canary로 간다. 작업마다 모델 버전·주소·요청 ID를 DB에 고정한다.
3. `POST /v1/predict`에는 작업 ID·분석 항목·도메인·본문을 보내고, 응답의 작업 ID·항목·모델 버전과 레이블을 검증한다. 카테고리 코드는 도메인별 `voc.classification_category_map`에서 업무 카테고리 UUID로 바꾼다. [요청·응답 예시 및 레이블 계약](../../contracts/classification-v1.md)
4. 작업 결과와 호출 이력은 각각 `voc.classification_job`과 `voc.classification_attempt`에 기록한다. 최신 버전이면서 상담사가 결과를 수정하지 않은 경우 `public.ticket`의 `sentiment`, `category_id`, 해당 처리 상태를 반영한다. Django 목록·상세가 이 값을 조회한다.

재시작 후에는 처리 기한이 지난 작업을 다시 획득한다. HTTP 호출은 실패 시 같은 작업 ID·버전으로 반복될 수 있고, 업무 DB는 회사·이벤트·전처리 버전·본문 버전·분석 항목 조합으로 작업을 한 번 생성한다. 이전 실행의 늦은 응답은 현재 시도 번호를 확인한 후 처리한다.

## Git에서 배분율 바꾸기

```sh
python3 src/infra/canary-demo/demo.py set-weight 10
python3 src/infra/canary-demo/demo.py set-weight 50
python3 src/infra/canary-demo/demo.py set-weight 0
```

각 명령은 전용 Git 미러의 `feature/local-model-canary`에서 `values-local.yaml`의 `routing.sentiment.canaryPercent`를 지정한 값으로 커밋한다. Argo CD가 그 SHA의 Helm 차트를 동기화하고 새 비율의 워커가 준비되면 명령이 끝난다. 0%로 되돌리면 이후에 배정되는 감성 작업이 모두 Stable로 간다. 이미 배정된 작업은 최초 버전에서 마친다.

`verify`는 0 → 10 → 50 → 0% 각 단계와 전환 도중에 요청을 보낸다. 양쪽 모델을 재현성 있게 호출하도록 해시 구간을 고른다. 출력의 10%·50%는 해당 단계의 배분 임계값이며 전체 100건의 비율은 단계별 표본 구성에 따라 달라진다. 기본 성공 기준은 outbox 등록부터 업무 DB 결과 저장까지 건당 30초다. 예를 들어 팀에서 정한 시간이 10초라면 `demo.py verify --deadline-seconds 10`으로 실행한다. Grafana p95는 모델 HTTP 응답시간을 측정한다.

팀 GitHub로 전환할 때는 차트와 이미지를 먼저 게시하고 [Argo CD Application](../argocd/voc-classification-local.yaml)의 저장소·대상 브랜치를 확인한다. 전용 클러스터에서 해당 Application을 적용한 뒤 대상 브랜치에 [values-local.yaml](../helm/voc-classification/values-local.yaml) 변경을 커밋·푸시하면 Argo CD가 같은 방식으로 반영한다. 이때 비율 변경은 팀 Git에서 관리한다. `demo.py set-weight`와 `verify`는 로컬 미러를 시연하는 명령이다. [GitHub Actions](../../../.github/workflows/model-canary.yml)는 PR에서 계약 테스트와 Helm 검증을 실행하고, 수동 실행에서 이미지 게시를 선택하면 GHCR에 빌드 결과를 올린다.

## 실제 모델을 인수할 때

1. 팀원에게 Stable·Canary 각각의 가중치 또는 CPU 서빙 이미지, 고정 버전, 토크나이저·전처리 버전, 감성 및 도메인별 카테고리 매핑, 입력과 기대 레이블 예시를 받는다. 현재 배분 정책에서 Stable은 두 분석 항목, Canary는 감성을 처리해야 한다.
2. 가중치를 받았다면 서빙 이미지에 모델 의존성과 Python 로더를 추가하고 `models.stable.backend` 또는 `models.canary.backend`에 로더를 지정한다. 이미 서빙 이미지를 받았다면 [API 계약](../../contracts/classification-v1.md)과 같은 경로로 호출할 수 있는지 확인한다. Apple Silicon의 Pod에서 Linux ARM64 CPU로 모델 로드와 워밍업을 마친 뒤 버전별 샘플의 응답 버전·레이블을 확인한다.
3. [Helm values](../helm/voc-classification/values.yaml)의 `models.stable`, `models.canary`에 각각 릴리스 버전과 이미지 digest를 지정한다. 로컬 이미지는 클러스터에 import하고 레지스트리 이미지는 Pod가 pull할 수 있게 한다. 모델 크기에 맞춰 메모리와 CPU를 설정한다. 도메인의 카테고리 코드를 DB에 등록하고 [Prometheus 대상](observability/prometheus.yml)을 새 모델 서비스 이름으로 갱신한다.
4. 라벨이 붙은 사례로 감성과 카테고리 정확도를 확인하고, 합의한 응답시간 안에 실제 추론이 끝나는지 측정한다. `verify`와 Django·Grafana 자동 점검에는 고정 응답 모델의 버전·기대값이 들어 있다. 실제 모델 인수 시험에서는 팀의 표본과 기대값을 사용한다.
5. 감성 비율을 단계적으로 늘리고 Grafana와 업무 티켓 값을 대조한다. 문제가 나타나면 0%로 되돌린다. 이전 버전으로 배정된 `pending`·`running` 작업이 끝난 뒤 그 버전의 서비스를 내린다.
