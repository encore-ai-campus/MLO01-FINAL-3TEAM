# 티켓 분류 API v1

워커는 `POST /v1/predict`로 분석 항목 하나를 요청한다. 호출 시 `Content-Type: application/json`을 사용한다. 워커는 작업에 기록된 본문·전처리 버전으로 `model_text`를 조회하고, 모델 서버는 전달받은 본문으로 추론한다. 같은 작업의 재시도에는 같은 `request_id`와 모델 버전을 사용한다. `GET /openapi.json`에서 현재 서버의 요청·응답 스키마를 읽을 수 있다.

```json
{
  "request_id": "62a02c0c-9f13-4610-a99e-7a665ad5b02f",
  "analysis_kind": "category",
  "domain_id": "f7adfece-886c-4e9d-8aa6-427a875bbf69",
  "model_text": "배송에 관해 문의합니다."
}
```

`request_id`는 작업 UUID, `domain_id`는 업무 도메인의 UUID다. `model_text`는 공백만 있는 경우를 제외하고 정확한 본문 값과 줄바꿈을 유지한다. `analysis_kind`는 `sentiment` 또는 `category`다.

```json
{
  "request_id": "62a02c0c-9f13-4610-a99e-7a665ad5b02f",
  "analysis_kind": "category",
  "model_version": "stub-v1",
  "label": "delivery"
}
```

응답 HTTP 200은 요청 ID·분석 항목을 그대로 반환하며 실제 처리 버전을 명시한다. 감성 레이블은 `긍정`, `중립`, `부정`, `매우 부정` 중 하나다. 카테고리는 도메인의 등록 코드 또는 판단 보류를 뜻하는 JSON `null`이다. 워커는 코드를 해당 도메인의 카테고리 UUID로 변환한다. 선택 필드 `score`는 모델이 제공하는 0~1 범위 점수다. 서비스는 검증 오류를 HTTP 422로 반환한다. 모델 로딩·추론 장애는 5xx로 처리한다.

## 실제 모델 배포

실제 API 구현과 정확한 응답 형식은 [모델 서버](../ml/serving/README.md)에서 관리한다. 실제 서버는 `request_id`와 `domain_id`를 UUID 문자열로 검증한다. `MODEL_VERSION`은 체크포인트 릴리스명이며, `MODEL_PATH`에서 가중치를 읽는다. 카테고리를 지원하는 배포에는 도메인 UUID와 학습 라벨의 업무 코드 매핑이 필요하다. 두 KLUE 체크포인트의 감성 출력은 긍정·중립·부정이다.

실제 서버의 준비 상태는 `GET /readyz`의 `status`로, 실제 모델 버전은 추론 응답의 `model_version`으로 확인한다. 분류 워커가 보낸 문의는 모델 서버에서 DB 쓰기를 발생시키지 않는다. HTTP 422는 입력 형식 또는 배포에서 지원하지 않는 작업, 503은 모델 준비 실패, 500은 추론 실패를 나타낸다.

[시연용 고정 응답 서버](../infra/canary-demo/fixture/server.py)는 별도 이미지로 구동한다. 실제 모델의 시연과 증빙 생성은 [KLUE 실행 절차](../infra/canary-demo/klue/README.md)를 따른다.
