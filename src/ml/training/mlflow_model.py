import math
from pathlib import Path

import mlflow
from mlflow.pyfunc import PythonModel

from voc_models.features import NUMERIC_FIELDS
from voc_models.tickets import load_checkpoint


def optional(value):
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else value


class VocModel(PythonModel):
    def load_context(self, context):
        path = Path(context.artifacts["checkpoint"])
        self.predict_ticket = load_checkpoint(path, path / "domains.json")

    def predict(self, context, model_input, params=None):
        return [
            self.predict_ticket(
                row["analysis_kind"], row["domain_id"], row["body"], title=optional(row.get("title")),
                customer_features={name: optional(row.get(name)) for name in NUMERIC_FIELDS},
            )
            for row in model_input.to_dict("records")
        ]


mlflow.models.set_model(VocModel())
