"""One KLUE-BERT encoder with independent sentiment and VOC classification heads."""

import torch
from torch import nn
from torch.nn import functional as F
from transformers import BertConfig, BertModel, BertPreTrainedModel
from transformers.modeling_outputs import SequenceClassifierOutput


TASK_ORDER = ("sentiment", "commerce", "finance", "food_delivery")


class VocMultitaskBert(BertPreTrainedModel):
    """Train individual heads from their own labels and share one BERT forward pass."""

    def __init__(self, config):
        super().__init__(config)
        self.task_labels = getattr(config, "voc_task_labels", None)
        self.task_order = tuple(getattr(config, "voc_task_order", TASK_ORDER))
        if (
            not isinstance(self.task_labels, dict)
            or "sentiment" not in self.task_labels
            or len(self.task_order) != len(self.task_labels)
            or set(self.task_order) != set(self.task_labels)
        ):
            raise ValueError("모델 설정의 작업 순서와 감성·도메인 라벨 정의가 일치해야 합니다.")

        self.id_to_label = {}
        for task in self.task_order:
            labels = self.task_labels[task]
            if (
                not isinstance(labels, dict)
                or not labels
                or any(
                    not isinstance(name, str) or not name or type(number) is not int
                    for name, number in labels.items()
                )
                or sorted(labels.values()) != list(range(len(labels)))
            ):
                raise ValueError(f"{task} 모델 라벨 번호는 0부터 연속해야 합니다.")
            self.id_to_label[task] = {number: name for name, number in labels.items()}

        self.max_length = getattr(config, "voc_max_length", 128)
        self._validate_max_length(self.max_length)
        self.max_labels = max(len(labels) for labels in self.task_labels.values())
        self.bert = BertModel(config)
        dropout = config.classifier_dropout
        self.dropout = nn.Dropout(config.hidden_dropout_prob if dropout is None else dropout)
        self.heads = nn.ModuleDict({
            task: nn.Linear(config.hidden_size, len(self.task_labels[task]))
            for task in self.task_order
        })
        self.post_init()

    @classmethod
    def from_base_pretrained(cls, path, task_labels, max_length=128):
        """Load local BERT weights and initialize the new, untrained VOC heads."""
        config = BertConfig.from_pretrained(path, local_files_only=True)
        config.voc_task_labels = task_labels
        config.voc_task_order = list(task_labels)
        config.voc_max_length = max_length
        model, loading_info = cls.from_pretrained(
            path, config=config, local_files_only=True, output_loading_info=True,
        )
        missing_bert = [
            key for key in loading_info["missing_keys"] if key.startswith("bert.")
        ]
        if missing_bert:
            raise ValueError(f"기본 KLUE-BERT 가중치가 일부 누락되었습니다. {missing_bert[:5]}")
        return model

    def _validate_max_length(self, value):
        if type(value) is not int or not 1 <= value <= self.config.max_position_embeddings:
            raise ValueError("최대 입력 길이는 모델이 지원하는 위치 임베딩 범위의 정수여야 합니다.")

    def _encode(self, input_ids, attention_mask=None, token_type_ids=None):
        return self.bert(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            return_dict=True,
        ).pooler_output

    def forward(
        self,
        input_ids,
        attention_mask=None,
        token_type_ids=None,
        task_ids=None,
        labels=None,
        loss_weights=None,
        task=None,
    ):
        """Use a uniform logit shape for batches containing examples from multiple tasks."""
        if (task is None) == (task_ids is None):
            raise ValueError("배치의 작업 이름 또는 행별 작업 번호를 지정해야 합니다.")

        encoded = self.dropout(self._encode(input_ids, attention_mask, token_type_ids))
        if task is not None:
            if task not in self.heads:
                raise ValueError(f"지원하지 않는 분류 작업입니다. {task}")
            logits = self.heads[task](encoded)
        else:
            if task_ids.shape != (len(encoded),) or task_ids.dtype != torch.long:
                raise ValueError("작업 번호는 입력 행마다 정수 한 개가 필요합니다.")
            outputs = []
            for name in self.task_order:
                scores = self.heads[name](encoded)
                missing_labels = self.max_labels - scores.shape[-1]
                scores = F.pad(scores, (0, missing_labels), value=torch.finfo(scores.dtype).min)
                outputs.append(scores)
            all_logits = torch.stack(outputs)
            selection = task_ids.reshape(1, -1, 1).expand(1, -1, self.max_labels)
            logits = all_logits.gather(0, selection).squeeze(0)

        loss = None
        if labels is not None:
            losses = F.cross_entropy(logits, labels, reduction="none")
            if loss_weights is not None:
                if loss_weights.shape != losses.shape:
                    raise ValueError("손실 가중치는 입력 행마다 하나씩 필요합니다.")
                losses = losses * loss_weights
            loss = losses.mean()
        return SequenceClassifierOutput(loss=loss, logits=logits)

    @torch.inference_mode()
    def classify(self, tokenizer, text, domain, max_length=None):
        """Return sentiment and the supplied domain's issue from one encoding."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("분류할 VOC 본문을 입력해야 합니다.")
        if not isinstance(domain, str) or domain == "sentiment" or domain not in self.heads:
            raise ValueError(f"지원하지 않는 세부유형 도메인입니다. {domain}")

        length = self.max_length if max_length is None else max_length
        self._validate_max_length(length)
        self.eval()
        tokens = tokenizer(text, truncation=True, max_length=length, return_tensors="pt")
        encoded = self._encode(**tokens.to(self.device))

        def prediction(task):
            score, label_id = self.heads[task](encoded).softmax(dim=-1)[0].max(dim=-1)
            return {"label": self.id_to_label[task][label_id.item()], "score": score.item()}

        return {
            "sentiment": prediction("sentiment"),
            "issue": prediction(domain),
        }
