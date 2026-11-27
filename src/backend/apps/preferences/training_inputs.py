"""Check the artifacts used by the training container before offering a selection."""
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import boto3
import yaml
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.core.cache import cache
from django.conf import settings
from django.db import connection


# The trainer accepts this pinned KLUE encoder or a tenant's registered multitask checkpoint.
ENCODER_REVISION = "77c8b3d707df785034b4e50f2da5d37be5f0f546"


def storage():
    return boto3.client("s3", endpoint_url=os.getenv("MLFLOW_S3_ENDPOINT_URL") or None,
                        config=Config(connect_timeout=2, read_timeout=8, retries={"total_max_attempts": 2}))


def location(uri):
    url = urlsplit(uri)
    key = url.path.lstrip("/")
    if url.scheme != "s3" or not url.netloc or not key or url.query or url.fragment:
        raise ValueError("S3 객체 경로가 필요합니다.")
    return url.netloc, key


def training_domains():
    filename = os.getenv("VOC_TRAINING_DOMAINS_FILE")
    if not filename:
        return None
    domains = json.loads(Path(filename).read_text(encoding="utf-8"))
    if not isinstance(domains, dict) or not domains:
        raise ValueError("학습에 사용할 도메인 매핑이 필요합니다.")
    for route in domains.values():
        categories = route["categories"]
        if (route["task"] not in {"commerce", "finance", "food_delivery"} or not categories
                or not isinstance(categories, dict)
                or any(not isinstance(code, str) or not code.strip() for code in categories.values())
                or len(set(categories.values())) != len(categories)):
            raise ValueError("학습기의 도메인·분류 매핑을 확인해 주세요.")
    return domains


def valid_snapshot(file, row_count, domains):
    import pyarrow.parquet as pq
    file.seek(0)
    snapshot = pq.ParquetFile(file)
    contract = json.loads((Path(settings.BASE_DIR).parent / "contracts/training-snapshot.json").read_text())
    fields = [(name, kind) for kind, names in contract.items() for name in names]
    if snapshot.metadata.num_rows != row_count or [(f.name, str(f.type)) for f in snapshot.schema_arrow] != fields:
        return False
    # Match the trainer's two prediction heads and check every row, including later batches.
    columns = ["domain_uuid", "sentiment_label_uuid", "sentiment_value", "category_label_uuid",
               "category_uuid", "title", "body", "unresolved_count", "avg_first_reply_7d_seconds"]
    has_sentiment = has_category = False
    for batch in snapshot.iter_batches(batch_size=2048, columns=columns):
        for row in batch.to_pylist():
            domain = row["domain_uuid"]
            if not domain or (domains is not None and domain not in domains):
                return False
            if row["sentiment_label_uuid"]:
                if row["sentiment_value"] not in {"긍정", "중립", "부정"}:
                    return False
                has_sentiment = True
            if row["category_label_uuid"]:
                category = row["category_uuid"]
                if not category or (domains is not None and category not in domains[domain]["categories"]):
                    return False
                has_category = True
            if row["sentiment_label_uuid"] or row["category_label_uuid"]:
                if not ((row["body"] or "").strip() or (row["title"] or "").strip()):
                    return False
                numbers = (row["unresolved_count"], row["avg_first_reply_7d_seconds"])
                if any(n is not None and (not math.isfinite(n) or n < 0) for n in numbers):
                    return False
    return has_sentiment and has_category


def evaluation_available(uri, digest, row_count):
    try:
        bucket, key = location(uri)
        s3 = storage()
        obj = {"Bucket": bucket, "Key": key}
        metadata = s3.head_object(**obj)
        if metadata["ContentLength"] < 12:
            return False
        domains = training_domains()
        identity = repr((uri, digest, row_count, metadata.get("VersionId"), metadata["ETag"],
                         json.dumps(domains, sort_keys=True)))
        cache_key = "voc-eval-" + hashlib.sha256(identity.encode()).hexdigest()
        if cache.get(cache_key):
            return True
        checksum = hashlib.sha256()
        with tempfile.SpooledTemporaryFile(max_size=8*1024*1024) as contents:
            with s3.get_object(**obj, IfMatch=metadata["ETag"])["Body"] as stream:
                while chunk := stream.read(1024*1024):
                    checksum.update(chunk)
                    contents.write(chunk)
            valid = checksum.hexdigest() == digest and valid_snapshot(contents, row_count, domains)
        if valid:
            cache.set(cache_key, True, 3600)
        return valid
    except (ValueError, KeyError, TypeError, OSError, BotoCoreError, ClientError):
        return False


def evaluation_sets(company, version=None):
    with connection.cursor() as cur:
        cur.execute("""SELECT dataset_version,row_count,snapshot_ref,sha256 FROM voc.training_dataset
            WHERE company_uuid=%s AND purpose='eval' AND finalized_at IS NOT NULL AND row_count>0
                AND sha256 ~ '^[0-9a-f]{64}$' AND snapshot_ref LIKE 's3://%%'
                AND (%s::text IS NULL OR dataset_version=%s)
            ORDER BY finalized_at DESC,dataset_version DESC""", (company, version, version))
        rows = cur.fetchall()
    return [(name, f"{name} ({count}건)") for name, count, uri, digest in rows
            if evaluation_available(uri, digest, count)]


def model_files_available(uri, role):
    try:
        bucket, prefix = location(uri)
        s3 = storage()
        prefix = prefix.rstrip("/") + "/"
        response = s3.get_object(Bucket=bucket, Key=prefix + "MLmodel")
        with response["Body"] as manifest:
            flavors = yaml.safe_load(manifest.read(262144))["flavors"]
        if role == "multitask":
            folder = flavors["python_function"]["artifacts"]["checkpoint"]["path"]
            files = ["model.safetensors", "config.json", "tokenizer.json", "tokenizer_config.json", "input_features.json"]
        else:
            if "transformers" not in flavors:
                return False
            folder, files = "", ["model/model.safetensors", "model/config.json", "components/tokenizer/tokenizer_config.json"]
        path = PurePosixPath(folder)
        if path.is_absolute() or ".." in path.parts:
            return False
        root = prefix + (str(path) + "/" if folder else "")
        for name in files:
            if s3.head_object(Bucket=bucket, Key=root + name)["ContentLength"] <= 0:
                return False
        if role == "multitask":
            with s3.get_object(Bucket=bucket, Key=root + "input_features.json")["Body"] as f:
                features = json.loads(f.read(65536))
            if features != {"features": ["title","body","unresolved_count","avg_first_reply_7d_seconds"],
                            "preprocess_version": "title-body-log1p-v1"}:
                return False
            with s3.get_object(Bucket=bucket, Key=root + "config.json")["Body"] as f:
                config = json.loads(f.read(262144))
            if (config.get("architectures") != ["VocMultitaskBert"] or not config.get("voc_uses_customer_features")
                    or set(config["voc_task_labels"]["sentiment"]) != {"positive","negative","neutral"}):
                return False
            # Deployments can mount the same domain ConfigMap that Argo supplies to the trainer.
            domains = training_domains()
            if domains is not None:
                grouped = {}
                for domain in domains.values():
                    grouped.setdefault(domain["task"],set()).update(domain["categories"].values())
                expected = {task: {label: i for i,label in enumerate(sorted(labels))}
                            for task,labels in grouped.items()}
                expected["sentiment"] = {"negative":0,"neutral":1,"positive":2}
                if config["voc_task_labels"] != expected:
                    return False
        return True
    except (ValueError, TypeError, KeyError, OSError, yaml.YAMLError, BotoCoreError, ClientError):
        return False
