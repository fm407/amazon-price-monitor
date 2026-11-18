# syntax=docker/dockerfile:1
FROM python:3.14-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
COPY requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install -r /tmp/requirements.txt

# Test stage: `docker build --target test -t apm-test . && docker run --rm apm-test`
FROM builder AS test
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN /venv/bin/pip install -r /tmp/requirements-dev.txt
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY tests ./tests
ENV PATH=/venv/bin:$PATH
CMD ["pytest"]

FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/venv/bin:$PATH DATA_DIR=/app/data
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data && chown app:app /app/data
WORKDIR /app
COPY --from=builder /venv /venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
USER app
VOLUME ["/app/data"]
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=4).status == 200 else 1)"]
CMD ["python", "-m", "app"]
