# 로컬 카나리 모델 대시보드

Prometheus가 5초마다 `voc-classification-stub-v1:8080`과 `voc-classification-stub-v2:8080`의 `/metrics`를 읽는다. Grafana는 실제 응답 모델 버전과 분석 항목별로 처리량, 선택한 기간의 오류 건수, HTTP 처리시간 p95, 누적 요청 수를 표시한다. 현재 시험용 차트는 버전별 모델 Pod 1개를 띄운다. 실제 모델 버전별 Service 이름을 바꿀 때 `prometheus.yml`의 대상도 함께 갱신한다. 모델 복제본을 늘릴 때는 Pod별로 수집하도록 대상을 변경한다.

## 배포와 접속

저장소 루트에서 격리된 시연 클러스터와 `voc-ml` 네임스페이스 준비 후 실행한다.

```bash
export KUBECONFIG="$PWD/.local/canary/kubeconfig"
kubectl --context k3d-voc-canary-demo apply -k src/infra/canary-demo/observability
kubectl --context k3d-voc-canary-demo -n voc-ml rollout status deployment/canary-grafana
kubectl --context k3d-voc-canary-demo -n voc-ml port-forward --address 127.0.0.1 svc/canary-grafana 18300:3000
```

[시험 모델 대시보드 열기](http://127.0.0.1:18300/d/voc-canary-demo). ClusterIP 서비스와 익명 Viewer 화면이며, 위 전달 포트는 Mac 루프백에만 열린다. 대시보드 데이터는 현재 Prometheus Pod의 임시 볼륨에 최대 6시간 보관한다. 모델이 준비되고 요청을 받은 뒤 여러 번 수집하면 처리율과 p95를 확인할 수 있다.

배분율 시연 후 위 `KUBECONFIG`를 설정한 터미널에서 `python3 src/infra/canary-demo/observability/verify.py`를 실행하면 대시보드·데이터 소스·모델 수집 상태와 실제 버전별 호출 건수를 확인할 수 있다.

## 결과 해석

- 모델당 실제 호출 수에는 HTTP 재시도도 포함된다. 시연 100건의 성공 판정에는 분석 작업의 고유 ID, 실제 버전, 완료 시각, 예측 결과를 사용한다.
- 배포 구간을 오른쪽 위 시간 선택기로 지정하면 오류 패널이 버전·분석 항목별 오류 및 입력 거절 건수를 보여 준다. Prometheus의 `increase`가 5초 간격 표본 사이의 증가량을 구간 경계까지 추정하므로 소수가 나올 수 있다. 관측된 버전·항목에서 오류 시계열이 없으면 0을 표시한다. 요청별 재시도와 최종 처리 상태는 DB 작업 이력에서 확인한다.
- p95는 모델 HTTP 처리 구간의 히스토그램 추정치다. 큐 대기와 업무 DB 반영까지의 지연은 워커 측 기록에서 확인한다.
- 기본 Prometheus 이미지와 Grafana 이미지는 `prom/prometheus:v3.5.0`, `grafana/grafana:12.2.0`이다. 클러스터가 레지스트리에 접근하지 못하는 Mac에서는 로컬 arm64 이미지를 `docker image save --platform linux/arm64`로 내보내고 `k3d image import`로 가져온다.
