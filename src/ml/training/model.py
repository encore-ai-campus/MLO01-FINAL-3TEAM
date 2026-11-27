import json
import math
import tempfile
from pathlib import Path

import mlflow
import torch
from huggingface_hub import ModelCard, snapshot_download
from sklearn.metrics import f1_score
from transformers import AutoTokenizer, BertModel

from .progress import NO_PROGRESS

from voc_models.features import MODEL_FIELDS, PREPROCESS_VERSION, join_text, numeric_vector
from voc_models.tickets import DOMAIN_TASKS, SENTIMENT_CODES
from voc_models.voc_multitask_bert import VocMultitaskBert

BASE_SOURCE = "klue/bert-base"
BASE_REVISION = "77c8b3d707df785034b4e50f2da5d37be5f0f546"
BASE_FILES = (
    "config.json", "model.safetensors", "special_tokens_map.json",
    "tokenizer.json", "tokenizer_config.json", "vocab.txt", "README.md",
)


def register_base(name):
    client = mlflow.MlflowClient()
    for version in client.search_model_versions(f"name = '{name}'"):
        if version.tags.get("source_revision") == BASE_REVISION:
            client.set_model_version_tag(name, version.version, "voc_training_input", "encoder")
            return version.version
    folder = snapshot_download(BASE_SOURCE, revision=BASE_REVISION, allow_patterns=list(BASE_FILES))
    model, details = BertModel.from_pretrained(
        folder, local_files_only=True, use_safetensors=True, output_loading_info=True,
    )
    if details["missing_keys"] or details["mismatched_keys"]:
        raise ValueError("Base encoder has incomplete weights")
    tokenizer = AutoTokenizer.from_pretrained(folder, local_files_only=True)
    mlflow.set_experiment("voc-pretrained-models")
    with mlflow.start_run(tags={"source_model": BASE_SOURCE, "source_revision": BASE_REVISION}):
        logged = mlflow.transformers.log_model(
            {"model": model, "tokenizer": tokenizer},
            name="encoder", task="feature-extraction", save_pretrained=True,
            model_card=ModelCard.load(Path(folder) / "README.md"),
            pip_requirements=["mlflow==3.6.0", "torch==2.9.1", "transformers==4.57.3"],
        )
        version = mlflow.register_model(logged.model_uri, name, tags={
            "voc_training_input": "encoder", "source_revision": BASE_REVISION,
        })
        return version.version


def label_maps(config):
    task_labels = {"sentiment": {code: index for index, code in enumerate(SENTIMENT_CODES)}}
    reverse_sentiment = {value: code for code, value in SENTIMENT_CODES.items()}
    grouped = {}
    if not config:
        raise ValueError("At least one domain mapping is required")
    for entry in config.values():
        task, categories = entry["task"], entry["categories"]
        if (task not in DOMAIN_TASKS or not categories or
                any(not isinstance(code, str) or not code.strip() for code in categories.values()) or
                len(set(categories.values())) != len(categories)):
            raise ValueError("Each domain requires a category UUID to business code mapping")
        grouped.setdefault(task, set()).update(categories.values())
    for task, labels in sorted(grouped.items()):
        task_labels[task] = {label: index for index, label in enumerate(sorted(labels))}
    domain_map = {}
    for domain, entry in config.items():
        task = entry["task"]
        codes = set(entry["categories"].values())
        domain_map[domain] = {
            "task": task, "labels": {name: name if name in codes else None for name in task_labels[task]},
        }
    return task_labels, reverse_sentiment, domain_map


def examples(rows, config, task_labels, reverse_sentiment):
    result = []
    for row in rows:
        domain = row["domain_uuid"]
        route = config.get(domain)
        if route is None:
            raise ValueError("Missing an approved label map for a dataset domain")
        if row["sentiment_label_uuid"]:
            code = reverse_sentiment[row["sentiment_value"]]
            result.append((row, "sentiment", task_labels["sentiment"][code]))
        if row["category_label_uuid"]:
            task = route["task"]
            code = route["categories"][row["category_uuid"]]
            result.append((row, task, task_labels[task][code]))
    if not any(task == "sentiment" for _, task, _ in result) or not any(
        task != "sentiment" for _, task, _ in result
    ):
        raise ValueError("Dataset needs at least one label for each prediction kind")
    return result


def batches(items, tokenizer, model, batch_size, indices):
    for start in range(0, len(indices), batch_size):
        batch = [items[index] for index in indices[start:start + batch_size]]
        tags = [task for _, task, _ in batch]
        texts = [join_text(row["title"], row["body"]) for row, _, _ in batch]
        inputs = tokenizer(texts, max_length=model.max_length, truncation=True, padding=True, return_tensors="pt")
        labels = [label for _, _, label in batch]
        inputs["task_ids"] = torch.tensor([model.task_order.index(task) for task in tags])
        inputs["customer_features"] = torch.tensor([numeric_vector(row) for row, _, _ in batch], dtype=torch.float32)
        yield inputs, tags, labels


def evaluate(model, tokenizer, items, batch_size):
    expected = {"sentiment": [], "category": []}
    predicted = {"sentiment": [], "category": []}
    model.eval()
    offset = 0
    with torch.inference_mode():
        for inputs, tasks, labels in batches(items, tokenizer, model, batch_size, list(range(len(items)))):
            predictions = model(**inputs).logits.argmax(dim=-1).tolist()
            for position, (task, actual, prediction) in enumerate(zip(tasks, labels, predictions)):
                kind = "sentiment" if task == "sentiment" else "category"
                row = items[offset + position][0]
                prefix = "" if task == "sentiment" else row["domain_uuid"]
                expected[kind].append(f"{prefix}:{actual}")
                predicted[kind].append(f"{prefix}:{prediction}")
            offset += len(labels)
    return {
        f"{kind}_macro_f1": float(f1_score(expected[kind], predicted[kind], average="macro", zero_division=0))
        for kind in expected
    }


def load_starting_model(base_uri, company, task_labels):
    name, version = base_uri.removeprefix("models:/").rsplit("/", 1)
    entry = mlflow.MlflowClient().get_model_version(name, version)
    kind = entry.tags.get("voc_training_input")
    if entry.status != "READY":
        raise ValueError("The Registry model version must be ready")
    if kind == "multitask" and entry.tags.get("company_uuid") == company:
        with tempfile.TemporaryDirectory() as folder:
            artifact = mlflow.artifacts.download_artifacts(artifact_uri=base_uri, dst_path=folder)
            flavor = mlflow.models.Model.load(artifact).flavors["python_function"]
            checkpoint = Path(artifact) / flavor["artifacts"]["checkpoint"]["path"]
            inputs = json.loads((checkpoint / "input_features.json").read_text(encoding="utf-8"))
            if inputs != {"features": list(MODEL_FIELDS), "preprocess_version": PREPROCESS_VERSION}:
                raise ValueError("The starting model requires a different feature schema")
            model, details = VocMultitaskBert.from_pretrained(
                checkpoint, local_files_only=True, use_safetensors=True, output_loading_info=True,
            )
            if (not model.uses_customer_features or model.task_labels != task_labels or
                    details["missing_keys"] or details["mismatched_keys"]):
                raise ValueError("The starting model requires the same features and label mapping")
            tokenizer = AutoTokenizer.from_pretrained(checkpoint, local_files_only=True)
        return model, tokenizer
    if kind != "encoder" or entry.tags.get("source_revision") != BASE_REVISION:
        raise ValueError("The Registry version is not an approved starting model for this company")
    with tempfile.TemporaryDirectory() as folder:
        components = mlflow.transformers.load_model(base_uri, dst_path=folder, return_type="components")
        components["model"].save_pretrained(folder, safe_serialization=True)
        model = VocMultitaskBert.from_base_pretrained(folder, task_labels, uses_customer_features=True)
    return model, components["tokenizer"]


def fit(base_uri, company, train_rows, evaluation_rows, config, output, *, epochs, batch_size, learning_rate, max_steps, progress=NO_PROGRESS):
    if epochs < 1 or batch_size < 1 or not math.isfinite(learning_rate) or learning_rate <= 0 or max_steps < 0:
        raise ValueError("Invalid training parameters")
    torch.manual_seed(42)
    task_labels, reverse_sentiment, domain_map = label_maps(config)
    train = examples(train_rows, config, task_labels, reverse_sentiment)
    validation = examples(evaluation_rows, config, task_labels, reverse_sentiment)
    with progress.stage("model_load"):
        model, tokenizer = load_starting_model(base_uri, company, task_labels)

    with progress.stage("training"):
        optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
        steps = 0
        model.train()
        for _ in range(epochs):
            for inputs, _, labels in batches(train, tokenizer, model, batch_size, torch.randperm(len(train)).tolist()):
                optimizer.zero_grad(set_to_none=True)
                loss = model(**inputs, labels=torch.tensor(labels)).loss
                if not torch.isfinite(loss):
                    raise RuntimeError("Training loss is not finite")
                loss.backward()
                optimizer.step()
                steps += 1
                if max_steps and steps >= max_steps:
                    break
            if max_steps and steps >= max_steps:
                break
        del optimizer
    with progress.stage("evaluation"):
        metrics = evaluate(model, tokenizer, validation, batch_size)
    model.save_pretrained(output, safe_serialization=True)
    tokenizer.save_pretrained(output)
    (output / "domains.json").write_text(json.dumps(domain_map, ensure_ascii=False), encoding="utf-8")
    (output / "input_features.json").write_text(json.dumps({
        "features": MODEL_FIELDS, "preprocess_version": PREPROCESS_VERSION,
    }), encoding="utf-8")
    return metrics
