FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

RUN addgroup --system django \
    && adduser --system --ingroup django --home /app django

COPY --chown=django:django . .
RUN mkdir -p /app/staticfiles \
    && chown -R django:django /app/staticfiles

USER django

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl --fail --silent --max-time 4 --header "Host: school-display.com" --header "X-Forwarded-Proto: https" http://127.0.0.1:8000/health/ >/dev/null || exit 1

CMD ["gunicorn", "config.asgi:application", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--workers=2", "--timeout=120", "--keep-alive=5", "--error-logfile=-"]
