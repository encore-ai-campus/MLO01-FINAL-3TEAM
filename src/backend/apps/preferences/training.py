import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from config.http import DeskError
from . import training_inputs
from .training_status import FAILURES, TERMINAL, project

WORKFLOW_NAME = re.compile(r"voc-training-[0-9a-f]{32}")
logger = logging.getLogger(__name__)

def request_json(url, token, payload=None, *, method=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    with urlopen(Request(url, data=data, headers=headers, method=method), timeout=8) as response:
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
            if role == "encoder" and tags.get("source_revision") == training_inputs.ENCODER_REVISION:
                label = "사전 학습"
            elif role == "multitask" and tags.get("company_uuid") == str(company):
                label = "VoC 파인튜닝"
            else:
                continue
            name, number = version["name"], str(version["version"])
            if version.get("status") == "READY" and re.fullmatch(r"[A-Za-z0-9_.-]+/[1-9][0-9]*", f"{name}/{number}"):
                endpoint = settings.MLFLOW_TRACKING_URI.rstrip("/") + "/api/2.0/mlflow/model-versions/get-download-uri"
                try:
                    uri = request_json(endpoint + "?" + urlencode({"name": name, "version": number}),
                                       settings.MLFLOW_TRACKING_TOKEN)["artifact_uri"]
                except HTTPError as error:
                    if error.code == 404:
                        continue
                    raise
                if training_inputs.model_files_available(uri, role):
                    models.append((name, int(number), label))
        token = result.get("next_page_token")
        if not token:
            break
        query["page_token"] = token
    return [
        (f"models:/{name}/{version}", f"{name} · v{version} ({label})")
        for name, version, label in sorted(models, key=lambda item: (item[0], -item[1]))
    ]


def argo_request(path="", payload=None, query=None, *, method=None):
    namespace = quote(settings.ARGO_WORKFLOWS_NAMESPACE, safe="")
    url = f"{settings.ARGO_WORKFLOWS_URL.rstrip('/')}/api/v1/workflows/{namespace}"
    token = settings.ARGO_WORKFLOWS_TOKEN
    if not token:
        token_file = Path(settings.ARGO_WORKFLOWS_TOKEN_FILE)
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
    search = f"?{urlencode(query)}" if query else ""
    if method:
        return request_json(f"{url}{path}{search}", token, payload, method=method)
    return request_json(f"{url}{path}{search}", token, payload) if payload is not None else request_json(
        f"{url}{path}{search}", token,
    )


def parameters_for(workflow):
    return {field["name"]: field.get("value") for field in workflow.get("spec", {}).get("arguments", {}).get("parameters", [])}


class TrainingBusy(ValueError):
    """The tenant already has a training workflow in flight."""


def workflow_name(actor, selection):
    return "voc-training-" + uuid5(NAMESPACE_URL, f"{actor['company_uuid']}/{actor['staff_user_uuid']}/{selection['request_id']}").hex


def check_training_slot(company, name, parameters):
    query = {"listOptions.labelSelector": f"voc.company={company}", "listOptions.limit": 100}
    busy = False
    while True:
        page = argo_request(query=query)
        for workflow in page.get("items") or []:
            if workflow.get("spec", {}).get("workflowTemplateRef", {}).get("name") != "voc-training":
                continue
            args = parameters_for(workflow)
            if args.get("company") != str(company):
                continue
            if workflow["metadata"]["name"] == name:
                if any(args.get(key) != value for key, value in parameters.items()):
                    raise ValueError("The training request already has different inputs")
                return name
            if workflow.get("status", {}).get("phase") not in ("Succeeded", "Failed", "Error"):
                busy = True
        cursor = page.get("metadata", {}).get("continue")
        if not cursor:
            break
        query["listOptions.continue"] = cursor
    if busy:
        raise TrainingBusy("이 회사에서 학습이 진행 중입니다. 완료 후 다음 학습을 시작합니다.")


@transaction.atomic
def submit(actor, selection):
    company, staff = actor["company_uuid"], actor["staff_user_uuid"]
    name = workflow_name(actor, selection)
    parameters = {
        "company": str(company), "eval-dataset": selection["eval_dataset"], "base-model-uri": selection["model"],
        "label-cutoff": selection["as_of_at"].isoformat(), "model-name": settings.VOC_TRAINING_MODEL_NAME,
    }
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('voc-training-submit/' || %s::text,0))", (str(company),))
        cursor.execute("SELECT to_regclass('voc.retraining_policy')")
        has_schedule = False
        if cursor.fetchone()[0]:
            cursor.execute("""SELECT EXISTS(SELECT 1 FROM voc.retraining_policy p WHERE p.company_uuid=%s
                AND (p.enabled OR EXISTS (SELECT 1 FROM voc.retraining_dispatch d
                     WHERE d.company_uuid=p.company_uuid AND d.state IN ('pending','running'))))""", (company,))
            has_schedule = cursor.fetchone()[0]
    if has_schedule or actor.get("scheduled"):
        existing = check_training_slot(company, name, parameters)
        if existing:
            return existing
    if actor.get("submit_before") and timezone.now() >= actor["submit_before"]:
        raise TrainingBusy("실행 가능 시간이 끝나 다음 시간대를 기다립니다.")
    origin = "scheduled" if actor.get("scheduled") else "manual"
    try:
        workflow = argo_request("/submit", {
            "resourceKind": "WorkflowTemplate",
            "resourceName": "voc-training",
            "submitOptions": {
                "name": name,
                "parameters": [f"{key}={value}" for key, value in parameters.items()],
                "labels": f"voc.company={company},voc.requested-by={staff},voc.trigger={origin}",
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


def stop_outcome(workflow):
    """Classify an observed terminal outcome; setting spec.shutdown alone is only a request."""
    phase = workflow.get("status", {}).get("phase")
    if phase not in TERMINAL:
        return None
    if phase in FAILURES and workflow.get("spec", {}).get("shutdown") in {"Stop", "Terminate"}:
        return "stopped"
    return "completed"


def _registration_started(workflow):
    nodes = workflow.get("status", {}).get("nodes") or {}
    return any(node.get("type") == "Pod" and node.get("templateName") == "register"
               and node.get("phase") not in (None, "Pending") for node in nodes.values())


def _has_registered(cursor, uid):
    cursor.execute("SELECT EXISTS (SELECT 1 FROM voc.model_version WHERE workflow_ref=%s)", (uid,))
    return cursor.fetchone()[0]


def _stop_record(cursor, name, company, *, lock=False):
    cursor.execute("""SELECT s.workflow_uid,s.state,coalesce(u.name,u.email) AS requested_by,s.requested_at,s.confirmed_at
        FROM voc.training_stop_request s JOIN public.staff_user u
          ON u.company_uuid=s.company_uuid AND u.staff_user_uuid=s.requested_by
        WHERE s.workflow_name=%s AND s.company_uuid=%s""" + (" FOR UPDATE OF s" if lock else ""), (name, company))
    row = cursor.fetchone()
    return dict(zip((col.name for col in cursor.description), row)) if row else None


def _confirm_stop(cursor, name, company, record, state):
    cursor.execute("""UPDATE voc.training_stop_request SET state=%s,confirmed_at=clock_timestamp()
        WHERE workflow_name=%s AND company_uuid=%s AND state='requested' RETURNING confirmed_at""",
        (state, name, company))
    record.update(state=state, confirmed_at=cursor.fetchone()[0])
    return record


def _public_stop(record):
    return {key: record[key] for key in ("state", "requested_by", "requested_at", "confirmed_at")}


def reconcile_stop_request(name, company):
    """Deliver a committed intent. Concurrent deliveries serialize on the audit row."""
    with transaction.atomic(), connection.cursor() as cursor:
        record = _stop_record(cursor, name, company, lock=True)
        if not record or record["state"] != "requested":
            return _public_stop(record) if record else None
        uid = record["workflow_uid"]
        if _has_registered(cursor, uid):
            return _public_stop(_confirm_stop(cursor, name, company, record, "completed"))
        flow = get_workflow(name, company)
        if flow["metadata"]["uid"] != uid:
            raise DeskError(409, "WORKFLOW_REPLACED", "학습 실행 식별자가 변경되었습니다.")
        outcome = stop_outcome(flow)
        if not outcome and not flow.get("spec", {}).get("shutdown"):
            flow = argo_request(f"/{name}/stop", {}, method="PUT")
            if not owns_workflow(flow, company) or flow["metadata"].get("uid") != uid:
                raise ValueError("Argo returned a different workflow after stop")
            outcome = stop_outcome(flow)
        if outcome:
            _confirm_stop(cursor, name, company, record, "completed" if _has_registered(cursor, uid) else outcome)
        return _public_stop(record)


def request_stop(actor, name):
    if actor.get("role") != "super_admin":
        raise DeskError(403, "TRAINING_FORBIDDEN", "모델 학습 권한이 필요합니다.")
    company = actor["company_uuid"]
    flow = get_workflow(name, company)
    uid = flow["metadata"]["uid"]
    # Commit intent before calling Argo, including when a response is lost or this process exits.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("voc-training-stop/"+name,))
        record = _stop_record(cursor, name, company)
        if record:
            if record["workflow_uid"] != uid:
                raise DeskError(409, "WORKFLOW_REPLACED", "학습 실행 식별자가 변경되었습니다.")
        else:
            cursor.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))", ("voc-training-register/"+uid,))
            obtained_registration_lock = cursor.fetchone()[0]
            if (not obtained_registration_lock or stop_outcome(flow) or _registration_started(flow)
                    or _has_registered(cursor, uid)):
                raise DeskError(409, "TRAINING_CLOSING", "종료되었거나 모델 등록을 시작한 학습입니다.")
            try:
                cutoff = datetime.fromisoformat(parameters_for(flow).get("label-cutoff") or "")
                if timezone.is_naive(cutoff):
                    raise ValueError
            except ValueError as exc:
                raise DeskError(409, "INVALID_CUTOFF", "학습 기준 시각을 확인할 수 없습니다.") from exc
            cursor.execute("""INSERT INTO voc.training_stop_request
                (workflow_name,workflow_uid,company_uuid,requested_by,cutoff) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (workflow_name) DO NOTHING""", (name,uid,company,actor["staff_user_uuid"],cutoff))
        record = _stop_record(cursor, name, company)
        if not record:
            raise DeskError(409, "WORKFLOW_REPLACED", "중지 요청의 학습 실행을 확인할 수 없습니다.")
    try:
        return reconcile_stop_request(name, company)
    except (OSError, ValueError, KeyError):
        logger.warning("학습 중지 요청 전달 대기 workflow=%s", name)
        return _public_stop(record)


def workflow_stop_status(name, company, workflow, result):
    with connection.cursor() as cursor:
        # Older installations can still display run history while the new schema is installed.
        cursor.execute("SELECT to_regclass('voc.training_stop_request')")
        if not cursor.fetchone()[0]:
            return {"state": "unavailable"}
        record = _stop_record(cursor, name, company)
    outcome = stop_outcome(workflow)
    if record and record["workflow_uid"] == workflow["metadata"]["uid"]:
        stop = _public_stop(record)
        if stop["state"] == "requested" and (outcome or result.get("registry_version")):
            stop["state"] = "completed" if result.get("registry_version") else outcome
        return stop
    if outcome == "stopped":
        return {"state": "stopped"}
    if outcome or _registration_started(workflow) or result.get("registry_version"):
        return {"state": "unavailable"}
    return {"state": "requested" if workflow.get("spec", {}).get("shutdown") else "available"}


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
                    "label": "중단됨" if stop_outcome(workflow) == "stopped" else project(workflow)["label"],
                    "source": "자동 재학습" if metadata.get("labels", {}).get("voc.trigger") == "scheduled" else "수동 학습",
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
    data["stop"] = workflow_stop_status(name, company, workflow, result)
    if data["stop"]["state"] in {"requested", "stopped"}:
        data["label"] = "중단됨" if data["stop"]["state"] == "stopped" else "중지 요청 중"
    return data
