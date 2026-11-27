FROM python:3.12-slim
WORKDIR /app/src
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY infra/channel_probe/requirements.txt /tmp/probe.txt
COPY collector/requirements.txt /tmp/collector.txt
RUN pip install --no-cache-dir -r /tmp/probe.txt -r /tmp/collector.txt
COPY infra/channel_probe/__main__.py infra/channel_probe/artifacts.py infra/channel_probe/normalize.py infra/channel_probe/providers.py infra/channel_probe/test_probe.py /app/src/infra/channel_probe/
COPY collector/__main__.py collector/events.py collector/pipeline.py collector/silver.py collector/sources.py collector/spool.py collector/test_collection.py collector/schema.sql /app/src/collector/
COPY collector/ticket_schema.sql collector/training_schema.sql collector/channel_intake_schema.sql collector/settings_schema.sql collector/followup_schema.sql /app/src/collector/
COPY collector/retraining_schema.sql collector/model_policy_schema.sql /app/src/collector/
COPY collector/schema-order.txt /app/src/collector/
COPY workers/classification/schema.sql /app/src/workers/classification/
ENTRYPOINT ["python", "-m", "collector"]
