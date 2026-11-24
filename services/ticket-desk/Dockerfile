FROM node:22-alpine AS frontend
WORKDIR /app/React
COPY React/package*.json ./
RUN npm ci --no-audit --no-fund
COPY React/index.html React/vite.config.js ./
COPY src /app/src
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY src/pages/01-LoginPage/backend/requirements.txt /tmp/api-requirements.txt
RUN pip install --no-cache-dir -r /tmp/api-requirements.txt
COPY src/pages/01-LoginPage/backend /app/src/pages/01-LoginPage/backend
COPY services/ticket-desk/serve.py /app/serve.py
COPY --from=frontend /app/React/dist /app/static
COPY src/pages/03-TicketListPage/03-TicketListPage.html /app/static/src/pages/03-TicketListPage/
COPY src/pages/04-TicketDetailPage/04-TicketDetailPage.html /app/static/src/pages/04-TicketDetailPage/
CMD ["uvicorn", "serve:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
