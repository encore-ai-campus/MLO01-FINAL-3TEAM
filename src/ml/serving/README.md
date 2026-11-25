# VoC 모델 서버

M5의 Kubernetes에서 같은 CPU 이미지로 Stable과 Canary를 실행합니다. 두 배포의 MODEL_VERSION과 읽기 전용 모델 경로를 각각 지정합니다. 배포 비율과 티켓 결과 저장은 분류 워커에서 담당합니다.

## 이미지와 실행

저장소 루트에서 실행합니다. MODEL_DIR은 팀에서 전달한 학습 완료 체크포인트의 model 디렉터리, DOMAIN_MAP_FILE은 합의한 실제 UUID와 코드가 들어 있는 JSON 파일의 절대 경로입니다. 배포 전 두 파일을 컨테이너 사용자에게 읽기 가능하게 마운트합니다.

```sh
docker buildx build --platform linux/arm64 --load -t voc-model:local src/ml/serving

docker run --rm --platform linux/arm64 -p 8080:8080 \
  -e MODEL_VERSION="$STABLE_MODEL_VERSION" \
  -e MODEL_PATH=/models/stable \
  -e MODEL_DOMAIN_MAP=/config/domains.json \
  -v "$MODEL_DIR:/models/stable:ro" -v "$DOMAIN_MAP_FILE:/config/domains.json:ro" \
  voc-model:local
```

Canary는 별도 컨테이너에서 $CANARY_MODEL_VERSION과 해당 MODEL_PATH를 지정하고 MODEL_ANALYSIS_KINDS=sentiment로 실행합니다. Stable 기본값은 sentiment,category입니다. MODEL_BACKEND의 기본값은 voc_models.tickets:load이며 로컬 체크포인트에서 config.json, model.safetensors 및 토크나이저를 읽습니다. 대체 백엔드는 load()가 predict(analysis_kind, domain_id, model_text) 함수를 반환하는 같은 계약을 따릅니다.

## 도메인과 업무 코드

MODEL_DOMAIN_MAP의 최상위 키는 팀 DB의 도메인 UUID입니다. 각 값의 task는 commerce, finance, food_delivery 중 체크포인트에서 사용하는 분류층 키입니다. labels에는 해당 분류층의 학습 라벨을 모두 나열하고 각각을 업무 카테고리 코드 또는 null로 지정합니다. 같은 업무 코드로 여러 학습 라벨을 매핑할 수 있습니다. 선택 필드 min_score의 기본값은 0.0이며 팀 검수 후 조정합니다. 등록되지 않은 도메인의 카테고리, 업무 코드가 null인 라벨, 기준 미만 점수는 보류합니다. 감성 전용 배포에는 도메인 매핑 파일이 필요 없습니다.

입력 도메인은 예시 값으로 교체해야 합니다. 아래는 체크포인트의 커머스 라벨과 매핑 형식을 보여주는 템플릿입니다.

```json
{
  "<팀에서 받은 커머스 UUID>": {
    "task": "commerce",
    "labels": {
      "no_issue": "<합의한 일반 문의 코드>",
      "item_defective": "<합의한 상품 불량 코드>",
      "delivery_delay": "<합의한 배송 지연 코드>",
      "wrong_delivery": "<합의한 오배송 코드>"
    }
  }
}
```

## API와 검증

- POST /v1/predict에는 request_id 및 domain_id UUID 문자열, analysis_kind, model_text를 보냅니다. 요청당 라벨 하나와 선택 점수를 반환하며 request_id와 analysis_kind를 그대로 돌려주고 model_version은 MODEL_VERSION 값을 반환합니다. 감성 체크포인트는 긍정·중립·부정 세 라벨을 지원합니다. 카테고리 보류는 HTTP 200과 label: null입니다.
- GET /livez는 프로세스 상태, GET /readyz는 실제 로딩 완료 상태를 반환합니다. 로딩 중 또는 실패 시 준비 확인 및 예측은 503입니다. 입력 형식과 비활성 작업은 422, 백엔드 실행 오류는 500입니다. GET /metrics는 요청 종류별 처리 결과와 추론 시간 히스토그램, 모델 로딩 시간과 준비 상태를 내보냅니다.
- 실제 Stable·Canary 체크포인트를 제공받으면 같은 문의와 기대 라벨로 버전 및 응답을 먼저 확인합니다. M5에서 메모리·로딩 시간과 배치 1 워밍업 후 추론 p95를 측정합니다. 모델 추론 목표 3초와 워커 HTTP 타임아웃 10초를 따로 확인합니다.
- HTTP 계약 테스트는 이 디렉터리에서 httpx==0.28.1과 requirements.txt를 설치한 뒤 python -m unittest -v test_server를 실행합니다. 테스트는 모델 로더를 대체해 HTTP 동작과 준비 상태를 검증합니다.

