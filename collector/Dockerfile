FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY tools/channel_probe/requirements.txt /tmp/probe.txt
COPY collector/requirements.txt /tmp/collector.txt
RUN pip install --no-cache-dir -r /tmp/probe.txt -r /tmp/collector.txt
COPY tools/channel_probe/__main__.py tools/channel_probe/artifacts.py tools/channel_probe/normalize.py tools/channel_probe/providers.py tools/channel_probe/test_probe.py /app/tools/channel_probe/
COPY collector/__main__.py collector/events.py collector/pipeline.py collector/sources.py collector/test_collection.py collector/schema.sql /app/collector/
ENTRYPOINT ["python", "-m", "collector"]
