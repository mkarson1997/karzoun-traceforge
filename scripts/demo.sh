#!/usr/bin/env sh
set -eu

printf '%s\n' '[TraceForge] Building and starting the local pipeline...'
docker compose up -d --build

wait_url() {
  url="$1"
  attempts=0
  until curl --fail --silent --show-error "$url" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 40 ]; then
      printf 'Timed out waiting for %s\n' "$url" >&2
      exit 1
    fi
    sleep 1
  done
}

wait_url 'http://localhost:8080/readyz'
wait_url 'http://localhost:8081/healthz'

printf '%s\n' '[TraceForge] Sending synthetic coding-agent sessions with deliberate privacy test vectors...'
docker compose run --rm --no-deps privacy-gateway traceforge-demo --endpoint privacy-gateway:4317 --count 3

printf '%s\n' '[TraceForge] Demo ready: http://localhost:8081'
printf '%s\n' 'The synthetic email, prompt, source code and bearer token must not appear in persistent storage.'
