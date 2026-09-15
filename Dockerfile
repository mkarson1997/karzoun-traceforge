FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system traceforge && adduser --system --ingroup traceforge traceforge

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip && python -m pip install .

USER traceforge
EXPOSE 4317 8080
ENTRYPOINT ["traceforge-gateway"]
