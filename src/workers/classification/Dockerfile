FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker.py online_features.py schema.sql verify-schema.sql ./
USER 65532:65532
ENTRYPOINT ["python", "worker.py"]
CMD ["run"]
