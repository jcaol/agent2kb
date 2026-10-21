# What was tested: the Docker backend

This is the detailed test record behind [docker.md](docker.md). It says what was run where, what
was not, and which parts only CI exercises.

Where this was written there is no Docker daemon, so compose itself could not run. The image
and the stack's services were built and run with rootless buildah and podman instead, one
container per compose service, on the host network (`--net host --pid host`,
`--add-host seaweedfs:127.0.0.1`, ports moved to 18xxx/19xxx and bound to 127.0.0.1, throwaway
named volumes, no host paths mounted, no LLM login). Everything below passed in that setup:

| Step | Result |
|---|---|
| `buildah bud` of this `Dockerfile` | builds; `a2kb --version`, `claude --version` (2.1.302), `codex --version` (0.162.1), `supercronic -version`, ffmpeg, pandoc, Python imports, ownership of `/data`, `/secrets`, `/proxy` |
| `a2kb bootstrap init` | admin + `research-writer` + `research-obsidian` + MCP token generated; `keys.json` and `s3.json` mode 0600; a second run generates nothing and changes nothing |
| SeaweedFS 4.48 (the pinned image) with `s3.json` | starts (its entrypoint drops to uid 1000, which can read the 0600 file); `a2kb bootstrap provision`: bucket, config, `research` KB, writer key accepted |
| `kb` service | `supercronic -test` accepts the generated crontab, which names no model; `kb-cron-backup.sh` runs inside the container |
| Split secrets ([who can read what](docker.md#who-can-read-which-secret)) | in the `kb` and `kb-mcp` containers `/secrets` and `/s3conf` are empty, `cat /secrets/keys.json` fails, and the admin secret is not found in any readable volume, home, data directory or environment; `kb-mcp` cannot write its (read-only) kb home and still serves `list_recent`; the kb config holds only the two writer keys |
| `a2kb connect obsidian research` | prints the settings; secret hidden without `--show-secret`; loopback warning |
| `a2kb -p research doctor --read-only`, `a2kb bootstrap health` | no blocking problems, exit 0 |
| `a2kb -p research ingest --url https://example.com` | stored under `kbs/research/raw/` |
| `kb-mcp` service | `a2kb connect agent` prints URL and token (with `--show-secret`); 401 without or with a wrong token; `initialize`, `search` and `list_recent` over HTTP with the token |
| Adding a KB (`KB_NAMES=research work`) | `init` adds only `work-*` keys and a new MCP port; research keys and token byte-identical; `provision` before restarting SeaweedFS stops with the restart instruction; after the restart `work` is ready, data of `research` intact |
| The Obsidian key, with boto3 from the host | list, put, get, delete inside `kbs/research/` work; `kbs/work/` list/get/put and a whole-bucket list give `AccessDenied` |
| Writer rotation | `rotate` prints the three commands; after the SeaweedFS restart the running `kb-mcp` answers `bad_credentials`; after `kb-provision` the kb container works with the new key; after restarting `kb-mcp` it serves again |
| `a2kb bootstrap health` | with SeaweedFS stopped it exits 1 and prints the failed check (`Storage / endpoint reachable ...`); exits 0 once S3 is back |
| CopyObject / UploadPartCopy matrix ([security notes](docker.md#security-notes)) | same requests on 4.18 (bypass reproduced) and on 4.48 (all refused) |
| Rotate / revoke ([section](docker.md#rotating-or-revoking-keys)) | rotating the Obsidian key: the old key still works until SeaweedFS restarts, then `InvalidAccessKeyId`; `a2kb connect obsidian` shows the new key and the new key works. Revoking: denied after the restart, `connect` refuses and says to rotate, `init` prints a note. Rotating the writer key: the kb config holds a dead key until `provision` stores the new one, then `a2kb status` works. A KB dropped from `KB_NAMES` gets the warning and keeps working until `revoke`; after revoke + restart both keys are denied; listing it again issues fresh keys that work |
| `weed shell s3.configure` | adds an identity to the running server without a restart; it survives a restart next to the file's identities (not used here, see [docker.md](docker.md#seaweedfs-and-new-identities)) |
| Generated Caddyfile | `caddy validate` (2.11.7) says "Valid configuration" |
| Remotely Save settings and prefix handling | read from the plugin's source (`en.json`, `settings.ts`, `fsS3.ts`, release 0.5.25 at the time), replayed with boto3 against SeaweedFS |
| Compose files | `docker compose config -q` for the default, `https` and external-S3 variants (the Docker CLI renders them without a daemon); YAML tests |
| Python logic | `tests/test_bootstrap.py`, `tests/test_connect.py` (mock S3, no network) |

Two limitations of that test environment, neither of which is a property of the image: the
build needs `--security-opt seccomp=unconfined` (buildah's default filter makes Node's
`io_uring_setup` return an invalid file descriptor), and `podman stop` does not return with
`--pid host`, so containers were ended with `podman kill`.

**Only CI runs** (`scripts/docker-e2e.sh`): `docker compose up` itself (dependency order,
`service_completed_successfully`, health checks, volume initialisation and ownership, published
ports, the compose network names such as `seaweedfs` and `kb-mcp`), the recreate-on-`KB_NAMES`
behaviour, and `caddy validate` inside compose. Never exercised anywhere: certificate issuance
with a real domain, Obsidian itself (a real Remotely Save sync), Claude Code adding the printed
MCP command, the `claude` and `codex` logins inside the container, and the external-S3 mode
against a real provider.
