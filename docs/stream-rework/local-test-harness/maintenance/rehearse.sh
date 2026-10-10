#!/bin/sh
# Runs inside the pinned nginx Alpine fixture, with no external network.
set -euo pipefail

nginx -t -c /fixture/nginx.conf
nginx -c /fixture/nginx.conf

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
