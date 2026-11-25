"""Provision the demo Grafana dashboards and Prometheus scrape targets for the two KLUE releases."""
import json
from pathlib import Path
from shutil import copytree
import sys
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from demo import kube


def main():
    releases = json.loads((HERE/"values.json").read_text())["models"]
    with TemporaryDirectory() as tmp:
        config = Path(tmp)/"monitoring"
        copytree(HERE.parent/"observability", config)
        targets = [{"targets":[f"voc-classification-{m['version']}:8080"],
                    "labels":{"model_version":m["version"]}} for m in releases.values()]
        prom = {"global":{"scrape_interval":"5s"},"scrape_configs":[
            {"job_name":"voc-classifier","static_configs":targets}]}
        (config/"prometheus.yml").write_text(json.dumps(prom))
        file = config/"canary.json"
        dashboard = json.loads(file.read_text())
        dashboard.update(title="VoC 카나리 · KLUE-BERT", description="실제 로컬 KLUE 모델의 CPU 추론 지표",
                         tags=["voc","canary","klue"])
        for panel in dashboard["panels"]:
            for target in panel["targets"]:
                target["expr"] = (target["expr"]
                    .replace("voc_inference_requests_total", "voc_model_predictions_total")
                    .replace("voc_inference_duration_seconds_bucket", "voc_model_prediction_seconds_bucket")
                    .replace('outcome!="success"', 'outcome=~"error|unsupported|not_ready"'))
            if panel["id"] == 3:
                panel["description"] = "토크나이징과 모델 추론에 걸린 시간의 p95 추정치입니다."
        file.write_text(json.dumps(dashboard,ensure_ascii=False))
        kube("apply", "-k", str(config))
    for component in ("prometheus","grafana"):
        kube("rollout","status","deployment/canary-"+component,"--timeout=180s")

if __name__=="__main__":
    main()
