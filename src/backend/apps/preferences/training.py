import json
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from django.conf import settings
from django.db import connection

from config.http import DeskError
from .training_status import project

WORKFLOW_NAME = re.compile(r"voc-training-[0-9a-f]{32}")

def request_json(url, token, payload=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    with urlopen(Request(url, data=data, headers=headers), timeout=8) as response:
        return json.load(response)


def available_models(company):
    url = settings.MLFLOW_TRACKING_URI.rstrip("/") + "/api/2.0/mlflow/model-versions/search"
    models = []
    query = {"max_results": 100}
    while True:
        result = request_json(f"{url}?{urlencode(query)}", settings.MLFLOW_TRACKING_TOKEN)
        for version in result.get("model_versions", []):
            tags = {tag["key"]: tag["value"] for tag in version.get("tags", [])}
            role = tags.get("voc_training_input")
            if role == "encoder":
                label = "사전 학습"
            elif role == "multitask" and tags.get("company_uuid") == str(company):
                label = "VoC 파인튜닝"
            else:
                continue
            name, number = version["name"], str(version["version"])
            if version.get("status") == "READY" and re.fullmatch(r"[A-Za-z0-9_.-]+/[1-9][0-9]*", f"{name}/{number}"):
                models.append((name, int(number), label))
        token = result.get("next_page_token")
        if not token:
            break
        query["page_token"] = token
    return [
        (f"models:/{name}/{version}", f"{name} · v{version} ({label})")
        for name, version, label in sorted(models, key=lambda item: (item[0], -item[1]))
    ]


def argo_request(path="", payload=None, query=None):
    namespace = quote(settings.ARGO_WORKFLOWS_NAMESPACE, safe="")
    url = f"{settings.ARGO_WORKFLOWS_URL.rstrip('/')}/api/v1/workflows/{namespace}"
    token = settings.ARGO_WORKFLOWS_TOKEN
    if not token:
        token_file = Path(settings.ARGO_WORKFLOWS_TOKEN_FILE)
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
    search = f"?{urlencode(query)}" if query else ""
    return request_json(f"{url}{path}{search}", token, payload) if payload is not None else request_json(
        f"{url}{path}{search}", token,
    )


def parameters_for(workflow):
    return {field["name"]: field.get("value") for field in workflow.get("spec", {}).get("arguments", {}).get("parameters", [])}


def submit(actor, selection):
    company, staff = actor["company_uuid"], actor["staff_user_uuid"]
    name = "voc-training-" + uuid5(NAMESPACE_URL, f"{company}/{staff}/{selection['request_id']}").hex
    parameters = {
        "company": str(company), "eval-dataset": selection["eval_dataset"], "base-model-uri": selection["model"],
        "label-cutoff": selection["as_of_at"].isoformat(), "model-name": settings.VOC_TRAINING_MODEL_NAME,
    }
    try:
        workflow = argo_request("/submit", {
            "resourceKind": "WorkflowTemplate",
            "resourceName": "voc-training",
            "submitOptions": {
                "name": name,
                "parameters": [f"{key}={value}" for key, value in parameters.items()],
                "labels": f"voc.company={company},voc.requested-by={staff}",
            },
        })
    except HTTPError as error:
        if error.code != 409:
            raise
        workflow = argo_request(f"/{name}")
        existing = parameters_for(workflow)
        if any(existing.get(key) != value for key, value in parameters.items()):
            raise ValueError("A different Workflow already exists for this request") from error
    if workflow["metadata"]["name"] != name:
        raise ValueError("Argo returned an unexpected Workflow name")
    return name


def owns_workflow(workflow, company):
    metadata = workflow.get("metadata", {})
    labels = metadata.get("labels", {})
    return (
        WORKFLOW_NAME.fullmatch(metadata.get("name", "")) is not None
        and labels.get("voc.company") == str(company)
        and bool(labels.get("voc.requested-by"))
        and workflow.get("spec", {}).get("workflowTemplateRef", {}).get("name") == "voc-training"
        and parameters_for(workflow).get("company") == str(company)
    )


def get_workflow(name, company):
    if WORKFLOW_NAME.fullmatch(name):
        try:
            workflow = argo_request(f"/{name}")
        except HTTPError as error:
            if error.code != 404:
                raise
        else:
            if workflow.get("metadata", {}).get("name") == name and owns_workflow(workflow, company):
                return workflow
    raise DeskError(404, "TRAINING_RUN_NOT_FOUND", "학습 실행을 찾을 수 없습니다.")


def recent_workflows(company):
    query = {"listOptions.labelSelector": f"voc.company={company},voc.requested-by", "listOptions.limit": 100}
    rows = []
    while True:
        result = argo_request(query=query)
        for workflow in result.get("items") or []:
            if owns_workflow(workflow, company):
                metadata, parameters = workflow["metadata"], parameters_for(workflow)
                rows.append({
                    "name": metadata["name"], "submitted_at": metadata.get("creationTimestamp"),
                    "model": parameters.get("base-model-uri"), "evaluation": parameters.get("eval-dataset"),
                    "label": project(workflow)["label"],
                })
        cursor = result.get("metadata", {}).get("continue")
        if not cursor:
            break
        query["listOptions.continue"] = cursor
    return sorted(rows, key=lambda row: (row["submitted_at"] or "", row["name"]), reverse=True)[:10]


def progress_and_result(ref, company):
    with connection.cursor() as cursor:
        cursor.execute("""SELECT node_id,operation,state,reason,paths,updated_at
                          FROM voc.training_progress WHERE workflow_ref=%s""", (ref,))
        progress = [
            {"node_id": node, "operation": op, "state": state, "reason": reason,
             "paths": json.loads(paths) if isinstance(paths, str) else paths, "updated_at": updated.isoformat()}
            for node, op, state, reason, paths, updated in cursor.fetchall()
        ]
        cursor.execute("""
            SELECT train.dataset_version AS train_version,train.row_count AS train_count,train.snapshot_ref AS train_path,
                   eval.dataset_version AS eval_version,eval.row_count AS eval_count,eval.snapshot_ref AS eval_path,
                   r.sentiment_macro_f1,r.category_macro_f1,r.mlflow_run_id,
                   m.model_name,m.registry_version
            FROM voc.training_run r
            JOIN voc.training_dataset train ON train.dataset_uuid=r.train_dataset_uuid
            JOIN voc.training_dataset eval ON eval.dataset_uuid=r.eval_dataset_uuid
            LEFT JOIN voc.model_version m ON m.workflow_ref=r.workflow_ref
            WHERE r.workflow_ref=%s AND train.company_uuid=%s AND eval.company_uuid=train.company_uuid
        """, (ref, company))
        row = cursor.fetchone()
        return progress, dict(zip([item.name for item in cursor.description], row)) if row else {}


def browser_url(value):
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None:
        return parsed._replace(query="", fragment="").geturl().rstrip("/")
    return ""


def service_links(name, result):
    links = {}
    argo = browser_url(settings.ARGO_WORKFLOWS_UI_URL)
    mlflow = browser_url(settings.MLFLOW_UI_URL or settings.MLFLOW_TRACKING_URI)
    minio = browser_url(settings.MINIO_CONSOLE_URL)
    if argo:
        links["Argo 실행"] = f"{argo}/workflows/{quote(settings.ARGO_WORKFLOWS_NAMESPACE, safe='')}/{name}"
    if mlflow:
        links["MLflow"] = mlflow
        run_id = result.get("mlflow_run_id")
        if run_id:
            try:
                query = urlencode({"run_id": run_id})
                run = request_json(f"{settings.MLFLOW_TRACKING_URI.rstrip('/')}/api/2.0/mlflow/runs/get?{query}",
                                   settings.MLFLOW_TRACKING_TOKEN)
                experiment = quote(str(run["run"]["info"]["experiment_id"]), safe="")
                links["MLflow 실행"] = f"{mlflow}/#/experiments/{experiment}/runs/{quote(run_id, safe='')}"
            except (OSError, ValueError, KeyError):
                pass
        if result.get("registry_version"):
            model = quote(result["model_name"], safe="")
            links["MLflow 등록 모델"] = f"{mlflow}/#/models/{model}/versions/{result['registry_version']}"
    if minio:
        links["MinIO 콘솔"] = minio
    return links


def snapshot(name, company):
    workflow = get_workflow(name, company)
    progress, result = progress_and_result(workflow["metadata"]["uid"], company)
    data = project(workflow, progress)
    for key in ("train", "eval"):
        if result.get(f"{key}_path"):
            data["paths"][key] = result[f"{key}_path"]
    data["result"] = result
    data["links"] = service_links(name, result)
    return data
