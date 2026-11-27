import json
import os
from pathlib import Path
from uuid import UUID

from .features import join_text, numeric_vector


SENTIMENT_CODES = {"negative": "부정", "neutral": "중립", "positive": "긍정"}
DOMAIN_TASKS = {"commerce", "finance", "food_delivery"}


def read_domain_map(path, task_labels):
    """Load the team-provided mapping from domain UUID to model task and business label codes."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(entries, dict) or not entries:
        raise ValueError("MODEL_DOMAIN_MAP must contain at least one domain mapping")

    domains = {}
    for domain_id, entry in entries.items():
        normalized_id = str(UUID(domain_id))
        if normalized_id in domains or not isinstance(entry, dict):
            raise ValueError("Invalid or duplicate domain mapping")
        task = entry.get("task")
        labels = entry.get("labels")
        min_score = entry.get("min_score", 0.0)
        if task not in DOMAIN_TASKS or task not in task_labels:
            raise ValueError("Mapped domain task is not present in the checkpoint")
        if not isinstance(labels, dict) or set(labels) != set(task_labels[task]):
            raise ValueError("Map each checkpoint label to a configured business code or null")
        if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in labels.values()):
            raise ValueError("Mapped category must be a non-empty code or null")
        if type(min_score) not in (int, float) or not 0 <= min_score <= 1:
            raise ValueError("min_score must be a finite number from 0 to 1")
        domains[normalized_id] = task, labels, min_score
    return domains


def load():
    kinds = {item.strip() for item in os.environ.get("MODEL_ANALYSIS_KINDS", "sentiment,category").split(",")}
    return load_checkpoint(os.environ["MODEL_PATH"], os.environ.get("MODEL_DOMAIN_MAP"), kinds)


def load_checkpoint(model_path, domain_map, kinds=("sentiment", "category")):
    """Load a fine-tuned shared BERT checkpoint and return a stateless predictor."""
    import torch
    from transformers import AutoConfig, AutoTokenizer

    from .voc_multitask_bert import VocMultitaskBert

    model_path = Path(model_path)
    if not (model_path / "config.json").is_file():
        raise ValueError("MODEL_PATH must point to a saved local multitask model directory")

    config = AutoConfig.from_pretrained(model_path, local_files_only=True)
    if config.model_type == "modernbert":
        from .voc_multitask_ax import VocMultitaskAX
        model = VocMultitaskAX(model_path, config)
    else:
        model, loading = VocMultitaskBert.from_pretrained(
            model_path, local_files_only=True, use_safetensors=True, output_loading_info=True
        )
        if loading["missing_keys"] or loading["mismatched_keys"]:
            raise ValueError("Checkpoint has no complete set of trained model weights")
    if set(model.task_labels["sentiment"]) != set(SENTIMENT_CODES):
        raise ValueError("Checkpoint must support the existing three sentiment classes")
    model.to("cpu").eval()
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    domains = read_domain_map(domain_map, model.task_labels) if "category" in kinds else {}

    @torch.inference_mode()
    def predict(analysis_kind, domain_id, model_text, *, title=None, customer_features=None):
        if analysis_kind == "sentiment":
            task, labels, min_score = "sentiment", SENTIMENT_CODES, 0
        else:
            domain = domains.get(str(UUID(domain_id)))
            if domain is None:
                return {"label": None}
            task, labels, min_score = domain

        if model.uses_customer_features:
            if customer_features is None:
                raise ValueError("Customer features are required")
            model_text = join_text(title, model_text)
            customer_features = torch.tensor([numeric_vector(customer_features)])
        inputs = tokenizer(model_text, truncation=True, max_length=model.max_length, return_tensors="pt")
        logits = model(**inputs, task=task, customer_features=customer_features).logits
        score, label_id = logits.softmax(dim=-1)[0].max(dim=-1)
        score = score.item()
        label = labels[model.id_to_label[task][label_id.item()]]
        return {"label": label if score >= min_score else None, "score": score}

    predict.requires_customer_features = model.uses_customer_features
    return predict
