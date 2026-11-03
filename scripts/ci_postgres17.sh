#!/usr/bin/env bash
# Disposable PostgreSQL 17 server for CI only. Mnerys itself runs natively.
set -euo pipefail

if [[ -z "${GITHUB_ACTIONS:-}" ]]; then
  printf '%s\n' 'This harness is restricted to ephemeral GitHub Actions runners.' >&2
  exit 1
fi

name="mnerys-pg17-ci-${GITHUB_RUN_ID:-test}-${GITHUB_RUN_ATTEMPT:-1}"
secret="$(openssl rand -hex 24)"
printf '::add-mask::%s\n' "$secret"

cleanup() {
  docker rm --force "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Bind to loopback only; discover it locally, never hardcode server addresses.
loopback="$(getent ahostsv4 localhost | awk 'NR == 1 {print $1}')"
if [[ -z "$loopback" ]]; then
  printf '%s\n' 'Unable to resolve local loopback address.' >&2
  exit 1
fi

docker run --detach --rm --name "$name" \
  --publish "${loopback}::5432" \
  --tmpfs /var/lib/postgresql/data:rw,size=384m \
  --env POSTGRES_DB=mnerys_test_ci \
  --env POSTGRES_USER=mnerys_test \
  --env "POSTGRES_PASSWORD=$secret" \
  postgres:17-alpine >/dev/null

port="$(docker port "$name" 5432/tcp | head -n 1 | awk -F: '{print $NF}')"
if ! [[ "$port" =~ ^[0-9]+$ ]]; then
  printf '%s\n' 'Could not resolve isolated PostgreSQL test port.' >&2
  exit 1
fi

ready=0
for _ in {1..60}; do
  if docker exec "$name" pg_isready -U mnerys_test -d mnerys_test_ci >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  printf '%s\n' 'PostgreSQL 17 test server did not become ready.' >&2
  exit 1
fi

# No credentials are written to disk or printed into the workflow logs.
export MNERYS_TEST_POSTGRES_URL="postgresql+psycopg2://mnerys_test:${secret}@localhost:${port}/mnerys_test_ci"
export ODYSSEUS_DATA_DIR="$RUNNER_TEMP/mnerys-pg17-tests"
python -m pytest -q tests/test_sqlite_postgres_migration.py tests/test_postgresql_support.py
