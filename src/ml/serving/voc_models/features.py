import math

NUMERIC_FIELDS = ("unresolved_count", "avg_first_reply_7d_seconds")
MODEL_FIELDS = ("title", "body", *NUMERIC_FIELDS)
PREPROCESS_VERSION = "title-body-log1p-v1"


def join_text(title, body):
    return f"{title}\n{body}" if title and title.strip() else body


def numeric_vector(features):
    values = [features[name] for name in NUMERIC_FIELDS]
    if any(value is not None and (not math.isfinite(value) or value < 0) for value in values):
        raise ValueError("Invalid numeric feature")
    return [math.log1p(value or 0) for value in values] + [float(value is None) for value in values]
