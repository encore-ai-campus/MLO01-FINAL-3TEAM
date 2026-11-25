"""Seed an isolated PostgreSQL and verify worker routing through real Argo CD syncs."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
STATE = ROOT / ".local/canary"
KUBE = ["kubectl", "--kubeconfig", str(STATE / "kubeconfig"), "--context", "k3d-voc-canary-demo"]
COMPANY = "22222222-2222-4222-8222-222222222222"
CATEGORY = "44444444-4444-4444-8444-444444444444"
MIRROR = "http://canary-git.voc-ml.svc.cluster.local:8080/cgi-bin/git/charts.git"
MODEL_APP = "voc-classification"


def kube(*args, ns="voc-ml", data=None):
    return subprocess.run([*KUBE, "-n", ns, *args], input=data, text=True, check=True,
                          stdout=subprocess.PIPE, timeout=240).stdout.strip()


def sql(statement, *variables):
    return kube("exec", "-i", "deployment/canary-demo-db", "--", "psql", "-XqAt",
                "-v", "ON_ERROR_STOP=1", *variables, "-U", "voc_demo", "-d", "voc_canary_demo",
                data=statement)


def prepare():
    if not kube("get", "secret/voc-classification-db", "--ignore-not-found", "-o", "name"):
        password = secrets.token_hex(32)
        credentials = {"password": password, "DATABASE_URL":
                       f"postgresql://voc_demo:{password}@canary-demo-db:5432/voc_canary_demo"}
        # create, rather than apply, so credentials are not duplicated in an annotation.
        kube("create", "-f", "-", data=json.dumps({"apiVersion": "v1", "kind": "Secret",
             "metadata": {"name": "voc-classification-db"}, "stringData": credentials}))
    kube("apply", "-f", str(HERE / "database.yaml"))
    kube("rollout", "status", "deployment/canary-demo-db", "--timeout=180s")
    scripts = [ROOT / "src/collector" / name for name in ("schema.sql", "ticket_schema.sql", "settings_schema.sql", "followup_schema.sql")]
    scripts += [ROOT / "src/workers/classification/schema.sql", HERE / "fixtures.sql"]
    sql("BEGIN;\n" + "\n".join(path.read_text() for path in scripts) + "\nCOMMIT;")
    print("Demo PostgreSQL schema and synthetic tenant ready.")


def application():
    app = json.loads(kube("create", "--dry-run=client", "-f", str(HERE.parent / "argocd/voc-classification-local.yaml"),
                          "-o", "json", ns="argocd"))
    app["spec"]["source"].update(repoURL=MIRROR, targetRevision="feature/local-model-canary")
    kube("apply", "-f", "-", data=json.dumps(app), ns="argocd")
    # Explicitly selecting a fixture experiment restores chart defaults after an uncommitted KLUE experiment.
    kube("patch", "application", MODEL_APP, "--type=merge", "-p",
         json.dumps({"spec": {"source": {"helm": {"valuesObject": None}}}}), ns="argocd")


def publish(percent):
    script = HERE / "git-mirror/publish.py"
    result = subprocess.run([sys.executable, str(script), "--percent", str(percent)],
                            env={**os.environ, "KUBECONFIG": str(STATE / "kubeconfig")},
                            cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE)
    revision = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"Unexpected Git revision: {revision}")
    application()
    kube("annotate", "application", MODEL_APP, "argocd.argoproj.io/refresh=hard", "--overwrite", ns="argocd")
    return revision


def deployed(percent, revision):
    deadline = time.monotonic() + 210
    last = None
    while time.monotonic() < deadline:
        status = json.loads(kube("get", "application", MODEL_APP, "-o", "json", ns="argocd")).get("status", {})
        sync = status.get("sync", {})
        last = (sync.get("status"), status.get("health", {}).get("status"), status.get("conditions", []))
        dep = kube("get", "deployment", "voc-classification-worker", "--ignore-not-found", "-o", "json")
        if dep and sync.get("revision") == revision and last[:2] == ("Synced", "Healthy"):
            obj = json.loads(dep)
            env = obj["spec"]["template"]["spec"]["containers"][0]["env"]
            if any(v["name"] == "SENTIMENT_CANARY_PERCENT" and v["value"] == str(percent) for v in env):
                kube("rollout", "status", "deployment/voc-classification-worker", "--timeout=120s")
                return sync["revision"]
        time.sleep(1)
    raise RuntimeError(f"Argo CD did not converge at {percent}%: {last}")


def set_weight(percent):
    revision = publish(percent)
    deployed(percent, revision)
    print(f"Ready: sentiment canary {percent}% at Git {revision}", flush=True)
    return revision


def bucket(event):
    digest = hashlib.sha256(f"{COMPANY}:{event}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def enqueue(batch, buckets, body=None):
    ids = []
    for target in buckets:
        event = uuid4()
        while bucket(event) != target:
            event = uuid4()
        ids.append(str(event))
    variables = ["-v", f"batch={batch}", "-v", "ids={" + ",".join(ids) + "}"]
    if body is not None:
        variables += ["-v", f"body={body}"]
    sql((HERE / "enqueue.sql").read_text(), *variables)
    return ids


def outcomes(run_id):
    statement = """
      SELECT coalesce(json_agg(r),'[]') FROM (
        SELECT j.event_id, j.analysis_kind, j.state, j.applied, j.route, j.model_version, a.served_version, a.outcome,
               j.attempt_count,j.label, t.sentiment,t.category_uuid,t.sentiment_status,t.category_status,
               extract(epoch FROM j.finished_at-j.queued_at) AS seconds
        FROM voc.classification_job j JOIN voc.bronze_messages b USING(event_id)
        JOIN public.ticket t ON t.source_event_id=j.event_id
        LEFT JOIN voc.classification_attempt a ON (a.request_id,a.attempt_no)=(j.request_id,j.attempt_count)
        WHERE b.origin='synthetic-canary' AND split_part(b.source_message_id,'/',1)=:'run'
      ) r;
    """
    return json.loads(sql(statement, "-v", f"run={run_id}"))


def verify(deadline_seconds):
    run_id = str(uuid4())
    revisions = []
    expected = {}
    started = datetime.now(timezone.utc).isoformat()
    for index, percent in enumerate((0, 10, 50, 0)):
        previous = (0, 0, 10, 50)[index]
        revision = publish(percent)
        # New jobs arrive while controller and worker converge. Either active revision may decide them.
        if index:
            for event in enqueue(run_id, (0, 29, 84)):
                expected[event] = {previous, percent}
        deployed(percent, revision)
        revisions.append({"percent": percent, "revision": revision})
        fixed = (2, 9, 12, 23, 33, 42, 49, 50, 67, 77, 86, 96) if index else (1, 21, 43, 68, 95)
        batch = enqueue(run_id, fixed)
        expected.update({event: {percent} for event in batch})
        print(f"Synced {revision[:8]} at {percent}%, {len(expected)} synthetic tickets submitted", flush=True)
        # Finish the steady-state cohort before publishing the next threshold.
        until = time.monotonic() + 90
        while True:
            rows = outcomes(run_id)
            if any(row["state"] in ("failed", "skipped") for row in rows):
                raise RuntimeError("A synthetic job failed or was skipped")
            if sum(row["state"] == "succeeded" for row in rows) == len(expected)*2:
                break
            if time.monotonic() > until:
                raise RuntimeError("Timed out waiting for persisted predictions")
            time.sleep(.5)
    counts = Counter()
    keys = set()
    for row in rows:
        event, task = row["event_id"], row["analysis_kind"]
        key = (event, task)
        allowed = {"canary" if task == "sentiment" and bucket(event) < p else "stable" for p in expected[event]}
        version = "stub-v2" if row["route"] == "canary" else "stub-v1"
        if (key in keys or row["outcome"] != "succeeded" or row["route"] not in allowed or row["served_version"] != version
                or row["model_version"] != version or not row["applied"]
                or row["label"] != {"category": "delivery", "sentiment": "중립"}[task]
                or row["category_uuid"] != CATEGORY or row["sentiment"] != "중립"
                or row["sentiment_status"] != "succeeded" or row["category_status"] != "succeeded"
                or not 0 <= row["seconds"] <= deadline_seconds):
            raise RuntimeError(f"Unexpected result for {key}: {row}")
        counts[f"{task}/{version}"] += 1
        keys.add(key)
    if len(keys) != 100 or not counts["sentiment/stub-v2"]:
        raise RuntimeError("Expected 100 distinct results, including candidate traffic")
    result = {"run_id": run_id, "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
              "model": "fixture", "tickets": len(expected),
              "requests": len(keys), "successful": len(rows), "deadline_seconds": deadline_seconds,
              "max_seconds": max(row["seconds"] for row in rows),
              "attempts": sum(row["attempt_count"] for row in rows),
              "by_model": dict(counts), "revisions": revisions}
    (STATE / "last-run.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare-db")
    weight = sub.add_parser("set-weight")
    weight.add_argument("percent", type=int, choices=range(101), metavar="0..100")
    check = sub.add_parser("verify")
    check.add_argument("--deadline-seconds", type=float, default=30)
    args = parser.parse_args()
    if args.cmd == "prepare-db":
        prepare()
    elif args.cmd == "set-weight":
        set_weight(args.percent)
    else:
        if not 0 < args.deadline_seconds < float("inf"):
            parser.error("--deadline-seconds must be a positive, finite number")
        try:
            verify(args.deadline_seconds)
        except (Exception, KeyboardInterrupt):
            print("Verification interrupted. Restoring the demo Git weight to 0%.", file=sys.stderr)
            set_weight(0)
            raise
