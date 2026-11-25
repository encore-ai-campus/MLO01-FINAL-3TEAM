FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir confluent-kafka==2.16.2
COPY exporter.py .
ENTRYPOINT ["python", "exporter.py"]
