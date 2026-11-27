# VoC 학습과 리니지

## 실행 환경

- PostgreSQL의 VoC DB에 기존 수집·티켓 스키마와 `collector/training_schema.sql`을 적용합니다. collector 초기화도 이 스키마를 적용합니다.
- Feast Registry와 MLflow Registry에는 같은 PostgreSQL 서버의 `feast_registry`, `mlflow` 데이터베이스를 각각 사용합니다.
- MinIO에 데이터셋 버킷과 MLflow 모델 버킷을 따로 만듭니다. 학습 파일을 모델 버전 삭제와 독립적으로 보관합니다.
- 분류 워커는 기존 worker 스키마를 다시 적용합니다. 숫자 피처를 쓰는 버전만 Helm의 `models.stable.customerFeatures` 또는 `models.canary.customerFeatures`를 켭니다.
- 숫자 피처 Canary를 먼저 켜면 스냅샷이 없는 기존 문의는 기존 Stable 모델에 남습니다. 기존 작업을 처리한 뒤 새 모델을 Stable로 승격합니다.
- 숫자 피처를 켠 워커에는 `worker.feastSecret`의 `FEAST_REGISTRY_URL`, `FEAST_REDIS_CONNECTION`을 전달합니다. Feast 로딩을 고려해 워커 메모리 한도를 조정합니다.

학습 컨테이너는 다음 환경 변수를 사용합니다.

| 변수 | 내용 |
|---|---|
| `DATABASE_URL` | VoC PostgreSQL DSN |
| `FEAST_REGISTRY_URL` | `postgresql+psycopg://` 형식의 Feast Registry URL |
| `FEAST_REDIS_CONNECTION` | Feast Redis 연결 문자열. 예시는 `redis:6379,db=0`입니다. |
| `MLFLOW_TRACKING_URI` | MLflow 서버 주소 |
| `MLFLOW_S3_ENDPOINT_URL` | MinIO의 S3 API 주소 |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | 데이터셋·모델 버킷 자격 증명 |
| `VOC_DATASET_BUCKET` | 학습·평가 Parquet 저장 버킷 |

학습 작업은 모델 파일을 MinIO에서 직접 읽고 씁니다. MLflow의 PostgreSQL URL에는 `postgresql+psycopg2://`를 사용합니다. 서버는 다음 방식으로 시작합니다.

관리자 웹과 재학습 스케줄러에도 같은 MLflow 주소와 모델·평가셋 버킷의 읽기 권한을 설정합니다. 시작 모델은 READY 상태, 소유 회사 또는 공통 인코더 리비전, MLmodel과 실제 체크포인트·토크나이저를 확인합니다. `VOC_TRAINING_DOMAINS_FILE`로 학습 워크플로와 동일한 도메인 설정 파일을 마운트하면 파인튜닝 모델의 분류 헤드와 라벨 순서, 평가셋 각 행의 도메인·세부유형 매핑도 확인합니다. 고정 평가셋은 회사, 파일 해시, Parquet 스키마, 행 수, 감성·세부유형 정답과 입력값을 확인합니다. 보관용 벤치마크는 모델 교체 검토에서 조회합니다.

```sh
mlflow server --backend-store-uri "$MLFLOW_BACKEND_DB" \
  --no-serve-artifacts --default-artifact-root s3://voc-model-artifacts --host "$MLFLOW_BIND_ADDRESS"
```

## 한 번 준비하는 작업

```sh
docker build -f src/ml/training/Dockerfile -t voc-training:local .
export PYTHONPATH=src:src/ml/serving
python -m ml.training.workflow register-features
python -m ml.training.workflow register-base
```

`register-base`는 고정 리비전의 KLUE-BERT를 등록합니다. 처음 등록하면 `models:/voc-bert-base/1`로 찾습니다.

고정 평가셋은 별도 검수한 Gold 정답별로 `gold_label_uuid`, `reviewer_uuid`, `evidence_ref`를 적은 JSON 배열로 지정합니다.

```sh
python -m ml.training.workflow freeze-eval --company "$COMPANY_UUID" --reviewed reviewed.json
```

Argo namespace에 환경 변수를 담은 `voc-training-env` Secret과 `voc-training-domains` ConfigMap을 준비합니다. ConfigMap의 `domains.json` 형식은 아래와 같습니다.

```json
{
  "<domain-uuid>": {
    "task": "commerce",
    "categories": {
      "<category-uuid>": "delivery_delay"
    }
  }
}
```

카테고리 코드는 해당 도메인의 `voc.classification_category_map`과 일치시킵니다. 파일을 바꾸면 새 Workflow에서 실행합니다.

```sh
kubectl -n "$ARGO_NAMESPACE" apply -f src/infra/argo-workflows/voc-training.yaml
argo submit -n "$ARGO_NAMESPACE" --from workflowtemplate/voc-training \
  -p company="$COMPANY_UUID" -p eval-dataset="$EVAL_VERSION" \
  -p label-cutoff="$LABEL_CUTOFF_UTC"
```

이미 확정한 학습 자료를 반복하려면 `label-cutoff` 대신 `train-dataset`을 지정합니다. 시작 모델은 고정 Registry 버전으로 지정합니다.

## 티켓 데스크에서 실행

`STAFF_USER.role = 'super_admin'` 계정으로 왼쪽 사이드바의 **모델 학습**을 엽니다. 기준 시각, 시작 모델 버전, 고정 평가셋을 선택합니다. 화면은 기준 시각을 `Asia/Seoul`로 읽고 Workflow의 `label-cutoff`에 전달합니다.

제출 후 실행 상세에서 데이터 준비와 모델 학습을 봅니다. 최근 실행 목록은 같은 회사가 이 화면에서 제출한 최근 10건을 표시합니다.

| 데스크 환경 변수 | 내용 |
|---|---|
| `MLFLOW_TRACKING_URI` | MLflow Registry를 제공하는 서버 주소 |
| `MLFLOW_TRACKING_TOKEN` | 서버가 베어러 인증을 사용하면 필요한 조회 토큰 |
| `ARGO_WORKFLOWS_URL` | Argo Server API 주소 |
| `ARGO_WORKFLOWS_NAMESPACE` | `voc-training` 템플릿이 있는 namespace. 기본값은 `argo`입니다. |
| `ARGO_WORKFLOWS_TOKEN` | Argo가 실제 제출 권한을 확인할 때 사용하는 베어러 토큰 |
| `ARGO_WORKFLOWS_TOKEN_FILE` | Argo 토큰 파일 경로. 기본값은 `/run/secrets/argo_workflows_token`입니다. |
| `ARGO_WORKFLOWS_UI_URL` | 브라우저에서 접속하는 Argo Web UI 주소 |
| `MLFLOW_UI_URL` | 브라우저에서 접속하는 MLflow UI 주소. 기본값은 `MLFLOW_TRACKING_URI`입니다. |
| `MINIO_CONSOLE_URL` | 브라우저에서 접속하는 MinIO 콘솔 주소 |
| `VOC_TRAINING_MODEL_NAME` | 결과 모델의 Registry 이름. 기본값은 `voc-bert`입니다. |

Argo의 client 인증을 사용하는 환경에서는 템플릿에 포함된 `voc-training-web` 서비스 계정 토큰을 전달합니다. 환경 변수로 토큰을 주면 그 값을 사용합니다. 경로로 설정하면 데스크가 제출할 때마다 파일을 읽어 갱신을 반영합니다.

데이터베이스에 `voc.training_progress` 스키마를 적용하고 Argo 매니페스트의 웹 서비스 계정에 Workflow 목록 권한을 적용합니다. 학습 이미지와 데스크를 갱신하면 새 실행의 세부 상태가 표시됩니다.

데스크는 준비된 KLUE-BERT 버전과 해당 회사의 VoC 학습 버전을 조회합니다. VoC 버전에서 이어 학습하려면 피처 전처리와 라벨 매핑이 일치해야 합니다.

## 검수와 보관

분석·배정 수정 화면은 펼친 입력 버전과 감성·카테고리 변경 근거를 저장합니다. 긴급 여부를 선택하고 검수 사유를 입력하면 `voc.counselor_review`에도 기록합니다. 발송 성공을 확인할 때 해당 버전과 일치하는 감성·카테고리를 Gold로 확정합니다. 기존 `매우 부정` 라벨은 학습 정답에서 `부정`으로 합칩니다.

운영 수집은 마이그레이션 이후 신규 티켓부터 시작합니다. 피처·정답이 확정된 파일을 사용해 학습을 다시 실행할 수 있습니다. 기존 행의 물리 삭제는 Gold와 데이터셋 외래 키를 포함한 보관 정책으로 관리합니다. Bronze 원문은 원본 ID로 별도 보관소에서 찾습니다.

```sh
python -m ml.training.workflow trace --name voc-bert --version 1 --feature-uuid "$FEATURE_UUID"
```

## 격리 검증

`python -m ml.training.smoke_fixture`는 검증용 DB에 합성 Gmail·Discord 입력과 검수 정답을 만듭니다. 출력의 `domains`와 `eval_reviews`를 각각 설정 파일과 고정 평가 명세로 저장합니다. `cutoff`를 Argo에 전달하고 `max-steps=1`로 한 학습 단계를 확인합니다.

`VOC_TRAINING_TEST_DSN="$DATABASE_URL" python -m unittest ml.training.test_history`로 7일 경계와 입력 버전 보존을 확인합니다. 기존 Django·분류 워커·서빙 테스트도 같은 격리 환경에서 실행합니다.
