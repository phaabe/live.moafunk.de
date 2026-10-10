#!/bin/sh
# Runs inside the pinned nginx Alpine fixture, with no external network.
set -euo pipefail

# Begin with admission open. A rate-limited synthetic response lets us observe
# a request still owned by the old worker after the admission reload.
: > /tmp/admission-server.conf
dd if=/dev/zero of=/tmp/pre-reload-body bs=512 count=1 2>/dev/null
nginx -t -c /fixture/nginx.conf
nginx -c /fixture/nginx.conf
: > /tmp/early.headers
: > /tmp/early.body
curl --silent --show-error --max-time 20 --dump-header /tmp/early.headers \
    --output /tmp/early.body http://127.0.0.1:8080/ws/stream &
early_pid=$!
attempt=0
until grep -q '200 OK' /tmp/early.headers; do
    attempt=$((attempt + 1))
    [[ "$attempt" -lt 100 ]]
    kill -0 "$early_pid"
    sleep 0.05
done
cp /fixture/admission-server.conf /tmp/admission-server.conf
nginx -t -c /fixture/nginx.conf
nginx -s reload -c /fixture/nginx.conf
attempt=0
until [[ "$(curl --silent --show-error --max-time 3 --request POST \
    --output /dev/null --write-out '%{http_code}' \
    http://127.0.0.1:8080/api/recording/start)" == "503" ]]; do
    attempt=$((attempt + 1))
    [[ "$attempt" -lt 100 ]]
    sleep 0.05
done
kill -0 "$early_pid"
[[ "$(wc -c < /tmp/early.body)" -lt "$(wc -c < /tmp/pre-reload-body)" ]]
wait "$early_pid"
cmp /tmp/pre-reload-body /tmp/early.body
echo 'PASS: new admission refused while old request continued; old response drained'

checks=0
check() {
    expected="$1"
    method="$2"
    path="$3"
    actual=$(curl --silent --show-error --path-as-is --max-time 3 \
        --request "$method" --output /dev/null --write-out '%{http_code}' \
        "http://127.0.0.1:8080$path")
    if [[ "$actual" != "$expected" ]]; then
        echo "FAIL: $method $path: expected $expected, got $actual" >&2
        exit 1
    fi
    checks=$((checks + 1))
}

for path in /ws/stream '/ws/stream?test=true&force=true' /ws/stream-test \
    '/ws/recording/finalize?show_id=1' /metrics /api/internal/deploy-precheck \
    /api/new-job /ws/%73tream /api/../ws/stream; do
    check 503 GET "$path"
done
for path in /api/recording/start /api/my-show/go-live /api/my-show/confirm \
    /api/my-show/upload /api/my-show/upload/finalize/test /api/shows \
    /api/shows/1/recordings/reexport /api/shows/1/upload-recording/init \
    /api/submit/init /api/auth/change-password; do
    check 503 POST "$path"
done
check 503 PUT /api/shows/1
check 503 DELETE /api/my-show/upload
check 503 PATCH /api/shows/1
check 503 POST /api/shows/1/host
check 503 GET /api/stream/stop
check 503 POST /api/stream/stop/extra
check 503 POST /api/recording/status

for path in /health /api/auth/me /api/stream/status /api/stream/metrics \
    /api/recording/status /api/shows /api/shows/1 /api/shows/1/recordings \
    /api/my-show /api/my-shows /live.mp3 /test.mp3 /status-json.xsl; do
    check 204 GET "$path"
done
for path in /api/auth/login /api/auth/logout /api/stream/stop \
    /api/recording/stop /api/recording/marker; do
    check 204 POST "$path"
done

# Same advisory lock primitive as the runbook. A second descriptor cannot
# enter while the first owner keeps its descriptor open.
exec 9>/tmp/moafunk-deploy.lock
flock -n 9
if flock -n /tmp/moafunk-deploy.lock /bin/sh -c 'touch /tmp/second-deploy'; then
    echo 'FAIL: concurrent deploy acquired the lock' >&2
    exit 1
fi
[[ ! -e /tmp/second-deploy ]]
flock -u 9
flock -n /tmp/moafunk-deploy.lock /bin/sh -c 'touch /tmp/second-deploy'
[[ -e /tmp/second-deploy ]]

echo "PASS: $checks route checks; concurrent deploy refused; lock reusable"
nginx -s quit -c /fixture/nginx.conf
