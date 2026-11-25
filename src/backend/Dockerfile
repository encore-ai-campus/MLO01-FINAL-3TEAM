FROM node:22-slim AS followup-assets
WORKDIR /app/src/backend/frontend
COPY backend/frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY backend/frontend/ ./
COPY pages/design-preview/00-Shared/ /app/src/pages/design-preview/00-Shared/
COPY pages/design-preview/08-FollowUpPage/ /app/src/pages/design-preview/08-FollowUpPage/
RUN node build-followup.mjs

FROM python:3.12-slim
WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY --from=followup-assets /app/src/backend/.followup-assets/ ./.followup-assets/
RUN DJANGO_SECRET_KEY=build-only-collectstatic python manage.py collectstatic --noinput
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
