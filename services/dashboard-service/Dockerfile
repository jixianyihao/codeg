# AresClaw dashboard service — one image, two entrypoints.
#   control:  uvicorn dashboard_service.app:create_control_app_from_env --factory
#   content:  python -m dashboard_service serve-content
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard_service ./dashboard_service
COPY web ./web
COPY alembic.ini ./
COPY alembic ./alembic

# Migrations run as an explicit step (`alembic upgrade head`), never at
# worker startup; the app refuses to serve on an unmigrated schema.
ENV DASHBOARD_STORAGE_DIR=/data/staging
RUN mkdir -p /data/staging

EXPOSE 8080 8081
CMD ["python", "-m", "dashboard_service", "serve-control"]
