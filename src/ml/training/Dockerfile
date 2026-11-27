FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONPATH=/app/src:/app/src/ml/serving PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY src/ml/training/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.9.1 \
    && pip install --no-cache-dir -r /tmp/requirements.txt
COPY src/ml/training/ /app/src/ml/training/
COPY src/ml/serving/voc_models/ /app/src/ml/serving/voc_models/
COPY src/workers/classification/online_features.py /app/src/workers/classification/online_features.py
USER 10001:10001
ENTRYPOINT ["python", "-m", "ml.training.workflow"]
