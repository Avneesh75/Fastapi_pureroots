# PureRoots FastAPI service (shopping API at /api/v1).
# Build context is backend/ so the shared offer-pricing package can be installed:
#   docker build -f backend/api/Dockerfile backend
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt .
RUN pip install -r requirements.txt

# Shared offer/pricing maths used by both FastAPI and Django (single source of truth).
COPY shared/ /shared/
RUN pip install /shared/pureroots_pricing

COPY api/ .
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/media \
    && useradd --create-home --uid 1000 pureroot \
    && chown -R pureroot:pureroot /app
USER pureroot

EXPOSE 8001
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8001/api/v1/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--proxy-headers", "--forwarded-allow-ips", "*"]
