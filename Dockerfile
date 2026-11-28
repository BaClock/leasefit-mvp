# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir -r requirements.txt && \
    useradd --create-home --uid 10001 leasefit && chown leasefit:leasefit /app && \
    install -d -o leasefit -g leasefit -m 0700 /var/lib/leasefit-metrapi
COPY --chown=leasefit:leasefit . .
USER leasefit
RUN sed -i 's/\r$//' scripts/start.sh && \
    SECRET_KEY=build-only-not-runtime DB_ENGINE=sqlite python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["sh", "scripts/start.sh"]
