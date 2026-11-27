import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import pandas as pd
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError
from psycopg.rows import dict_row

from workers.classification.online_features import FEATURES, FEATURE_REFS, store

from .progress import NO_PROGRESS

_types = {str(t): t for t in (pa.string(), pa.timestamp("us", tz="UTC"), pa.int64(), pa.float64())}
_contract = Path(__file__).resolve().parents[2] / "contracts/training-snapshot.json"
SCHEMA = pa.schema([(name, _types[kind]) for kind, names in json.loads(_contract.read_text()).items() for name in names])


def database():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def checksum(value):
    return hashlib.sha256(value).hexdigest()


def s3():
    return boto3.client("s3", endpoint_url=os.environ["MLFLOW_S3_ENDPOINT_URL"])


def object_path(uri):
    url = urlsplit(uri)
    if url.scheme != "s3" or not url.netloc or not url.path.strip("/"):
        raise ValueError("Dataset requires a MinIO object URI")
    return {"Bucket": url.netloc, "Key": url.path.lstrip("/")}


def read_object(client, path):
    with client.get_object(**path)["Body"] as body:
        return body.read()


def load_snapshot(dataset):
    if not dataset["sha256"]:
        raise ValueError("Dataset is not finalized")
    contents = read_object(s3(), object_path(dataset["snapshot_ref"]))
    if checksum(contents) != dataset["sha256"]:
        raise ValueError("Dataset checksum mismatch")
    table = pq.read_table(io.BytesIO(contents))
    if not table.schema.equals(SCHEMA) or table.num_rows != dataset["row_count"]:
        raise ValueError("Dataset schema or row count mismatch")
    return table.to_pylist()


def find_dataset(cn, company, version, purpose):
    dataset = cn.execute("""
        SELECT * FROM voc.training_dataset
        WHERE company_uuid=%s AND dataset_version=%s AND purpose=%s AND finalized_at IS NOT NULL
    """, (company, version, purpose)).fetchone()
    if dataset is None:
        raise ValueError("No finalized dataset with this version for the company")
    return dataset


def select_labels(cn, company, *, cutoff=None, reviewed=None):
    if (cutoff is None) == (reviewed is None):
        raise ValueError("Supply a training cutoff or separately reviewed evaluation labels")
    reviews = {}
    if reviewed is not None:
        for review in reviewed:
            gold_id = str(review["gold_label_uuid"])
            reviewer = review["reviewer_uuid"]
            if gold_id in reviews or not review["evidence_ref"].strip():
                raise ValueError("Each evaluation label requires distinct review evidence")
            if not cn.execute("""
                SELECT 1 FROM public.staff_user WHERE company_uuid=%s AND staff_user_uuid=%s
            """, (company, reviewer)).fetchone():
                raise ValueError("Evaluation reviewer does not belong to the company")
            reviews[gold_id] = review
        if not reviews:
            raise ValueError("No reviewed evaluation labels")
    condition = (
        "l.gold_label_uuid=ANY(%s::uuid[])" if reviewed is not None else
        "l.confirmed_at<=%s AND l.label_origin IN ('counselor','seed')"
    )
    labels = cn.execute(f"""
        SELECT f.*, l.gold_label_uuid,l.label_kind,l.sentiment_value,l.category_uuid,
               l.label_origin,l.reviewer_uuid,l.completion_message_uuid,l.as_of_at,l.confirmed_at,l.evidence
        FROM voc.gold_label l JOIN voc.feature_snapshot f USING(feature_uuid)
        WHERE f.company_uuid=%s AND {condition} AND f.available_at<=l.as_of_at
          AND (l.label_kind<>'category' OR EXISTS (
              SELECT 1 FROM public.category c WHERE c.category_uuid=l.category_uuid AND c.domain_uuid=f.domain_uuid
          ))
        ORDER BY l.confirmed_at DESC,l.gold_label_uuid
    """, (company, list(reviews) if reviewed is not None else cutoff)).fetchall()
    if reviewed is not None and {str(label["gold_label_uuid"]) for label in labels} != reviews.keys():
        raise ValueError("At least one review references an unavailable or out-of-scope Gold label")
    grouped = {}
    for label in labels:
        key = str(label["feature_uuid"])
        kind = label["label_kind"]
        if key not in grouped:
            grouped[key] = {name: label.get(name) for name in SCHEMA.names}
        row = grouped[key]
        if row[f"{kind}_label_uuid"] is not None and row[f"{kind}_evidence"] is not None:
            if reviewed is not None:
                raise ValueError("Evaluation must select one label per snapshot and label kind")
            continue
        gold_id = str(label["gold_label_uuid"])
        row[f"{kind}_label_uuid"] = gold_id
        value_key = "sentiment_value" if kind == "sentiment" else "category_uuid"
        row[value_key] = label[value_key]
        row[f"{kind}_evidence"] = canonical({
            "origin": label["label_origin"], "reviewer_uuid": label["reviewer_uuid"],
            "completion_message_uuid": label["completion_message_uuid"],
            "confirmed_at": label["confirmed_at"], "source": label["evidence"],
            "evaluation_review": reviews.get(gold_id),
        })
        row["as_of_at"] = min(row["as_of_at"], label["as_of_at"])
    for row in grouped.values():
        row["text_sha256"] = checksum(" ".join(row["body"].casefold().split()).encode())
        row["calculation_sources"] = canonical(row["calculation_sources"])
        for name in SCHEMA.names:
            if name.endswith("_uuid") or name.endswith("_event_id"):
                row[name] = str(row[name]) if row[name] is not None else None
    return sorted(grouped.values(), key=lambda row: row["feature_uuid"])


def exclude_evaluation(rows, evaluation):
    conversations = {row["source_conversation"] for row in evaluation if row["source_conversation"]}
    texts = {row["text_sha256"] for row in evaluation}
    return [
        row for row in rows
        if row["source_conversation"] not in conversations and row["text_sha256"] not in texts
    ]


def retrieve_features(rows):
    entity = pd.DataFrame([
        {"feature_uuid": row["feature_uuid"], "event_timestamp": row["as_of_at"]} for row in rows
    ])
    results = store().get_historical_features(entity_df=entity, features=list(FEATURE_REFS)).to_df()
    by_id = {row["feature_uuid"]: row for row in results.to_dict("records")}
    if len(results) != len(rows) or len(by_id) != len(rows):
        raise ValueError("Feast did not return exactly one row per selected snapshot")
    for row in rows:
        if row["feature_uuid"] not in by_id:
            raise ValueError("Feast omitted a selected snapshot")
        picked = by_id[row["feature_uuid"]]
        for name in FEATURES:
            value = None if pd.isna(picked[name]) else picked[name]
            if value != row[name]:
                raise ValueError(f"Feast did not return the frozen {name}")
            row[name] = value


def finalize_dataset(cn, company, purpose, rows, progress=NO_PROGRESS):
    with progress.stage("feast"):
        if not rows:
            raise ValueError("No eligible dataset items")
        retrieve_features(rows)
    with progress.stage("dataset_save") as result:
        dataset, reused = _save_dataset(cn, company, purpose, rows)
        if reused:
            result["state"] = "reused"
        result["paths"] = {purpose: dataset["snapshot_ref"]}
    return dataset


def _save_dataset(cn, company, purpose, rows):
    fingerprint = checksum(canonical(rows).encode())
    # Keep the UUID and object path stable even if an upload outlives a failed DB transaction.
    reservation = cn.execute("""
        WITH identity AS (SELECT gen_random_uuid() AS id, nextval('voc.training_dataset_number') AS number)
        INSERT INTO voc.training_dataset (dataset_uuid,company_uuid,dataset_version,purpose,selection_sha256,snapshot_ref)
        SELECT id,%s,%s || '-v' || number,%s,%s,'s3://' || %s || '/' || id || '/data.parquet' FROM identity
        ON CONFLICT (company_uuid,purpose,selection_sha256) DO NOTHING
        RETURNING dataset_uuid
    """, (company, purpose, purpose, fingerprint, os.environ["VOC_DATASET_BUCKET"])).fetchone()
    with cn.transaction():
        dataset = cn.execute("""
            SELECT * FROM voc.training_dataset
            WHERE company_uuid=%s AND purpose=%s AND selection_sha256=%s FOR UPDATE
        """, (company, purpose, fingerprint)).fetchone()
        if reservation and dataset["dataset_uuid"] != reservation["dataset_uuid"]:
            raise RuntimeError("Dataset reservation mismatch")
        if dataset["finalized_at"]:
            load_snapshot(dataset)
            return dataset, True
        output = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), output, compression="zstd")
        payload = output.getvalue()
        path = object_path(dataset["snapshot_ref"])
        client = s3()
        reused = False
        try:
            client.put_object(**path, Body=payload, IfNoneMatch="*", ContentType="application/vnd.apache.parquet")
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                raise
            reused = True
        contents = read_object(client, path)
        actual = pq.read_table(io.BytesIO(contents))
        if not actual.schema.equals(SCHEMA) or actual.to_pylist() != rows:
            raise ValueError("MinIO object does not match selected dataset items")
        with cn.cursor() as cursor:
            cursor.executemany("""
                INSERT INTO voc.training_dataset_item
                (dataset_uuid,feature_uuid,as_of_at,sentiment_label_uuid,category_label_uuid)
                VALUES (%s,%s,%s,%s,%s)
            """, [
                (dataset["dataset_uuid"], row["feature_uuid"], row["as_of_at"],
                 row["sentiment_label_uuid"], row["category_label_uuid"]) for row in rows
            ])
        return cn.execute("""
            UPDATE voc.training_dataset SET sha256=%s,row_count=%s,finalized_at=clock_timestamp()
            WHERE dataset_uuid=%s RETURNING *
        """, (checksum(contents), len(rows), dataset["dataset_uuid"])).fetchone(), reused
