FROM python:3.12-slim
WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
RUN DJANGO_SECRET_KEY=build-only-collectstatic python manage.py collectstatic --noinput
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
