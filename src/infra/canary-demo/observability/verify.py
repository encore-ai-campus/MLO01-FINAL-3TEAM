"""Check the local dashboard and its model counters after a canary demo."""
import json
import subprocess
from urllib.parse import urlencode


def grafana_get(path):
    proxy = "/api/v1/namespaces/voc-ml/services/http:canary-grafana:http/proxy"
    return json.loads(subprocess.check_output([
        "kubectl", "--context", "k3d-voc-canary-demo", "get", "--raw", proxy + path,
    ], text=True))


def query(expression):
    response = grafana_get("/api/datasources/proxy/uid/voc-prometheus/api/v1/query?"
                           + urlencode({"query": expression}))
    if response["status"] != "success":
        raise RuntimeError("Prometheus query failed")
    return response["data"]["result"]


def main():
    health = grafana_get("/api/health")
    dashboard = grafana_get("/api/dashboards/uid/voc-canary-demo")["dashboard"]
    source = grafana_get("/api/datasources/uid/voc-prometheus/health")
    if health["database"] != "ok" or source["status"] != "OK" or len(dashboard["panels"]) != 4:
        raise SystemExit("Dashboard or datasource is not ready")

    targets = {row["metric"]["instance"]: row["value"][1]
               for row in query('up{job="voc-classifier"}')}
    if targets != {"voc-classification-stub-v1:8080": "1", "voc-classification-stub-v2:8080": "1"}:
        raise SystemExit(f"Both model scrape targets must be up: {targets}")

    errors = next(panel for panel in dashboard["panels"] if panel["id"] == 2)
    target = errors["targets"][0]
    if errors["type"] != "stat" or not target.get("instant") or "[$__range]" not in target["expr"]:
        raise SystemExit("The selected-period error panel is not provisioned")
    selected_errors = query(target["expr"].replace("$__range", "15m"))
    if not selected_errors:
        raise SystemExit("Waiting for enough samples for the 15-minute error panel")

    rows = query('sum by (model_version, analysis_kind) '
                 '(voc_inference_requests_total{job="voc-classifier",outcome="success"})')
    counts = {(row["metric"]["model_version"], row["metric"]["analysis_kind"]): float(row["value"][1])
              for row in rows}
    expected = [("stub-v1", "category"), ("stub-v1", "sentiment"), ("stub-v2", "sentiment")]
    if any(counts.get(key, 0) < 1 for key in expected):
        raise SystemExit(f"Run the routing demo, then wait for the next scrape: {counts}")
    print(f'{dashboard["title"]} · Grafana {health["version"]} · datasource {source["status"]}')
    for row in selected_errors:
        metric = row["metric"]
        print(f'15m errors: {metric["model_version"]} {metric["analysis_kind"]} {row["value"][1]}')
    for (version, kind), value in sorted(counts.items()):
        print(f"{version} {kind}: {value:g}")


if __name__ == "__main__":
    main()
