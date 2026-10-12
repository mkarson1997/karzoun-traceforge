FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system traceforge \
    && adduser --system --ingroup traceforge traceforge \
    && mkdir -p /var/lib/traceforge \
    && chown -R traceforge:traceforge /var/lib/traceforge

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip && python -m pip install .

USER traceforge
EXPOSE 4317 4320 8080 8081
CMD ["traceforge-gateway"]
