"""Project Argo workflow and per-attempt records into the training screen."""

from datetime import datetime

OVERALL = ("접수중", "접수 완료", "실행중", "실행 완료")
READ = ("데이터 요청", "데이터 로딩중", "로딩 완료")
SAVE = ("저장 요청", "저장중", "저장 완료")
STAGES = (
    ("데이터 준비", (
        ("eval_read", "MinIO 고정 평가셋 읽기", "prepare", READ),
        ("postgres", "PostgreSQL 데이터 조회", "prepare", READ),
        ("feast", "Feast Offline 피처 조회", "prepare", READ),
        ("dataset_save", "MinIO 학습셋 저장", "prepare", SAVE),
        ("training_read", "MinIO 학습·평가 파일 읽기", "train", READ),
    )),
    ("모델 학습", (
        ("model_load", "MLflow 시작 모델 로딩", "train", ("모델 로딩 요청", "모델 로딩 완료")),
        ("training", "학습", "train", ("학습중", "학습 완료")),
        ("evaluation", "평가", "train", ("평가중", "평가 완료")),
        ("model_save", "MinIO 모델 저장", "train", SAVE),
        ("registry", "MLflow Registry 등록", "register", ("등록 요청", "등록중", "등록 완료")),
    )),
)
FAILURES = {"Failed", "Error"}
TERMINAL = FAILURES | {"Succeeded"}
REASONS = {
    "invalid_data": "데이터나 모델 설정을 확인해 주세요.",
    "timeout": "연결 시간이 초과됐습니다.",
    "database": "PostgreSQL 작업을 완료하지 못했습니다.",
    "operation": "작업을 완료하지 못했습니다.",
}


def points(labels, active=None, state="active"):
    return [
        {"label": label, "state": state if i == active else "done" if active is None or i < active else "waiting"}
        for i, label in enumerate(labels)
    ]


def current_attempts(nodes):
    attempts = {}
    retrying = {}
    for parent in nodes.values():
        if parent.get("type") != "Retry":
            continue
        children = [nodes[child] for child in parent.get("children", ()) if child in nodes]
        if not children:
            continue
        child = children[-1]
        template = child.get("templateName", parent.get("templateName"))
        attempts[template] = child
        if parent.get("phase") == "Running":
            failed = next((item for item in reversed(children) if item.get("phase") in FAILURES), None)
            if failed:
                retrying[template] = failed
    for key, node in nodes.items():
        template = node.get("templateName")
        if node.get("type") == "Pod" and template not in attempts:
            attempts[template] = {**node, "id": node.get("id", key)}
    return attempts, retrying


def failure_reason(node):
    message = str(node.get("message", "")).lower()
    if "oomkilled" in message or "out of memory" in message:
        return "실행 메모리가 부족합니다."
    if "imagepull" in message or "image pull" in message:
        return "실행 이미지를 불러오지 못했습니다."
    if "deadline" in message or "timeout" in message:
        return REASONS["timeout"]
    return REASONS["operation"]


def project(workflow, records=()):
    status = workflow.get("status", {})
    phase = status.get("phase")
    attempts, retrying = current_attempts(status.get("nodes", {}))
    stopped = phase in FAILURES and workflow.get("spec", {}).get("shutdown") in {"Stop", "Terminate"}
    failed = phase in FAILURES
    complete = phase == "Succeeded"
    running = phase == "Running" or failed
    active = 3 if complete else 2 if running else 1
    label = ("실행 완료" if complete else "중단" if stopped else "실패" if failed else
             "재시도" if retrying else "실행중" if running else "접수 완료")
    overall = points(OVERALL, None if complete else active, "failed" if failed else "active")
    by_attempt = {(row["node_id"], row["operation"]): row for row in records}
    cards = []
    paths = {}
    for heading, stages in STAGES:
        rows = []
        for operation, title, template, labels in stages:
            node = attempts.get(template, {})
            record = by_attempt.get((node.get("id"), operation), {})
            # A manual Argo retry can reuse a node ID.
            if (record and node.get("startedAt")
                    and datetime.fromisoformat(record["updated_at"]) < datetime.fromisoformat(node["startedAt"])):
                record = {}
            state = record.get("state")
            message = ""
            if state in {"completed", "reused"}:
                step = points(labels)
                message = "재사용" if state == "reused" else ""
                paths.update(record.get("paths") or {})
            elif state in {"requested", "running", "failed"}:
                broken = state == "failed" or node.get("phase") in FAILURES
                step = points(labels, 0 if state == "requested" or len(labels) == 2 else 1,
                              "failed" if broken else "active")
                if broken:
                    message = REASONS.get(record.get("reason"), failure_reason(node))
                    message = f"{'재시도' if template in retrying else '중단' if stopped else '실패'} · {message}"
                elif template in retrying:
                    message = f"재시도 중 · {failure_reason(retrying[template])}"
            else:
                step = points(labels, 0, "waiting")
                if node.get("phase") in TERMINAL or complete or (failed and not records):
                    message = "세부 기록 없음"
            rows.append({"title": title, "points": step, "message": message})
        cards.append({"title": heading, "rows": rows})
    failed_node = next((node for node in attempts.values() if node.get("phase") in FAILURES), status)
    notice = failure_reason(failed_node) if failed and not stopped else ""
    if retrying and not failed:
        notice = failure_reason(next(iter(retrying.values())))
    return {
        "overall": overall, "label": label, "terminal": phase in TERMINAL, "cards": cards, "paths": paths,
        "notice": notice,
    }
