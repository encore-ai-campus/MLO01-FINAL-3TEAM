"""Import the original KLUE-BERT v1 and A.X comparison report for one company."""
import hashlib
import json
import math
import re
from pathlib import Path
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


TASKS = ("sentiment", "commerce", "finance", "food_delivery")
MODELS = ("klue_bert_v1", "ax_encoder")


def read_model(archive):
    config = json.loads(archive.read("model/config.json"))
    with archive.open("model/model.safetensors") as weights:
        digest = hashlib.file_digest(weights, "sha256").hexdigest()
    return config["voc_task_labels"], digest


def counts_from_matrix(evaluation, labels, expected_rows):
    matrix = evaluation["confusion_matrix"]
    if len(matrix) != len(labels) or any(len(row) != len(labels) for row in matrix):
        raise ValueError("혼동행렬의 크기와 라벨 수가 다릅니다.")
    if any(type(value) is not int or value < 0 for row in matrix for value in row):
        raise ValueError("혼동행렬 건수가 올바르지 않습니다.")
    if sum(map(sum, matrix)) != expected_rows or evaluation["rows"] != expected_rows:
        raise ValueError("혼동행렬 합계와 평가 건수가 다릅니다.")
    counts = {
        code: {"n": sum(matrix[i]), "tp": matrix[i][i], "predicted": sum(row[i] for row in matrix)}
        for i, code in enumerate(labels)
    }
    if any(stats["n"] != evaluation["per_label"][code]["support"] for code, stats in counts.items()):
        raise ValueError("혼동행렬과 유형별 정답 표본이 다릅니다.")
    f1 = [2 * s["tp"] / (s["n"] + s["predicted"]) if s["n"] + s["predicted"] else 0
          for s in counts.values()]
    if not math.isclose(sum(f1) / len(f1), evaluation["macro_f1"], abs_tol=1e-8):
        raise ValueError("혼동행렬과 보고된 Macro-F1이 다릅니다.")
    return counts


def benchmark_rows(klue_zip, ax_zip):
    with ZipFile(klue_zip) as klue, ZipFile(ax_zip) as ax:
        models = dict(zip(MODELS, [read_model(klue), read_model(ax)], strict=True))
        source = ax.read("comparison.json")
    report = json.loads(source)
    if report["validation_scope"] != "validation_without_training_overlap" or set(report["tasks"]) != set(TASKS):
        raise ValueError("원본 4개 업무의 학습 중복 제외 검증셋 보고서를 확인해 주세요.")
    if report["rows"] != sum(report["tasks"][task]["rows"] for task in TASKS):
        raise ValueError("업무별 검증 행 수와 전체 행 수가 다릅니다.")
    for model, (_, digest) in models.items():
        if report[f"{model}_model_sha256"] != digest:
            raise ValueError(f"{model} 가중치가 비교 보고서의 모델과 일치하지 않습니다.")
    source_hash = hashlib.sha256(source).hexdigest()
    rows = []
    for task in TASKS:
        section = report["tasks"][task]
        labels = section["label_order"]
        split = section["split_sha256"]["validation"]
        if not labels or len(set(labels)) != len(labels) or not re.fullmatch("[a-f0-9]{64}", split):
            raise ValueError(f"{task} 라벨 또는 검증셋 식별자가 올바르지 않습니다.")
        support = None
        for model, (configuration, digest) in models.items():
            if configuration[task] != {label: index for index, label in enumerate(labels)}:
                raise ValueError(f"{task} 라벨 순서가 {model} 설정과 다릅니다.")
            evaluation = section[model]
            counts = counts_from_matrix(evaluation, labels, section["rows"])
            current_support = [entry["n"] for entry in counts.values()]
            if support is not None and support != current_support:
                raise ValueError(f"{task} 검증셋의 두 모델별 정답 표본 수가 다릅니다.")
            support = current_support
            rows.append((task, model, digest, source_hash, split, section["rows"], evaluation["macro_f1"],
                         json.dumps(counts), json.dumps(labels), json.dumps(evaluation["confusion_matrix"])))
    return rows


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--company", type=UUID, required=True, help="평가 자료를 제공할 회사 UUID")
        parser.add_argument("--klue-zip", type=Path, required=True, help="원본 KLUE-BERT v1 ZIP")
        parser.add_argument("--ax-zip", type=Path, required=True, help="원본 A.X ZIP (comparison.json 포함)")

    def handle(self, *args, **options):
        company = options["company"]
        try:
            rows = benchmark_rows(options["klue_zip"], options["ax_zip"])
        except (BadZipFile, KeyError, OSError, TypeError, ValueError) as error:
            raise CommandError(f"원본 평가 자료를 읽을 수 없습니다. {error}") from error
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM public.company WHERE company_id=%s", (company,))
            if not cursor.fetchone():
                raise CommandError("회사 UUID를 확인해 주세요.")
            cursor.executemany("""
                INSERT INTO voc.model_review_benchmark
                    (company_id, task_code, model_code, model_sha256, report_sha256, split_sha256,
                     row_count, macro_f1, label_counts, label_order, confusion_matrix)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
                ON CONFLICT (company_id,task_code,model_code) DO UPDATE SET
                    model_sha256=excluded.model_sha256,report_sha256=excluded.report_sha256,
                    split_sha256=excluded.split_sha256,row_count=excluded.row_count,macro_f1=excluded.macro_f1,
                    label_counts=excluded.label_counts,label_order=excluded.label_order,confusion_matrix=excluded.confusion_matrix
                """, [(company, *row) for row in rows])
        self.stdout.write(f"{company} 회사의 원본 모델 평가 {len(rows)}개를 반영했습니다.")
