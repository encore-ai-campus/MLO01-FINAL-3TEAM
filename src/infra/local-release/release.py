"""Apply the verified checkout to the existing VoC demo on Docker Desktop."""
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[3]
LIVE = Path(os.environ.get("VOC_LIVE", ".")).resolve()
COLLECTOR = LIVE.parent.parent
STATE = Path(os.environ.get("VOC_RELEASE_HOME", Path.home() / ".local/share/voc-develop-cd"))
NODE = "k3d-voc-canary-demo-server-0"
KUBE = ["docker", "exec", "-i", NODE, "kubectl"]
TRAIN = "voc-training"
APP = "voc-live-classification"
GIT_URL = "https://github.com/encore-ai-campus/MLO01-FINAL-3TEAM.git"
WEB = "voc-live-ticket-web"
CHART = "src/infra/helm/voc-classification"
PROFILE = LIVE / "web.json"
HTTP = build_opener(ProxyHandler({}))


def run(*args, data=None):
    subprocess.run(args, input=data, text=True, cwd=ROOT, check=True)


def read(*args):
    return subprocess.check_output(args, text=True, cwd=ROOT).strip()


def kube(ns, *args):
    return json.loads(read(*KUBE, "-n", ns, "get", *args, "-o", "json"))


def atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        try:
            f.write(data)
            f.flush()
            os.replace(f.name, path)
        finally:
            Path(f.name).unlink(missing_ok=True)


def put(path, obj):
    atomic(path, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode())


def import_image(tag):
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["docker", "save", tag], stdout=archive, check=True)
        archive.seek(0)
        subprocess.run(["docker", "exec", "-i", NODE, "ctr", "-n", "k8s.io", "images", "import",
                        "--platform", "linux/arm64", "-"], stdin=archive, stdout=subprocess.DEVNULL, check=True)


def build(tag, dockerfile, context, *options):
    run("docker", "build", "-f", dockerfile, "-t", tag, *options, context)


def compose(profile, *services):
    run("docker", "compose", "-f", profile, "up", "-d", "--no-deps", "--no-build",
        "--wait", "--wait-timeout", "150", *services)


def migrate(files):
    if files:
        sql = "\n".join((ROOT / path).read_text() for path in files)
        run("docker", "exec", "-i", "-e", "PGOPTIONS=-c lock_timeout=5s", "encore-voc-collector-postgres-1",
            "psql", "-qX", "-1", "-v", "ON_ERROR_STOP=1", "-U", "voc", "-d", "voc", "-f", "-", data=sql)


def app_source(source):
    run(*KUBE, "-n", "argocd", "patch", "application", APP, "--type=json", "-p",
        json.dumps([dict(op="replace", path="/spec/source", value=source)]))
    run(*KUBE, "-n", "argocd", "annotate", "application", APP, "argocd.argoproj.io/refresh=hard", "--overwrite")


def wait_app(sha):
    limit = time.monotonic() + 360
    while time.monotonic() < limit:
        s = kube("argocd", "application", APP).get("status", {})
        sync = s.get("sync", {})
        if sync.get("revision") == sha and sync.get("status") == "Synced" and s.get("health", {}).get("status") == "Healthy":
            return
        time.sleep(4)
    raise RuntimeError(f"Argo CD did not synchronize {sha}")


def release_models(previous, sha, changed, undo):
    old = kube("argocd", "application", APP)["spec"]["source"]
    source = copy.deepcopy(old)
    first = old["repoURL"] != GIT_URL
    cfg = source["helm"]["valuesObject"]
    values = json.loads((ROOT / CHART / "values-klue.json").read_text())
    previous_values = json.loads(read("git", "show", f"{previous}:{CHART}/values-klue.json"))
    # Docker-only image tags, tenant and DB secret belong to this installation.
    # Values explicitly changed in Git take precedence over its previous runtime overrides.
    for key in ("routing", "resources", "imagePullPolicy"):
        if previous_values.get(key) != values.get(key):
            cfg.pop(key, None)
    for lane in ("stable", "canary"):
        for key, val in values["models"][lane].items():
            if previous_values["models"][lane].get(key) != val:
                cfg["models"][lane].pop(key, None)
    if first or changed("src/workers/classification"):
        tag = "voc-classification-worker:develop-" + sha[:12]
        build(tag, "src/workers/classification/Dockerfile", "src/workers/classification")
        import_image(tag)
        migrate(["src/workers/classification/schema.sql"])
        cfg["worker"]["image"] = {"repository": tag.split(":")[0], "tag": tag.split(":")[1]}
    cfg["worker"]["schemaMode"] = "verify-schema"
    if changed("src/ml/serving", "src/infra/canary-demo/klue/Dockerfile"):
        server = "voc-klue-api:develop-" + sha[:12]
        build(server, "src/ml/serving/Dockerfile", "src/ml/serving")
        for lane, version, kinds in (("stable","v1","sentiment,category"), ("canary","v2","sentiment")):
            weights = Path(os.getenv("VOC_WEIGHTS_" + lane.upper(), Path.home() / f"Downloads/voc-klue-bert-multitask-{version}/model"))
            tag = f"voc-klue-local:develop-{sha[:12]}-{version}"
            build(tag, "src/infra/canary-demo/klue/Dockerfile", weights,
                  "--build-context", f"mapping={LIVE / 'klue/images'}", "--build-arg", f"SERVER_IMAGE={server}",
                  "--build-arg", f"ANALYSIS_KINDS={kinds}")
            import_image(tag)
            cfg["models"][lane]["image"] = {"repository": tag.split(":")[0], "tag": tag.split(":")[1]}
    source.update(repoURL=GIT_URL, targetRevision=sha, path=CHART)
    source["helm"]["valueFiles"] = ["values-klue.json"]
    undo.callback(app_source, old)
    app_source(source)
    wait_app(sha)


def release_training(sha, changed, undo):
    if not changed("src/ml/training", "src/ml/serving/voc_models", "src/workers/classification/online_features.py",
                   "src/infra/argo-workflows"):
        return
    previous = kube(TRAIN, "workflowtemplate", TRAIN)
    image = f"voc-training:develop-{sha[:12]}"
    build(image, "src/ml/training/Dockerfile", ".")
    import_image(image)
    manifest = (ROOT / "src/infra/argo-workflows/voc-training.yaml").read_text()
    documents = []
    for document in manifest.split("\n---\n"):
        obj = json.loads(subprocess.check_output([*KUBE, "-n", TRAIN, "create", "--dry-run=client",
                         "-o", "json", "-f", "-"], input=document, text=True))
        documents.append(obj)
    rendered = {"apiVersion": "v1", "kind": "List", "items": documents}
    template = next(i for i in documents if i["kind"] == "WorkflowTemplate")
    parameter = next(p for p in template["spec"]["arguments"]["parameters"] if p["name"] == "image")
    parameter["value"] = image
    for step in template["spec"]["templates"]:
        container = step.get("script") or step.get("container")
        if container:
            container.setdefault("env", []).append({"name": "FEAST_OFFLINE_SCHEMA", "value": "voc_training_scratch"})
    undo.callback(run, *KUBE, "-n", TRAIN, "patch", "workflowtemplate", TRAIN, "--type=json", "-p",
                  json.dumps([dict(op="replace", path="/spec", value=previous["spec"])]))
    run(*KUBE, "-n", TRAIN, "apply", "-f", "-", data=json.dumps(rendered))
    if changed("src/ml/training/requirements.txt", "src/ml/training/Dockerfile"):
        service = kube(TRAIN, "deployment", "mlflow")
        container = next(c for c in service["spec"]["template"]["spec"]["containers"] if c["name"] == "mlflow")
        update = [*KUBE, "-n", TRAIN, "set", "image", "deployment/mlflow"]
        undo.callback(run, *update, "mlflow=" + container["image"])
        run(*update, "mlflow=" + image)
        run(*KUBE, "-n", TRAIN, "rollout", "status", "deployment/mlflow", "--timeout=180s")


def pipeline(sha, changed, directory, undo):
    collector = changed("src/collector", "src/infra/channel_probe", "compose.yaml")
    monitoring = changed("src/monitoring", "compose.yaml")
    if not (collector or monitoring):
        return
    files = ["-f", ROOT / "compose.yaml"]
    if monitoring:
        files += ["-f", ROOT / "src/monitoring/compose.monitoring.yaml", "-f", COLLECTOR / ".local/monitoring/compose.lan.json"]
    profile = json.loads(read("docker", "compose", "--project-directory", COLLECTOR,
                              "--env-file", COLLECTOR / ".env", *files, "config", "--format", "json"))
    services = profile["services"]
    deploy = []
    if collector:
        schema_files = [f"src/collector/{s}.sql" for s in ("schema","ticket_schema","training_schema",
                        "channel_intake_schema","settings_schema","followup_schema")]
        migrate([f for f in schema_files if changed(f)])
        tag = f"voc-collector:develop-{sha[:12]}"
        build(tag, "src/collector/Dockerfile", "src")
        for name in ("discord", "gmail", "publish", "db-worker"):
            services[name].update(image=tag)
            deploy.append(name)
        if changed("src/collector/flink", "compose.yaml"):
            tag = f"voc-flink:develop-{sha[:12]}"
            build(tag, "src/collector/flink/Dockerfile", "src/collector/flink")
            for name in ("jobmanager","taskmanager"):
                services[name].update(image=tag)
                deploy.append(name)
    if monitoring:
        tag = f"voc-metrics:develop-{sha[:12]}"
        build(tag, "src/monitoring/Dockerfile", "src/monitoring")
        services["metrics"].update(image=tag)
        # Bind the verified monitoring sources for the entire life of this release.
        import shutil
        assets = directory / "monitoring"
        shutil.copytree(ROOT / "src/monitoring", assets, dirs_exist_ok=True)
        for svc in ("monitor-db","grafana","prometheus"):
            for vol in services[svc].get("volumes", []):
                target = vol["target"]
                if target.startswith("/etc/grafana/") or target in ("/etc/prometheus/prometheus.yml","/monitoring/schema.sql"):
                    relative = {"grafana": target.removeprefix("/etc/"), "monitor-db": "schema.sql",
                                "prometheus": "prometheus.yml"}[svc]
                    vol["source"] = str(assets / relative)
        deploy += ["metrics","prometheus","grafana","model-metrics-receiver"]
    # Reuse the original Compose configurations for rollback, including their bind mounts.
    for name in deploy:
        services[name].pop("build", None)
        container = f"encore-voc-collector-{name}-1"
        original = read("docker", "inspect", "-f", '{{index .Config.Labels "com.docker.compose.project.config_files"}}', container)
        image_id = read("docker", "inspect", "-f", "{{.Image}}", container)
        tag = f"voc-release-prior/{name}:{sha[:12]}"
        run("docker","tag",image_id,tag)
        override = directory / (name + "-rollback.json")
        put(override, {"services": {name: {"image": tag}}})
        args = ["docker","compose","--env-file",COLLECTOR / ".env"]
        for f in original.split(","):
            args += ["-f", f]
        undo.callback(run,*args,"-f",override,"up","-d","--no-deps","--no-build",name)
    file = directory / "pipeline.json"
    put(file, profile)
    if monitoring and changed("src/monitoring/schema.sql"):
        run("docker","compose","-f",file,"run","--rm","--no-deps","monitor-db")
    compose(file, *deploy)


def verify():
    run("docker","exec",WEB,"python","manage.py","check")
    # Call the same registry, tenant dataset query and authenticated Argo API as the web view.
    probe = """from django.db import connection
from apps.preferences.training import available_models,recent_workflows
from apps.preferences.presentation.training import evaluation_sets
with connection.cursor() as c:
    c.execute("SELECT DISTINCT company_uuid FROM public.staff_user WHERE role='super_admin'")
    companies=c.fetchall()
    c.execute("SELECT count(*) FROM public.ticket")
    print("tickets",c.fetchone()[0])
assert companies, 'No super admin'
for (company,) in companies:
    assert available_models(company) and evaluation_sets(company), 'Missing training selection'
    recent_workflows(company)
print('MLflow, evaluation set and Argo API OK')"""
    run("docker","exec",WEB,"python","manage.py","shell","-v","0","-c",probe)
    # Check both live routes directly, using synthetic text without creating a ticket.
    domain = read("docker", "exec", "encore-voc-collector-postgres-1", "psql", "-XAt", "-U", "voc", "-d", "voc",
                  "-c", "SELECT c.domain_uuid FROM public.company c JOIN public.staff_user s USING(company_uuid) "
                        "WHERE s.role='super_admin' ORDER BY c.company_uuid LIMIT 1")
    probe_model = """import json,os,sys,uuid
from urllib.request import Request,urlopen
for lane,kind in [('STABLE','sentiment'),('STABLE','category'),('CANARY','sentiment')]:
    ref=str(uuid.uuid4())
    body=dict(request_id=ref,domain_id=sys.argv[1],analysis_kind=kind,model_text='배송 예정일을 알려 주세요.')
    if os.environ.get(lane+'_CUSTOMER_FEATURES')=='1':
        body.update(title=None, customer_features=dict(unresolved_count=0,avg_first_reply_7d_seconds=None))
    req=Request(os.environ[lane+'_URL']+'/v1/predict',json.dumps(body).encode(),{'Content-Type':'application/json'})
    with urlopen(req,timeout=15) as response:
        data=json.load(response)
    assert (data['request_id'],data['analysis_kind'],data['model_version'])==(ref,kind,os.environ[lane+'_MODEL_VERSION'])
    print(lane, kind, data['model_version'])
"""
    run(*KUBE, "-n", "voc-live-ml", "exec", "deployment/" + APP + "-worker", "-c", "worker", "--",
        "python", "-c", probe_model, domain)
    for name in ("discord","gmail","postgres","kafka","publish","db-worker","jobmanager","taskmanager","metrics"):
        state = json.loads(read("docker","inspect","-f","{{json .State}}",f"encore-voc-collector-{name}-1"))
        if not state["Running"] or state.get("Health",{}).get("Status","healthy") != "healthy":
            raise RuntimeError(name + " is not ready")
    for url in ("http://192.168.0.16:8000/login/","http://127.0.0.1:13000/api/health",
                "http://127.0.0.1:19090/-/ready"):
        with HTTP.open(url, timeout=8) as resp:
            if resp.status != 200:
                raise RuntimeError(f"{url}: {resp.status}")
    with HTTP.open("http://127.0.0.1:18081/jobs/overview", timeout=8) as resp:
        if not any(job["state"] == "RUNNING" for job in json.load(resp)["jobs"]):
            raise RuntimeError("Silver Flink job is not running")
    for name in ("mlflow","minio","redis"):
        run(*KUBE,"-n",TRAIN,"rollout","status","deployment/"+name,"--timeout=30s")


def main(previous, sha):
    if read("git","rev-parse","HEAD") != sha:
        raise RuntimeError("Checkout differs from verified SHA")
    changed_paths = read("git","diff","--name-only",previous,sha).splitlines()
    def changed(*paths):
        return any(f == p or f.startswith(p + "/") for f in changed_paths for p in paths)
    if any(w.get("status", {}).get("phase") not in ("Succeeded","Failed","Error")
           for w in kube(TRAIN, "workflows").get("items",[])):
        raise RuntimeError("Training in progress; retry next poll")
    directory = STATE / "releases" / sha
    directory.mkdir(parents=True, exist_ok=True)
    tag = f"voc-live-ticket-desk:develop-{sha[:12]}"
    build(tag, "src/backend/Dockerfile", "src", "--label", f"org.opencontainers.image.revision={sha}")
    with contextlib.ExitStack() as undo:
        pipeline(sha, changed, directory, undo)
        release_models(previous, sha, changed, undo)
        release_training(sha, changed, undo)
        profile = json.loads(PROFILE.read_bytes())
        # Preserve session secrets, MLflow URLs, Argo token mounts, networks and assignment configuration.
        for service in ("web","assign"):
            profile["services"][service]["image"] = tag
        undo.callback(compose, PROFILE, "web","assign")
        undo.callback(atomic, PROFILE, PROFILE.read_bytes())
        put(PROFILE, profile)
        compose(PROFILE, "web","assign")
        verify()
        if read("docker","inspect","-f",'{{index .Config.Labels "org.opencontainers.image.revision"}}',WEB) != sha:
            raise RuntimeError("Unexpected running revision")
        recovery = LIVE / "klue/web-current.json"
        undo.callback(atomic, recovery, recovery.read_bytes())
        put(recovery, profile)
        saved_app = LIVE / "klue/application.json"
        local_app = json.loads(saved_app.read_bytes())
        local_app["spec"]["source"] = kube("argocd", "application", APP)["spec"]["source"]
        put(saved_app, local_app)
        undo.pop_all()


if __name__ == "__main__":
    os.umask(0o077)
    if sys.argv[1:] == ["verify"]:
        verify()
    else:
        with (LIVE / ".deploy-web.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            main(*sys.argv[1:])
