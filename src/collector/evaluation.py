"""Evaluate both deployed versions against one independently reviewed snapshot."""
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .inference import KINDS, call, endpoint, finish_call, start_call, validated_predictions
from .pipeline import db


class EvaluationError(ValueError):
    pass


def macro_f1(pairs):
    expected = Counter(actual for actual, _ in pairs)
    predicted = Counter(guess for _, guess in pairs)
    correct = Counter(actual for actual, guess in pairs if actual == guess)
    labels = expected.keys() | predicted.keys()
    return sum(2 * correct[label] / (expected[label] + predicted[label]) for label in labels) / len(labels)


def valid_text(value):
    return isinstance(value, str) and bool(value.strip())


def checked_snapshot(path):
    data = json.loads(Path(path).read_bytes())
    labels, sample_ids, label_ids = [], set(), set()
    if (not isinstance(data, dict) or not valid_text(data.get("dataset_version"))
            or not isinstance(data.get("samples"), list) or not data["samples"]):
        raise EvaluationError("평가셋 버전과 평가 항목을 입력해 주세요.")
    for sample in data["samples"]:
        if (not isinstance(sample, dict) or not valid_text(sample.get("id")) or sample["id"] in sample_ids
                or not valid_text(sample.get("text")) or not isinstance(sample.get("title"), str)
                or not isinstance(sample.get("labels"), dict) or not sample["labels"]
                or not set(sample["labels"]) <= set(KINDS)):
            raise EvaluationError("항목 ID·제목·본문 또는 정답 구성을 확인해 주세요.")
        sample_ids.add(sample["id"])
        UUID(sample["domain_id"])
        for kind, label in sample["labels"].items():
            if (not isinstance(label, dict) or label.get("origin") != "independent_review"
                    or not all(valid_text(label.get(key)) for key in ("id", "value", "reviewer", "evidence"))
                    or label["id"] in label_ids):
                raise EvaluationError("각 정답에는 고유 ID, 독립 검수 담당자와 검수 근거가 필요합니다.")
            label_ids.add(label["id"])
            labels.append((data["dataset_version"], label["id"], sample["id"], kind, label["value"],
                           label["reviewer"], label["evidence"]))
    checksum = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return data, checksum, labels


def register_snapshot(cn, data, checksum, labels):
    with cn.transaction():
        for sample in data["samples"]:
            desired = {kind: entry["value"] for kind, entry in sample["labels"].items()}
            requested = {"sentiment": desired.get("sentiment"), "category_id": desired.get("category")}
            if desired != validated_predictions(cn, sample["domain_id"], requested, desired.keys()):
                raise EvaluationError("감성 라벨 또는 해당 도메인에 등록된 카테고리 ID를 확인해 주세요.")
        created = cn.execute("""
            INSERT INTO voc.evaluation_set(dataset_version,checksum) VALUES (%s,%s) ON CONFLICT DO NOTHING
        """, (data["dataset_version"], checksum)).rowcount
        if created:
            with cn.cursor() as cursor:
                cursor.executemany("""
                    INSERT INTO voc.evaluation_label(dataset_version,label_id,sample_id,kind,expected,reviewer_ref,review_evidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                """, labels)
        else:
            existing = cn.execute("SELECT checksum FROM voc.evaluation_set WHERE dataset_version=%s",
                                  (data["dataset_version"],)).fetchone()
            if existing["checksum"] != checksum:
                raise EvaluationError("이미 등록한 버전의 평가셋 내용은 변경할 수 없습니다.")


def measure(cn, sample, route, traffic, ticket=None, body_version=None):
    request = {"title": sample["title"], "text": sample["text"], "domain_id": str(sample["domain_id"]),
               "kinds": list(sample["kinds"])}
    attempt_id = uuid4()
    start_call(cn, attempt_id, "classification", traffic, route, ticket, body_version)
    result = call(endpoint("classification", route), request)
    ended = datetime.now(timezone.utc)
    predictions = {}
    if not result.error:
        if not result.model_name or not result.model_version or result.rollout != route:
            result.error = "unexpected_model_identity"
        else:
            predictions = validated_predictions(cn, sample["domain_id"], result.payload, sample["kinds"])
            if len(predictions) != len(sample["kinds"]):
                result.error = "incomplete_classification"
    finish_call(cn, attempt_id, ended, result)
    if result.error:
        raise EvaluationError(f"{route} 분류 API 호출을 확인해 주세요. ({result.error})")
    return (result.model_name, result.model_version), predictions


def evaluate(path):
    routes = ("stable", "canary")
    if not all(endpoint("classification", route) for route in routes):
        raise EvaluationError("Stable과 Canary의 직접 분류 API 주소가 필요합니다.")
    data, checksum, labels = checked_snapshot(path)
    run, scores = uuid4(), []
    with db() as cn:
        register_snapshot(cn, data, checksum, labels)
        for route in routes:
            evaluated, version = {kind: [] for kind in KINDS}, None
            for sample in data["samples"]:
                identity, predicted = measure(cn, {**sample, "kinds": sample["labels"].keys()}, route, "evaluation")
                if version is not None and identity != version:
                    raise EvaluationError("평가 중 모델 버전이 바뀌었습니다. 배포 상태를 확인해 주세요.")
                version = identity
                for kind, guess in predicted.items():
                    evaluated[kind].append((sample["labels"][kind]["value"], guess))
            for kind, pairs in evaluated.items():
                if pairs:
                    scores.append((run, data["dataset_version"], route, *version, kind, macro_f1(pairs), len(pairs)))
        with cn.transaction(), cn.cursor() as cursor:
            cursor.executemany("""
                INSERT INTO voc.evaluation_result(run_id,dataset_version,rollout,model_name,model_version,kind,macro_f1,samples)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, scores)
    print(f"고정 평가 완료 {run}")


def replay(source):
    if not endpoint("classification", "canary"):
        raise EvaluationError("Canary의 직접 분류 API 주소가 필요합니다.")
    with db() as cn:
        row = cn.execute("""
            SELECT t.ticket_uuid,t.domain_uuid AS domain_id,h.body_version,coalesce(h.model_title,s.title,'') AS title,h.model_text AS text
            FROM voc.model_text_history h JOIN public.ticket t ON t.source_event_id=h.event_id
            JOIN voc.silver_messages s ON s.event_id=h.event_id AND s.schema_version=4 AND s.body_version=h.body_version
            WHERE h.event_id=%s AND s.message_kind='customer_inquiry'
            ORDER BY h.preprocess_version DESC LIMIT 1
        """, (UUID(source),)).fetchone()
        if not row:
            raise EvaluationError("재생할 문의 원본이나 모델 입력을 찾을 수 없습니다.")
        identity, _ = measure(cn, {**row, "kinds": KINDS}, "canary", "replay", row["ticket_uuid"], row["body_version"])
        print(f"Canary 재생 완료 {identity[0]} {identity[1]}")
