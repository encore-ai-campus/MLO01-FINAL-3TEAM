# 시연 티켓을 Django에서 조회하기

저장소 루트에서 로컬 클러스터와 `python3 src/infra/canary-demo/demo.py prepare-db`를 먼저 실행한다. 아래 명령은 현재 `src/backend/Dockerfile`을 빌드하고, `k3d-voc-canary-demo`의 `voc-ml`에 Django와 ClusterIP 서비스를 띄운다. 접속 대상은 시연 PostgreSQL `voc_canary_demo`다.

```sh
python3 src/infra/canary-demo/desk/desk.py setup
```

시연 계정은 `demoagent@canary.example.test`이며 `ticket.read` 권한을 가진다. 처음 실행할 때 만든 무작위 비밀번호는 이 클러스터의 Secret에 보관한다. 직접 로그인할 때는 아래 첫 줄로 비밀번호를 확인하고 두 번째 줄로 접속을 연다.

```sh
kubectl --kubeconfig .local/canary/kubeconfig --context k3d-voc-canary-demo -n voc-ml get secret canary-desk -o 'go-template={{index .data "staff-password" | base64decode}}{{"\n"}}'
python3 src/infra/canary-demo/desk/desk.py open
```

브라우저에서 [티켓 목록](http://127.0.0.1:18086/tickets/)으로 접속한 뒤 로그인한다. `Ctrl+C`로 로컬 포트 포워딩을 닫는다.

`python3 src/infra/canary-demo/demo.py verify`로 합성 트래픽을 흘린 뒤에는 다음 명령으로 데이터가 화면까지 도달했는지 검증한다. Pod 안에서 시연 상담사로 로그인해 목록을 열고, 감성과 카테고리가 모두 반영된 티켓의 상세 API와 HTML 표시값을 PostgreSQL 결과와 대조한 뒤 로그아웃한다. 고정 응답 모델의 기대값은 감성 `중립`, 카테고리 `배송`이다.

```sh
python3 src/infra/canary-demo/desk/desk.py verify
```
