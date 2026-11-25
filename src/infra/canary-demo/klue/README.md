# 로컬 KLUE 모델과 상담사 배정 검증

기존 k3d 시연 클러스터, 내부 Git의 분류 Helm 차트와 PostgreSQL을 사용한다. 공용 Django의 실제 모델 API 코드는 `src/ml/serving/`에 있다. 가중치는 개발자가 로컬에 보관한다.

저장소 루트에서 다음 순서로 실행한다. 먼저 기존 시연 환경을 준비하고, 이후 실제 모델 프로필을 사용한다. 분류 워커를 다시 빌드한 경우 노드에 동일한 이미지를 import한다.

```sh
python3 src/infra/canary-demo/demo.py prepare-db
docker build --platform linux/arm64 -t voc-classification-worker:klue-poc-1 src/workers/classification
docker image save --platform linux/arm64 -o .local/canary/klue-worker.tar voc-classification-worker:klue-poc-1
k3d image import .local/canary/klue-worker.tar --cluster voc-canary-demo
python3 src/infra/canary-demo/desk/desk.py setup
python3 src/infra/canary-demo/klue/agents.py
python3 src/infra/canary-demo/klue/build.py /path/to/v1/model /path/to/v2/model
python3 src/infra/canary-demo/klue/route.py 0
python3 src/infra/canary-demo/klue/metrics.py
python3 src/infra/canary-demo/klue/verify.py
```

v1은 감성과 카테고리, v2는 감성만 서빙한다. 모델 파일의 SHA-256과 릴리스 설정은 `build.py` 및 `values.json`에 있다. 체크포인트를 외부 레지스트리에 전송하지 않는다. `route.py`는 로컬 Application의 `spec.source.helm.valuesObject`를 변경하고 실제 동기화와 워커 롤아웃을 기다린다. 검증은 0 → 10 → 50 → 0% 순서로 동작하며 Git 커밋을 생성하지 않는다. 기존 모의 모델 시연으로 전환할 때는 `up.sh`를 사용한다.

시연 회사의 도메인은 커머스로 매핑했다. 모델 코드와 업무 분류의 대응은 `domains.json`에서 관리한다. 세 상담사에게 열람·상담 권한과 같은 팀, 즉시 보충 정책을 적용한다. Django의 `assign_classified_tickets` 소비자는 완료된 v1 카테고리 결과와 전달 표시를 같은 DB 트랜잭션에서 처리한다. `VOC_POC_RANDOM_ASSIGNMENT=1`인 시연 환경에서만 여유가 있는 팀원 중 무작위로 뽑는다. 수동 지정, 기존 담당자, 재전달 방지는 Django의 기존 배정 서비스를 사용한다.

실행마다 새 합성 티켓 36건이 추가된다. `.local/canary/klue-verification.json`에서 API 응답·모델별 건수·티켓별 담당자를 확인할 수 있다. `desk/desk.py open`으로 연 시연 웹에서 각 상담사가 로그인할 수 있다. 계정 정보는 `.local/canary/klue-agent-logins.json`에 로컬 전용으로 저장한다.

## 공용 Git으로 실채널 프로필 배포

[KLUE Application](../../argocd/voc-classification-klue.yaml)은 공용 `develop`의 [KLUE values](../../helm/voc-classification/values-klue.json)를 추적한다. 감성은 Stable 90%·Canary 10%, 카테고리는 Stable이 처리한다. 같은 이름으로 실행 중인 로컬 미러 Application이 있다면 기존 모델 이미지 태그, 업무 도메인 매핑, 회사 UUID 및 Secret 참조를 먼저 대조한다.

팀의 모델 이미지를 노드에 준비하고 Stable 이미지에 해당 회사 도메인의 분류 코드 매핑을 넣는다. 운영자가 분류 스키마와 카테고리 매핑을 먼저 적용하고 `voc-live-ml`의 `live-classifier-db` Secret에 런타임 계정의 `DATABASE_URL`을 설정한다. 이 프로필은 초기에는 모델만 실행한다. values에서 `worker.companyUUID`와 이미지 태그를 지정하고 `worker.enabled`를 켜면 워커는 `verify-schema`로 DB를 확인한 후 처리한다. 배분율은 같은 파일의 `routing.sentiment.canaryPercent` 변경을 병합해 조정한다.
