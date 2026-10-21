#!/usr/bin/env bash
# End-to-end check of the Docker backend: builds the image, starts the whole stack with
# `docker compose up`, then exercises it the way a user would. Used by
# .github/workflows/docker-e2e.yml; you can run it locally too (needs docker with compose,
# network access to build the image and fetch https://example.com, and free ports
# 8333 and 8765-8774 on 127.0.0.1).
#
# It uses its own compose project (a2kb-e2e) and env file (.env.e2e), so it neither reads nor
# changes the .env or the volumes of a real install. Everything is removed at the end.
#
#   scripts/docker-e2e.sh              # run and clean up
#   KB_E2E_KEEP=1 scripts/docker-e2e.sh   # leave the stack running for inspection

set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=a2kb-e2e
ENVFILE=.env.e2e
dc() { docker compose -p "$PROJECT" --env-file "$ENVFILE" "$@"; }
kbx() { dc exec -T kb "$@"; }
# Run a command in the kb container, show its output, and require a pattern in it. The
# output is captured first: in `cmd | grep -q`, grep exits at the first match, the writer
# gets SIGPIPE, and pipefail turns that into a failure even though the pattern was there.
expect_kb() {
    local pattern=$1 msg=$2 out rc=0
    shift 2
    out=$(kbx "$@" 2>&1) || rc=$?
    printf '%s\n' "$out"
    [ "$rc" -eq 0 ] || fail "$msg (exit $rc)"
    grep -q -- "$pattern" <<<"$out" || fail "$msg"
}
# The same for any text already captured (curl responses).
expect_in() { grep -q -- "$1" <<<"$3" || fail "$2"; }
step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "--- compose ps ---"; dc ps -a || true
        echo "--- logs ---"; dc logs --no-color --tail=200 || true
    fi
    if [ "${KB_E2E_KEEP:-}" != "1" ]; then
        dc --profile https down -v --remove-orphans >/dev/null 2>&1 || true
        rm -f "$ENVFILE"
    fi
    exit "$status"
}
trap cleanup EXIT

# The documented first step (`cp .env.example .env`), then the few values a test run needs.
# The groq key is a placeholder: nothing here calls an LLM, and doctor only checks it is set.
cp .env.example "$ENVFILE"
# Edit in place without `sed -i`, whose syntax differs between GNU (Linux) and BSD (macOS) sed.
set_env() { sed "$1" "$ENVFILE" > "$ENVFILE.tmp" && mv "$ENVFILE.tmp" "$ENVFILE"; }
set_env 's/^KB_NAMES=.*/KB_NAMES=research/; s/^KB_LLM=.*/KB_LLM=groq/'
printf 'KB_LLM_API_KEY=ci-placeholder-not-a-key\n' >> "$ENVFILE"

# State of a container: "<status> <exit code> <health>"
state() { docker inspect -f '{{.State.Status}} {{.State.ExitCode}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
    "$(dc ps -a -q "$1")"; }
wait_for() { # <service> <regex on state> <seconds>
    local svc=$1 want=$2 secs=$3 s=""
    for _ in $(seq 1 "$secs"); do
        s=$(state "$svc" 2>/dev/null || true)
        [[ "$s" =~ $want ]] && return 0
        sleep 1
    done
    fail "$svc: wanted state /$want/, last saw '$s'"
}

step "validate the compose files"
dc config -q
dc --profile https config -q
docker compose -p "$PROJECT" --env-file "$ENVFILE" \
    -f docker-compose.yml -f docker-compose.external-s3.yml config -q

step "docker compose up -d --build"
dc up -d --build
wait_for kb-init '^exited 0' 120
wait_for seaweedfs 'healthy$' 180
wait_for kb-provision '^exited 0' 240
wait_for kb 'running 0 (healthy|starting)' 60
wait_for kb-mcp 'healthy$' 120

# keys.json is the master copy (kb-init and kb-provision mount it); the services get derived files
kbinit() { dc run --rm --no-deps -T kb-init "$@"; }

step "generated secrets are private and every KB has its own scoped keys"
kbinit sh -c 'for f in /secrets/keys.json /s3conf/s3.json /connect/connect.json /mcpkeys/mcp.json; do
    [ "$(stat -c %a "$f")" = 600 ] || { echo "$f is not mode 600"; exit 1; }; done' \
    || fail "a secrets file is not mode 600"
kbinit python3 - <<'PY'
import json
d = json.load(open("/s3conf/s3.json"))
ids = {i["name"]: i["actions"] for i in d["identities"]}
assert set(ids) == {"admin", "research-writer", "research-obsidian"}, sorted(ids)
for name in ("research-writer", "research-obsidian"):
    assert all(a.endswith("/kbs/research/*") for a in ids[name]), ids[name]
print("identities ok")
PY

step "the kb and kb-mcp containers cannot read the S3 admin key"
ADMIN_SK=$(kbinit python3 -c 'import json;print(json.load(open("/secrets/keys.json"))["admin"]["secret_key"])')
[ -n "$ADMIN_SK" ] || fail "no admin key found"
for svc in kb kb-mcp; do
    dc exec -T "$svc" sh -c '! test -e /secrets/keys.json && ! test -e /s3conf/s3.json' \
        || fail "$svc can see keys.json or s3.json"
    dc exec -T "$svc" sh -c "grep -rqF -- '$ADMIN_SK' /secrets /s3conf /connect /mcpkeys /home/kb /data 2>/dev/null" \
        && fail "the admin secret is readable inside $svc"
done
dc exec -T kb-mcp sh -c 'touch /home/kb/x 2>/dev/null' && fail "kb-mcp can write its kb config"

step "a2kb connect obsidian (with the secret) and the keys it prints"
OBS=$(kbx a2kb connect obsidian research --show-secret)
printf '%s\n' "$OBS"
field() { printf '%s\n' "$OBS" | sed -n "s/^  $1  *//p" | head -1; }
ENDPOINT=$(field Endpoint); AK=$(field 'Access Key ID'); SK=$(field 'Secret Access Key')
BUCKET=$(field 'Bucket Name'); PREFIX=$(field 'Change The Remote Prefix' | awk '{print $1}')
[ "$ENDPOINT" = "http://127.0.0.1:8333" ] || fail "unexpected endpoint '$ENDPOINT'"
[ -n "$AK" ] && [ -n "$SK" ] && [ "$PREFIX" = "kbs/research/" ] || fail "incomplete settings"
expect_in 'phone' "no loopback warning" "$OBS"
kbx a2kb connect obsidian research | grep -q "$SK" && fail "secret printed without --show-secret"

step "the Obsidian key, from the host: list/put/get/delete inside its prefix, nothing outside"
# What Remotely Save does, through the published port (host network), signed like any S3 client.
docker run -i --rm --network host --entrypoint python3 \
    -e ENDPOINT="$ENDPOINT" -e AK="$AK" -e SK="$SK" -e BUCKET="$BUCKET" -e PREFIX="$PREFIX" \
    agent2kb:local - <<'PY'
import os

import boto3
import botocore.exceptions
from botocore.config import Config

c = boto3.client(
    "s3",
    endpoint_url=os.environ["ENDPOINT"],
    aws_access_key_id=os.environ["AK"],
    aws_secret_access_key=os.environ["SK"],
    region_name="us-east-1",
    config=Config(
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    ),
)
B, P = os.environ["BUCKET"], os.environ["PREFIX"]


def outcome(call):
    try:
        call()
        return "ok"
    except botocore.exceptions.ClientError as e:
        return e.response["Error"]["Code"]


key = P + "Clippings/e2e.md"
assert outcome(lambda: c.list_objects_v2(Bucket=B, Prefix=P)) == "ok", "list inside the prefix"
assert outcome(lambda: c.put_object(Bucket=B, Key=key, Body=b"# e2e")) == "ok", "put"
assert c.get_object(Bucket=B, Key=key)["Body"].read() == b"# e2e", "get"
assert outcome(lambda: c.delete_object(Bucket=B, Key=key)) == "ok", "delete"
other = outcome(lambda: c.put_object(Bucket=B, Key="kbs/other/x.md", Body=b"x"))
assert other == "AccessDenied", f"put outside the prefix: {other}"
whole = outcome(lambda: c.list_objects_v2(Bucket=B))
assert whole == "AccessDenied", f"whole-bucket list: {whole}"
print("obsidian key scoping ok")
PY

step "rotating the Obsidian key: the old one dies when SeaweedFS restarts, the new one works"
kbinit a2kb bootstrap rotate research --role obsidian
dc restart seaweedfs
wait_for seaweedfs 'healthy$' 180
NEW=$(kbx a2kb connect obsidian research --show-secret)
NAK=$(printf '%s\n' "$NEW" | sed -n 's/^  Access Key ID  *//p' | head -1)
NSK=$(printf '%s\n' "$NEW" | sed -n 's/^  Secret Access Key  *//p' | head -1)
[ -n "$NAK" ] && [ "$NAK" != "$AK" ] || fail "a2kb connect did not show a new key"
probe() { # <access key> <secret>: prints ok or the S3 error code of a listing inside the prefix
    docker run -i --rm --network host --entrypoint python3 \
        -e ENDPOINT="$ENDPOINT" -e AK="$1" -e SK="$2" -e BUCKET="$BUCKET" -e PREFIX="$PREFIX" \
        agent2kb:local - <<'PY'
import os

import boto3
import botocore.exceptions
from botocore.config import Config

c = boto3.client(
    "s3", endpoint_url=os.environ["ENDPOINT"], aws_access_key_id=os.environ["AK"],
    aws_secret_access_key=os.environ["SK"], region_name="us-east-1",
    config=Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required",
                  response_checksum_validation="when_required"),
)
try:
    c.list_objects_v2(Bucket=os.environ["BUCKET"], Prefix=os.environ["PREFIX"])
    print("ok")
except botocore.exceptions.ClientError as e:
    print(e.response["Error"]["Code"])
PY
}
[ "$(probe "$NAK" "$NSK")" = ok ] || fail "the new Obsidian key does not work"
[ "$(probe "$AK" "$SK")" = InvalidAccessKeyId ] || fail "the old Obsidian key still works"

step "a2kb doctor and ingest inside the container"
kbx a2kb -p research doctor --read-only
kbx a2kb -p research ingest --url https://example.com
expect_kb 'Raw files' "status" a2kb -p research status

step "the generated crontab is valid and names no model"
kbx cat /home/kb/.kb/crontab.generated
kbx supercronic -test /home/kb/.kb/crontab.generated
kbx grep -qiE 'claude|gpt|haiku|sonnet|llama' /home/kb/.kb/crontab.generated && fail "model named"

step "MCP over HTTP with the generated token"
AGENT=$(kbx a2kb connect agent research --show-secret)
printf '%s\n' "$AGENT" | head -4
TOKEN=$(printf '%s\n' "$AGENT" | grep -o "Bearer [^']*" | head -1 | cut -d' ' -f2)
URL=$(printf '%s\n' "$AGENT" | grep -o 'http://127.0.0.1:[0-9]*/mcp' | head -1)
[ -n "$TOKEN" ] && [ -n "$URL" ] || fail "a2kb connect agent printed no url/token"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$URL")" = 401 ] || fail "MCP answered without a token"
H=(-H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json'
   -H 'Accept: application/json, text/event-stream')
SID=$(curl -s -D - -o /dev/null -X POST "$URL" "${H[@]}" -d \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"e2e","version":"1"}}}' \
    | tr -d '\r' | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')
[ -n "$SID" ] || fail "no MCP session"
curl -s -o /dev/null -X POST "$URL" "${H[@]}" -H "mcp-session-id: $SID" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
call() { curl -s -X POST "$URL" "${H[@]}" -H "mcp-session-id: $SID" -d "$1"; }
RESP=$(call '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search","arguments":{"query":"knowledge base"}}}')
printf '%s\n' "$RESP" >&2
expect_in 'wiki/index.md' "search found nothing" "$RESP"
RESP=$(call '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_recent","arguments":{"kind":"raw"}}}')
expect_in 'Example-Domain' "the ingested page is not visible over MCP" "$RESP"

step "adding a KB: edit KB_NAMES, docker compose up -d (no restart by hand)"
BEFORE_AK=$NAK
set_env 's/^KB_NAMES=.*/KB_NAMES=research work/'
dc up -d
wait_for kb-provision '^exited 0' 240
wait_for seaweedfs 'healthy$' 180
wait_for kb 'running 0 (healthy|starting)' 120
expect_kb work "KB 'work' was not created" a2kb list
kbx a2kb -p work doctor --read-only
kbx a2kb -p work ingest --url https://example.com
AK_AFTER=$(kbx a2kb connect obsidian research --show-secret | sed -n 's/^  Access Key ID  *//p')
[ "$AK_AFTER" = "$BEFORE_AK" ] || fail "the research key changed"
expect_kb 'Raw files' "research lost its data" a2kb -p research status
# the kb container was recreated by `up` and generated its crontab again at start
kbx grep -q '/data/work work' /home/kb/.kb/crontab.generated || fail "crontab lacks the new KB"

step "a second up changes nothing"
dc up -d
wait_for kb-provision '^exited 0' 240
[ "$(kbx a2kb connect obsidian research --show-secret | sed -n 's/^  Access Key ID  *//p')" = "$BEFORE_AK" ] \
    || fail "keys changed on a repeated up"

step "the HTTPS Caddyfile is valid"
dc run --rm --no-deps -e DOMAIN=kb.example.com -e ACME_EMAIL=ops@example.com kb-init
dc --profile https run --rm --no-deps --entrypoint caddy caddy \
    validate --config /proxy/Caddyfile --adapter caddyfile

printf '\nALL CHECKS PASSED\n'
