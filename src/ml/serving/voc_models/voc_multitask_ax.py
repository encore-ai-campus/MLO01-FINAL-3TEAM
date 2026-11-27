from torch import nn
from safetensors.torch import load_model
from transformers import ModernBertModel
from transformers.modeling_outputs import SequenceClassifierOutput


class VocMultitaskAX(nn.Module):
    """Inference contract of the supplied A.X encoder with CLS-pooled task heads."""
    uses_customer_features = False

    def __init__(self, path, config):
        super().__init__()
        if config.architectures != ["VocMultitaskAX"] or getattr(config, "voc_uses_customer_features", False):
            raise ValueError("Unsupported A.X checkpoint")
        config.reference_compile = False
        config._attn_implementation = "sdpa"
        self.model = ModernBertModel(config)
        self.task_labels = config.voc_task_labels
        self.max_length = config.voc_max_length
        self.id_to_label = {task: {i: label for label, i in labels.items()} for task, labels in self.task_labels.items()}
        self.heads = nn.ModuleDict({task: nn.Linear(config.hidden_size, len(labels))
                                   for task, labels in self.task_labels.items()})
        load_model(self, str(path / "model.safetensors"), strict=True)

    def forward(self, input_ids, attention_mask, task, customer_features=None, token_type_ids=None):
        hidden = self.model(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        return SequenceClassifierOutput(logits=self.heads[task](hidden[:, 0]))
