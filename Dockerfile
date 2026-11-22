# syntax=docker/dockerfile:1
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 flow
COPY pyproject.toml README.md ./
COPY src ./src
# Optional extra CA (corporate/egress proxies): docker build --secret id=extra_ca,src=<ca.pem> .
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --no-cache-dir .
COPY config ./config
COPY data ./data
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV FLOW_DB_PATH=/home/flow/var/flow.db FLOW_HOST=0.0.0.0 PORT=8000
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
# Web app: browser call demo, dashboard, admin and the Retell tool endpoints.
# Entrypoint drops root after fixing volume ownership. The v0.1 triage API: `make api`.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["flow", "serve"]
