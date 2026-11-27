# develop 병합 후 Mac 시연 서버 배포

GitHub의 표준 Ubuntu 러너가 PR과 병합된 develop 커밋을 각각 테스트한다. 로그인한 Mac 사용자의 launchd 에이전트는 5분마다 GitHub에 HTTPS로 접속한다. 현재 develop SHA, 같은 SHA의 성공한 push 워크플로, 해당 SHA를 develop에 병합한 PR을 대조한다. Mac과 데이터베이스 접속 정보는 GitHub 작업에 전달하지 않는다.

## 최초 설치

기존 Docker Desktop 시연 환경에서 실행한다. Django Compose 프로필, 로컬 모델 가중치, 수집기 계정, MLflow·Argo Workflows 연결 및 고정 평가셋이 준비돼 있어야 한다.

```sh
python3 src/infra/local-release/install.py /Users/2eelogan/project/mlo-01-final-team3/.local/live-ticket-check
```

설치 시 실행 중인 Django 이미지의 Git SHA를 최초 비교 기준으로 저장한다. Mac에 로그인한 상태에서 Docker Desktop이 실행되고 LAN 주소가 192.168.0.16이어야 한다. 절전 중에 병합된 변경은 다시 깨어난 뒤 최신 성공한 develop으로 따라간다.

## 배포 범위

- Django 웹과 배정 워커는 검증한 커밋으로 빌드한 이미지를 사용한다. 기존 Compose의 MLflow 주소, Argo 서비스 계정 토큰 마운트, DB·업무 채널 설정을 보존한다.
- 재학습 예약 실행기는 같은 웹 이미지와 연결 설정을 사용한다. 웹과 예약 실행기에는 평가셋과 시작 모델 버킷에 대한 읽기 권한이 필요하다. 기존 웹 환경에서 제공하거나 `web.json` 옆 `training-read.env`에 MinIO 주소(`MLFLOW_S3_ENDPOINT_URL`), 읽기 전용 자격 증명과 브라우저에서 열 수 있는 `VOC_GRAFANA_URL`을 설정한다. 로컬 자격 증명 파일은 해당 사용자만 읽을 수 있도록 보관한다. 학습 도메인은 Argo의 `voc-training-domains` 설정을 읽기 전용으로 연결한다. 새 회사의 자동 예약 기본값은 꺼짐이다.
- 실모델 분류 앱은 동일 커밋의 Helm 차트를 Argo CD가 동기화한다. 기존 회사, 모델 가중치와 DB Secret을 사용한다. 추론 또는 워커 소스 변경 시 해당 이미지를 빌드해 k3d 노드에 넣는다. 라우팅 정책은 `values-klue.json`에서 관리한다.
- 학습 소스나 WorkflowTemplate이 변경되면 CPU 학습 이미지를 교체하고 템플릿을 적용한다. Feast의 로컬 SQL 작업은 `voc_training_scratch` 스키마를 쓴다. 진행 중인 학습이 있으면 다음 확인 주기에 배포한다. 병합 자체가 학습이나 후보 모델 승격을 시작하지 않는다.
- 수집·Flink·모니터링 소스 변경 시 해당 Compose 서비스를 갱신한다. 기존 데이터 볼륨과 자격 증명을 쓴다. 호환되는 버전의 DB 변경은 트랜잭션으로 반영한다. 데이터 삭제나 비호환 마이그레이션은 별도 전환 절차가 필요하다.
- Django 설정, LAN 로그인 응답, 티켓 DB, 시작 모델·고정 평가셋 조회, 인증된 Argo 목록, 수집기, 실행 중인 Flink 잡, Prometheus, Grafana, Argo CD 동기화 및 학습 서비스 준비 상태를 검사한다. 실패한 배포는 성공 SHA로 기록하지 않는다. 앱과 웹은 직전 설정으로 복구하고 다음 주기에 다시 확인한다.

## 상태 확인

```sh
launchctl print "gui/$(id -u)/local.voc.develop-release"
tail -n 60 ~/.local/share/voc-develop-cd/release.log
cat ~/.local/share/voc-develop-cd/deployed
curl -fsS -o /dev/null -w '%{http_code}\n' http://192.168.0.16:8000/login/
```

Mac에서 즉시 확인하려면 `launchctl kickstart "gui/$(id -u)/local.voc.develop-release"`를 실행한다. 기존 작업이 끝나기 전에 재시작하는 `-k` 옵션은 사용하지 않는다. 에이전트 설치 코드를 갱신한 경우 최초 설치 명령을 다시 실행한다. 브랜치 직접 push와 PR 테스트만 성공한 커밋은 자동 배포 대상에서 제외한다. 이 경로가 신뢰하는 코드는 develop에 병합된 코드이므로 저장소의 병합 권한을 관리한다.
