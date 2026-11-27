import argparse
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import mlflow
from mlflow.models import ModelSignature
from mlflow.types import ColSpec, Schema

from voc_models.features import MODEL_FIELDS, PREPROCESS_VERSION
from workers.classification.online_features import register

from .datasets import (
    canonical, checksum, database, exclude_evaluation, finalize_dataset, find_dataset,
    load_snapshot, select_labels,
)
from .progress import NO_PROGRESS, Progress


@contextmanager
def workflow_lock(cn, ref):
    cn.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (ref,))
    try:
        yield
    finally:
        cn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (ref,))


def run_record(cn, ref):
    row = cn.execute("SELECT * FROM voc.training_run WHERE workflow_ref=%s", (ref,)).fetchone()
    if row is None:
        raise ValueError("Workflow has not prepared its datasets")
    return row


def load_by_id(cn, dataset_uuid):
    item = cn.execute("SELECT * FROM voc.training_dataset WHERE dataset_uuid=%s", (dataset_uuid,)).fetchone()
    return item, load_snapshot(item)


def check_starting_model_history(cn, base_uri, company, eval_rows):
    while True:
        name, version = base_uri.removeprefix("models:/").rsplit("/", 1)
        parent = cn.execute("""
            SELECT r.base_model_uri,r.train_dataset_uuid
            FROM voc.model_version v JOIN voc.training_run r USING(workflow_ref)
            WHERE v.model_name=%s AND v.registry_version=%s
        """, (name, int(version))).fetchone()
        if parent is None:
            return
        dataset, rows = load_by_id(cn, parent["train_dataset_uuid"])
        if str(dataset["company_uuid"]) != str(company):
            raise ValueError("The starting model belongs to another company")
        if len(exclude_evaluation(rows, eval_rows)) != len(rows):
            raise ValueError("Evaluation examples overlap a starting model's training data")
        base_uri = parent["base_model_uri"]


def prepare(cn, args, progress=NO_PROGRESS):
    if (not re.fullmatch(r"models:/[^/]+/[1-9][0-9]*", args.base_model_uri)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", args.model_name)):
        raise ValueError("Use a fixed base Registry version and a valid model name")
    cutoff = datetime.fromisoformat(args.label_cutoff) if args.label_cutoff else None
    if cutoff is not None and cutoff.utcoffset() is None:
        raise ValueError("Gold cutoff must contain a timezone")
    with workflow_lock(cn, args.workflow_ref):
        existing = cn.execute("SELECT * FROM voc.training_run WHERE workflow_ref=%s", (args.workflow_ref,)).fetchone()
        with progress.stage("eval_read") as result:
            evaluation = find_dataset(cn, args.company, args.eval_dataset, "eval")
            eval_rows = load_snapshot(evaluation)
            result["paths"] = {"eval": evaluation["snapshot_ref"]}
        if existing:
            if (existing["eval_dataset_uuid"] != evaluation["dataset_uuid"]
                    or existing["base_model_uri"] != args.base_model_uri or existing["model_name"] != args.model_name
                    or existing["label_cutoff"] != cutoff):
                raise ValueError("Workflow inputs changed")
            trainer, _ = load_by_id(cn, existing["train_dataset_uuid"])
            if args.train_dataset and trainer["dataset_version"] != args.train_dataset:
                raise ValueError("Workflow training dataset changed")
            progress.write("dataset_save", "reused", paths={"train": trainer["snapshot_ref"]})
            return trainer["dataset_version"]
        check_starting_model_history(cn, args.base_model_uri, args.company, eval_rows)
        if args.train_dataset:
            trainer = find_dataset(cn, args.company, args.train_dataset, "train")
            train_rows = load_snapshot(trainer)
            if len(exclude_evaluation(train_rows, eval_rows)) != len(train_rows):
                raise ValueError("Selected training dataset overlaps the fixed evaluation dataset")
            progress.write("dataset_save", "reused", paths={"train": trainer["snapshot_ref"]})
        else:
            with progress.stage("postgres"):
                rows = exclude_evaluation(select_labels(cn, args.company, cutoff=cutoff), eval_rows)
                if not rows:
                    raise ValueError("No eligible dataset items")
            trainer = finalize_dataset(cn, args.company, "train", rows, progress)
        cn.execute("""
            INSERT INTO voc.training_run(workflow_ref,train_dataset_uuid,eval_dataset_uuid,base_model_uri,model_name,label_cutoff)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (args.workflow_ref, trainer["dataset_uuid"], evaluation["dataset_uuid"],
              args.base_model_uri, args.model_name, cutoff))
        return trainer["dataset_version"]


def train(cn, args, progress=NO_PROGRESS):
    with workflow_lock(cn, args.workflow_ref):
        record = run_record(cn, args.workflow_ref)
        if record["logged_model_uri"]:
            for stage in ("training_read", "model_load", "training", "evaluation", "model_save"):
                progress.write(stage, "reused")
            return record["logged_model_uri"]
        with progress.stage("training_read") as result:
            train_set, train_rows = load_by_id(cn, record["train_dataset_uuid"])
            eval_set, eval_rows = load_by_id(cn, record["eval_dataset_uuid"])
            result["paths"] = {"train": train_set["snapshot_ref"], "eval": eval_set["snapshot_ref"]}
        client = mlflow.MlflowClient()
        experiment = mlflow.set_experiment("voc-classification")
        run_id = record["mlflow_run_id"]
        if not run_id:
            previous = client.search_runs(
                [experiment.experiment_id], f"tags.workflow_ref = '{args.workflow_ref}'", max_results=1,
            )
            run_id = (previous[0] if previous else client.create_run(
                experiment.experiment_id, tags={"workflow_ref": args.workflow_ref},
            )).info.run_id
            cn.execute("UPDATE voc.training_run SET mlflow_run_id=%s WHERE workflow_ref=%s", (run_id, args.workflow_ref))

        config = json.loads(Path(args.domain_config).read_text(encoding="utf-8"))
        # These immutable parameters also reject changed settings on a resumed MLflow run.
        options = dict(epochs=args.epochs, batch_size=args.batch_size,
                       learning_rate=args.learning_rate, max_steps=args.max_steps)
        with tempfile.TemporaryDirectory() as folder, mlflow.start_run(run_id=run_id):
            output = Path(folder)
            mlflow.log_params({**options, "base_model_uri": record["base_model_uri"],
                "preprocess_version": PREPROCESS_VERSION, "input_features": ",".join(MODEL_FIELDS),
                "domain_config_sha256": checksum(canonical(config).encode())})
            if record["label_cutoff"] is not None:
                mlflow.log_param("label_cutoff", record["label_cutoff"].isoformat())
            for context, dataset in (("training", train_set), ("evaluation", eval_set)):
                mlflow.log_input(mlflow.data.from_pandas(
                    _metadata_frame(dataset), source=dataset["snapshot_ref"],
                    name=dataset["dataset_version"], digest=dataset["sha256"][:32],
                ), context=context)
            mlflow.log_dict({"training": _dataset_ref(train_set), "evaluation": _dataset_ref(eval_set),
                "features": list(MODEL_FIELDS), "preprocess_version": PREPROCESS_VERSION}, "datasets.json")
            from .model import fit

            metrics = fit(record["base_model_uri"], str(train_set["company_uuid"]),
                train_rows, eval_rows, config, output, progress=progress, **options)
            mlflow.log_metrics(metrics)
            here = Path(__file__).resolve().parent
            with progress.stage("model_save") as saved:
                logged = mlflow.pyfunc.log_model(
                    name="checkpoint", python_model=str(here / "mlflow_model.py"),
                    code_paths=[str(here.parent / "serving" / "voc_models")],
                    artifacts={"checkpoint": str(output)},
                    signature=ModelSignature(inputs=Schema([
                        ColSpec("string", "analysis_kind"), ColSpec("string", "domain_id"),
                        ColSpec("string", "body"), ColSpec("string", "title", required=False),
                        ColSpec("double", "unresolved_count", required=False),
                        ColSpec("double", "avg_first_reply_7d_seconds", required=False),
                    ])), pip_requirements=["mlflow==3.6.0", "torch==2.9.1", "transformers==4.57.3"],
                )
                cn.execute("""
                    UPDATE voc.training_run SET logged_model_uri=%s,sentiment_macro_f1=%s,category_macro_f1=%s
                    WHERE workflow_ref=%s
                """, (logged.model_uri, metrics["sentiment_macro_f1"], metrics["category_macro_f1"], args.workflow_ref))
                if logged.artifact_path.startswith("s3://"):
                    saved["paths"] = {"model": logged.artifact_path}
            return logged.model_uri


def _dataset_ref(dataset):
    return {
        "dataset_uuid": str(dataset["dataset_uuid"]),
        **{key: dataset[key] for key in ("dataset_version", "snapshot_ref", "sha256", "row_count")},
    }


def _metadata_frame(dataset):
    import pandas as pd

    return pd.DataFrame([{"dataset_uuid": str(dataset["dataset_uuid"]), "row_count": str(dataset["row_count"])}])


def register_candidate(cn, ref, progress=NO_PROGRESS):
    with workflow_lock(cn, ref), progress.stage("registry") as result:
        model = cn.execute("SELECT * FROM voc.model_version WHERE workflow_ref=%s", (ref,)).fetchone()
        if model:
            result["state"] = "reused"
            return f"models:/{model['model_name']}/{model['registry_version']}"
        run = run_record(cn, ref)
        if not run["logged_model_uri"] or run["sentiment_macro_f1"] is None or run["category_macro_f1"] is None:
            raise ValueError("Candidate has not completed training and evaluation")
        client = mlflow.MlflowClient()
        name = run["model_name"]
        prior = next((v for v in client.search_model_versions(f"name = '{name}'")
            if v.tags.get("workflow_ref") == ref or (
                v.run_id == run["mlflow_run_id"] and v.source == run["logged_model_uri"]
            )), None)
        tags = {"workflow_ref": ref, "base_model_uri": run["base_model_uri"],
                "input_features": ",".join(MODEL_FIELDS), "preprocess_version": PREPROCESS_VERSION}
        for purpose in ("train", "eval"):
            dataset = cn.execute("""
                SELECT dataset_version,snapshot_ref,company_uuid FROM voc.training_dataset WHERE dataset_uuid=%s
            """, (run[f"{purpose}_dataset_uuid"],)).fetchone()
            tags[f"{purpose}_dataset"] = dataset["dataset_version"]
            tags[f"{purpose}_snapshot_ref"] = dataset["snapshot_ref"]
            tags["company_uuid"] = str(dataset["company_uuid"])
        version = prior or mlflow.register_model(run["logged_model_uri"], name, tags=tags, await_registration_for=0)
        uri = f"models:/{name}/{version.version}"
        # Resolve the registered artifact before exposing a successful version in the DB.
        with tempfile.TemporaryDirectory() as folder:
            mlflow.pyfunc.load_model(uri, dst_path=folder)
        client.set_model_version_tag(name, version.version, "voc_training_input", "multitask")
        cn.execute("""
            INSERT INTO voc.model_version(workflow_ref,model_name,registry_version,artifact_ref)
            VALUES (%s,%s,%s,%s)
        """, (ref, name, int(version.version), uri))
        return uri


def trace(cn, name, version, feature_uuid):
    model = cn.execute("""
        SELECT v.*,r.mlflow_run_id,r.base_model_uri,r.train_dataset_uuid,r.eval_dataset_uuid
        FROM voc.model_version v JOIN voc.training_run r USING(workflow_ref)
        WHERE v.model_name=%s AND v.registry_version=%s
    """, (name, version)).fetchone()
    if model is None:
        raise ValueError("Unknown registered model version")
    lineage = {"model_uri": model["artifact_ref"], "run_id": model["mlflow_run_id"], "base": model["base_model_uri"]}
    found = False
    for purpose in ("train", "eval"):
        dataset, rows = load_by_id(cn, model[f"{purpose}_dataset_uuid"])
        lineage[purpose] = _dataset_ref(dataset)
        if feature_uuid:
            for row in rows:
                if row["feature_uuid"] == feature_uuid:
                    found = True
                    lineage[purpose]["source"] = {k: row[k] for k in (
                        "feature_uuid", "bronze_event_id", "silver_event_id", "silver_schema_version",
                        "body_version", "preprocess_version", "calculation_version", "calculation_sources",
                        "sentiment_label_uuid", "category_label_uuid", "seed_ref",
                    )}
                    break
    if feature_uuid and not found:
        raise ValueError("The feature does not belong to this model's datasets")
    return lineage


def main():
    app = argparse.ArgumentParser()
    commands = app.add_subparsers(dest="command", required=True)
    commands.add_parser("register-features")
    base = commands.add_parser("register-base")
    base.add_argument("--name", default="voc-bert-base")
    evaluation = commands.add_parser("freeze-eval")
    evaluation.add_argument("--company", required=True)
    evaluation.add_argument("--reviewed", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--workflow-ref", required=True)
    prep.add_argument("--company", required=True)
    prep.add_argument("--base-model-uri", required=True)
    prep.add_argument("--eval-dataset", required=True)
    prep.add_argument("--model-name", required=True)
    selection = prep.add_mutually_exclusive_group(required=True)
    selection.add_argument("--train-dataset")
    selection.add_argument("--label-cutoff")
    fit_cmd = commands.add_parser("train")
    fit_cmd.add_argument("--workflow-ref", required=True)
    fit_cmd.add_argument("--domain-config", required=True)
    fit_cmd.add_argument("--epochs", type=int, default=3)
    fit_cmd.add_argument("--batch-size", type=int, default=8)
    fit_cmd.add_argument("--learning-rate", type=float, default=2e-5)
    fit_cmd.add_argument("--max-steps", type=int, default=0)
    reg = commands.add_parser("register")
    reg.add_argument("--workflow-ref", required=True)
    lineage = commands.add_parser("trace")
    lineage.add_argument("--name", required=True)
    lineage.add_argument("--version", type=int, required=True)
    lineage.add_argument("--feature-uuid")
    args = app.parse_args()
    if hasattr(args, "workflow_ref") and not re.fullmatch(r"[A-Za-z0-9_.-]+", args.workflow_ref):
        app.error("Invalid workflow reference")
    if args.command == "register-features":
        register()
        return
    if args.command == "register-base":
        from .model import register_base

        if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.name):
            app.error("Invalid model name")
        print(register_base(args.name))
        return
    with database() as cn, Progress(getattr(args, "workflow_ref", None), os.getenv("ARGO_NODE_ID")) as progress:
        if args.command == "freeze-eval":
            reviewed = json.loads(Path(args.reviewed).read_text(encoding="utf-8"))
            rows = select_labels(cn, args.company, reviewed=reviewed)
            result = finalize_dataset(cn, args.company, "eval", rows)["dataset_version"]
        elif args.command == "prepare":
            result = prepare(cn, args, progress)
        elif args.command == "train":
            result = train(cn, args, progress)
        elif args.command == "register":
            result = register_candidate(cn, args.workflow_ref, progress)
        else:
            result = canonical(trace(cn, args.name, args.version, args.feature_uuid))
        print(result)


if __name__ == "__main__":
    main()
