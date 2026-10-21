"""Backend bootstrap for the Docker setup: ``a2kb bootstrap ...`` (hidden admin commands).

``docker compose up`` runs these in order, every time, so each step is idempotent:

``init``       before SeaweedFS starts: generate the S3 identities (an admin key, and per KB a
               ``<kb>-writer`` key for agent2kb and a ``<kb>-obsidian`` key for the Obsidian
               plugin, each scoped to ``kbs/<kb>/*``), the MCP port and token per KB, and the
               Caddyfile when a domain is set. Keys are random, written with mode 0600 into the
               secrets volume, and never regenerated or overwritten.
``provision``  once S3 answers: create the bucket and the v2 config, then each KB in
               ``KB_NAMES`` through the same code as ``a2kb new`` (existing KBs untouched).
``crontab``    write the supercronic crontab for the KBs in the config.
``mcp-serve``  one ``a2kb mcp --http`` process per KB in ``KB_MCP_KBS``, supervised.

Everything here takes its settings from environment variables so the compose file stays the
only place that names them. See docs/docker.md.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

from agent2kb import config_v2
from agent2kb.config_v2 import ConfigError

# keys.json (with the admin key) lives in one volume that only kb-init and kb-provision mount.
# Everything else gets a derived copy with just what it needs, in its own volume:
#   s3.json       -> SeaweedFS (all identities, no way to tell them apart from the server's view)
#   connect.json  -> the kb service, for `a2kb connect` (Obsidian keys and MCP tokens, no admin)
#   mcp.json      -> the kb-mcp service (MCP ports and tokens only)
SECRETS_ENV = "KB_SECRETS_DIR"
S3CONF_ENV = "KB_S3CONF_DIR"
CONNECT_ENV = "KB_CONNECT_DIR"
MCPKEYS_ENV = "KB_MCPKEYS_DIR"
PROXY_ENV = "KB_PROXY_DIR"
DEFAULT_SECRETS_DIR = "/secrets"
DEFAULT_S3CONF_DIR = "/s3conf"
DEFAULT_CONNECT_DIR = "/connect"
DEFAULT_MCPKEYS_DIR = "/mcpkeys"
DEFAULT_PROXY_DIR = "/proxy"
KEYS_FILE = "keys.json"
S3_FILE = "s3.json"
CONNECT_FILE = "connect.json"
MCP_FILE = "mcp.json"

ROLES = ("writer", "obsidian")

DEFAULT_BUCKET = "kb-data"
DEFAULT_REGION = "us-east-1"
#: Where the kb container reaches the bundled SeaweedFS (compose service name).
INTERNAL_ENDPOINT = "http://seaweedfs:8333"
DEFAULT_S3_PORT = 8333
#: One MCP server per KB on consecutive ports; compose publishes this whole range.
MCP_PORT_BASE = 8765
MCP_PORT_COUNT = 10
DEFAULT_KB_NAMES = "research"

_NAME_SPLIT = re.compile(r"[,\s]+")
# \Z, not $: "$" also matches before a trailing newline, which would reach the Caddyfile.
_DOMAIN_RE = re.compile(r"(?=.{4,253}\Z)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\Z")
_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_UPPER_DIGITS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


class BootstrapError(Exception):
    """A bootstrap step cannot continue; the message says what to change."""


# ─── settings from the environment ──────────────────────────────────


def secrets_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get(SECRETS_ENV) or DEFAULT_SECRETS_DIR)


def connect_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get(CONNECT_ENV) or DEFAULT_CONNECT_DIR)


def mcpkeys_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get(MCPKEYS_ENV) or DEFAULT_MCPKEYS_DIR)


@dataclass
class ViewDirs:
    """Where the derived files go; all in one directory unless the volumes are split."""

    s3: Path
    connect: Path
    mcp: Path

    @classmethod
    def together(cls, directory: Path) -> "ViewDirs":
        return cls(directory, directory, directory)


def view_dirs(env: Mapping[str, str]) -> ViewDirs:
    return ViewDirs(
        Path(env.get(S3CONF_ENV) or DEFAULT_S3CONF_DIR), connect_dir(env), mcpkeys_dir(env)
    )


def proxy_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get(PROXY_ENV) or DEFAULT_PROXY_DIR)


def is_external(env: Mapping[str, str]) -> bool:
    return (env.get("KB_S3_MODE") or "bundled").strip().lower() == "external"


def bucket_name(env: Mapping[str, str]) -> str:
    from agent2kb.setup_wizard import validate_bucket_name

    bucket = (env.get("KB_BUCKET_NAME") or DEFAULT_BUCKET).strip()
    problem = validate_bucket_name(bucket)
    if problem:
        raise BootstrapError(f"KB_BUCKET_NAME '{bucket}' {problem}.")
    return bucket


def parse_names(value: Optional[str], *, what: str = "KB_NAMES") -> list[str]:
    """``research, work`` -> ``['research', 'work']``: validated like ``a2kb new``, no repeats."""
    names: list[str] = []
    for part in _NAME_SPLIT.split((value or "").strip()):
        if not part:
            continue
        try:
            config_v2.validate_kb_name(part, strict=True)
        except ConfigError as e:
            raise BootstrapError(f"{what}: {e}") from None
        if part not in names:
            names.append(part)
    if not names:
        raise BootstrapError(f"{what} is empty: name at least one KB (e.g. research).")
    return names


def kb_names(env: Mapping[str, str]) -> list[str]:
    return parse_names(env.get("KB_NAMES") or DEFAULT_KB_NAMES)


def mcp_names(env: Mapping[str, str], names: list[str]) -> list[str]:
    """KBs served over MCP: ``KB_MCP_KBS``, default every KB; ``none`` turns MCP off."""
    value = (env.get("KB_MCP_KBS") or "").strip()
    if not value:
        return list(names)
    if value.lower() in ("none", "-"):
        return []
    return parse_names(value, what="KB_MCP_KBS")


def mcp_port_base(env: Mapping[str, str]) -> int:
    """First MCP port. Compose publishes 8765-8774, so only change it without compose (tests)."""
    value = (env.get("KB_MCP_PORT_BASE") or "").strip()
    if not value:
        return MCP_PORT_BASE
    if not value.isdigit() or not 1024 <= int(value) <= 65535 - MCP_PORT_COUNT:
        raise BootstrapError(f"KB_MCP_PORT_BASE '{value}' is not a port between 1024 and 65525.")
    return int(value)


def domain_of(env: Mapping[str, str]) -> str:
    domain = (env.get("DOMAIN") or "").strip().lower()
    if domain and not _DOMAIN_RE.fullmatch(domain):
        raise BootstrapError(f"DOMAIN '{domain}' is not a host name like kb.example.com.")
    return domain


# ─── secrets files ──────────────────────────────────────────────────


def _write_private(path: Path, text: str) -> None:
    """Atomic write, mode 0600 from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    os.chmod(path, 0o600)


def _write_if_changed(path: Path, text: str) -> bool:
    if path.is_file() and path.read_text() == text:
        os.chmod(path, 0o600)
        return False
    _write_private(path, text)
    return True


def load_keys(path: Path) -> dict:
    """The keys file, or ``{}`` if there is none yet. A damaged file is an error, never reset."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise BootstrapError(
            f"{path} is not valid JSON ({e}). Not touching it: restore it from a backup. "
            "Deleting it would issue new keys and lock out every existing client."
        ) from None
    if not isinstance(data, dict):
        raise BootstrapError(f"{path} must hold a JSON object. Not touching it.")
    return data


def _access_key() -> str:
    return "KB" + "".join(secrets.choice(_UPPER_DIGITS) for _ in range(18))


def _secret_key() -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(40))


def _credential() -> dict:
    return {"access_key": _access_key(), "secret_key": _secret_key()}


# ─── identities (before SeaweedFS starts) ───────────────────────────


def identity_actions(bucket: str, prefix: str, *, tagging: bool = True) -> list[str]:
    """SeaweedFS actions limited to one prefix: ``Read:<bucket>/<prefix>*`` and so on."""
    verbs = ["Read", "Write", "List"] + (["Tagging"] if tagging else [])
    return [f"{verb}:{bucket}/{prefix}*" for verb in verbs]


def render_connect_json(keys: dict) -> dict:
    """What `a2kb connect` needs: Obsidian keys (or their revoked markers) and MCP tokens."""
    return {
        "version": 1,
        "bucket": keys.get("bucket"),
        "kbs": {
            name: {"obsidian": entry["obsidian"]}
            for name, entry in keys.get("kbs", {}).items()
            if "obsidian" in entry
        },
        "mcp": keys.get("mcp", {}),
    }


def render_mcp_json(keys: dict) -> dict:
    return {"version": 1, "mcp": keys.get("mcp", {})}


def write_views(keys: dict, views: ViewDirs, *, external: bool) -> bool:
    """Write the files derived from ``keys`` (each atomic); True when s3.json changed.

    keys.json is always written first and these are re-derived on every ``init``, so a crash
    in between is healed by the next run: the derived files never know a key the master lacks.
    """

    def dump(doc: dict) -> str:
        return json.dumps(doc, indent=2) + "\n"

    _write_if_changed(views.connect / CONNECT_FILE, dump(render_connect_json(keys)))
    _write_if_changed(views.mcp / MCP_FILE, dump(render_mcp_json(keys)))
    if external:
        return False
    return _write_if_changed(views.s3 / S3_FILE, dump(render_s3_json(keys)))


def is_revoked(cred: Mapping) -> bool:
    return bool(cred.get("revoked_at"))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kb_prefix(name: str) -> str:
    return f"kbs/{name}/"


def render_s3_json(keys: dict) -> dict:
    """The SeaweedFS ``-s3.config`` document for the keys file.

    The admin identity creates the bucket (only ``a2kb bootstrap`` uses it). agent2kb signs as
    ``<kb>-writer``; the Obsidian plugin as ``<kb>-obsidian`` (read, write and list, no
    tagging). Both are limited to ``kbs/<kb>/``.
    """
    bucket = keys["bucket"]
    admin = keys["admin"]
    identities = [
        {
            "name": "admin",
            "credentials": [{"accessKey": admin["access_key"], "secretKey": admin["secret_key"]}],
            "actions": ["Admin", "Read", "Write", "List", "Tagging"],
        }
    ]
    for name, entry in keys.get("kbs", {}).items():
        for role, tagging in (("writer", True), ("obsidian", False)):
            cred = entry[role]
            if is_revoked(cred):
                continue  # kept in keys.json as a marker, never answered by S3
            identities.append(
                {
                    "name": f"{name}-{role}",
                    "credentials": [
                        {"accessKey": cred["access_key"], "secretKey": cred["secret_key"]}
                    ],
                    "actions": identity_actions(bucket, kb_prefix(name), tagging=tagging),
                }
            )
    return {"identities": identities}


@dataclass
class InitResult:
    keys: dict
    added: list[str] = field(default_factory=list)  # what was newly generated
    s3_json_changed: bool = False
    #: KBs with live credentials that are not in ``names`` (never revoked automatically)
    unlisted: list[str] = field(default_factory=list)
    #: ``<kb>-<role>`` of listed KBs whose credential is revoked and not yet re-issued
    revoked_in_use: list[str] = field(default_factory=list)


def ensure_identities(
    sdir: Path,
    names: list[str],
    mcp: list[str],
    *,
    bucket: str,
    external: bool = False,
    port_base: int = MCP_PORT_BASE,
    views: Optional[ViewDirs] = None,
) -> InitResult:
    """Create what is missing in ``keys.json`` (and the s3.json derived from it).

    Existing keys and tokens are never replaced, KBs missing from ``names`` keep theirs
    (removing a name does not revoke anything), and a changed bucket name is refused: the
    bucket is baked into the scoped identities.
    """
    keys_path = sdir / KEYS_FILE
    keys = load_keys(keys_path)
    added: list[str] = []
    old = json.dumps(keys, sort_keys=True)

    if keys.get("bucket") not in (None, bucket):
        raise BootstrapError(
            f"KB_BUCKET_NAME is '{bucket}' but the identities in {keys_path} were issued for "
            f"bucket '{keys['bucket']}'. Set KB_BUCKET_NAME back, or start over with new keys "
            f"(delete {keys_path}; existing clients must then be "
            "reconfigured, and the data stays in the old bucket)."
        )
    keys.setdefault("version", 1)
    keys.setdefault("bucket", bucket)
    kbs = keys.setdefault("kbs", {})
    if not external:
        if "admin" not in keys:
            keys["admin"] = _credential()
            added.append("admin")
        for name in names:
            entry = kbs.setdefault(name, {})
            if entry and all(r in entry and is_revoked(entry[r]) for r in ROLES):
                entry.clear()  # a fully revoked KB that is listed again starts over
            for role in ROLES:
                if role not in entry:
                    entry[role] = _credential()
                    added.append(f"{name}-{role}")

    tokens = keys.setdefault("mcp", {})
    taken = {int(v["port"]) for v in tokens.values()}
    for name in mcp:
        if name in tokens:
            continue
        free = [p for p in range(port_base, port_base + MCP_PORT_COUNT) if p not in taken]
        if not free:
            raise BootstrapError(
                f"At most {MCP_PORT_COUNT} KBs can be served over MCP (ports "
                f"{port_base}-{port_base + MCP_PORT_COUNT - 1}); trim KB_MCP_KBS."
            )
        tokens[name] = {"port": free[0], "token": secrets.token_urlsafe(32)}
        taken.add(free[0])
        added.append(f"{name}-mcp")

    if json.dumps(keys, sort_keys=True) != old:
        _write_private(keys_path, json.dumps(keys, indent=2) + "\n")
    changed = write_views(keys, views or ViewDirs.together(sdir), external=external)
    unlisted: list[str] = []
    revoked_in_use: list[str] = []
    if not external:
        unlisted = [
            n
            for n, e in kbs.items()
            if n not in names and any(r in e and not is_revoked(e[r]) for r in ROLES)
        ]
        revoked_in_use = [
            f"{n}-{r}" for n in names for r in ROLES if is_revoked(kbs.get(n, {}).get(r, {}))
        ]
    return InitResult(
        keys=keys,
        added=added,
        s3_json_changed=changed,
        unlisted=unlisted,
        revoked_in_use=revoked_in_use,
    )


# ─── revoke / rotate ────────────────────────────────────────────────

_RESTART_S3 = "docker compose restart seaweedfs"


@dataclass
class KeyChange:
    kb: str
    roles: list[str]
    s3_json_changed: bool = False
    noop: bool = False


def _load_for_change(sdir: Path, kb: str, external: bool) -> tuple[dict, Path]:
    if external:
        raise BootstrapError(
            "KB_S3_MODE=external: the S3 keys are managed at your storage provider, not here."
        )
    path = sdir / KEYS_FILE
    keys = load_keys(path)
    if kb not in (keys.get("kbs") or {}):
        known = ", ".join(sorted(keys.get("kbs") or {})) or "none"
        raise BootstrapError(f"No credentials for KB '{kb}' in {path} (known: {known}).")
    return keys, path


def _save_keys(sdir: Path, keys: dict, views: Optional[ViewDirs]) -> bool:
    _write_private(sdir / KEYS_FILE, json.dumps(keys, indent=2) + "\n")
    return write_views(keys, views or ViewDirs.together(sdir), external=False)


def _roles(role: Optional[str]) -> list[str]:
    if role is None:
        return list(ROLES)
    if role not in ROLES:
        raise BootstrapError(f"--role must be one of: {', '.join(ROLES)}.")
    return [role]


def revoke_credentials(
    sdir: Path,
    kb: str,
    role: Optional[str] = None,
    *,
    listed: list[str],
    external: bool = False,
    views: Optional[ViewDirs] = None,
) -> KeyChange:
    """Disable a KB's key(s): marked in ``keys.json`` (no secret kept), gone from ``s3.json``.

    A listed KB's writer key is what agent2kb itself uses, so that one (and a whole-KB revoke)
    needs the KB out of KB_NAMES first; ``rotate`` replaces a key instead.
    """
    keys, _ = _load_for_change(sdir, kb, external)
    roles = _roles(role)
    if kb in listed and (role is None or role == "writer"):
        raise BootstrapError(
            f"KB '{kb}' is still in KB_NAMES, and its writer key is the one agent2kb uses. Replace "
            f"keys with `a2kb bootstrap rotate {kb}`, revoke only the Obsidian key with "
            f"`--role obsidian`, or remove '{kb}' from KB_NAMES first."
        )
    entry = keys["kbs"][kb]
    todo = [r for r in roles if r in entry and not is_revoked(entry[r])]
    if not todo:
        return KeyChange(kb, [], noop=True)
    for r in todo:
        entry[r] = {"access_key": entry[r]["access_key"], "revoked_at": _now()}
    return KeyChange(kb, todo, s3_json_changed=_save_keys(sdir, keys, views))


def rotate_credentials(
    sdir: Path,
    kb: str,
    role: Optional[str] = None,
    *,
    external: bool = False,
    views: Optional[ViewDirs] = None,
) -> KeyChange:
    """New random key for ``writer`` and/or ``obsidian`` (or the MCP token with ``mcp``).

    The old key stops working once SeaweedFS reloads ``s3.json``; a revoked key is replaced
    the same way.
    """
    keys, _ = _load_for_change(sdir, kb, external)
    if role == "mcp":
        entry = (keys.get("mcp") or {}).get(kb)
        if not entry:
            raise BootstrapError(f"KB '{kb}' has no MCP token (it is not served over MCP).")
        entry["token"] = secrets.token_urlsafe(32)
        _write_private(sdir / KEYS_FILE, json.dumps(keys, indent=2) + "\n")
        write_views(keys, views or ViewDirs.together(sdir), external=False)
        return KeyChange(kb, ["mcp"])
    roles = _roles(role)
    for r in roles:
        keys["kbs"][kb][r] = _credential()
    return KeyChange(kb, roles, s3_json_changed=_save_keys(sdir, keys, views))


def run_revoke(
    env: Mapping[str, str], kb: str, role: Optional[str], say: Callable[[str], None] = print
) -> KeyChange:
    change = revoke_credentials(
        secrets_dir(env),
        kb,
        role,
        listed=kb_names(env),
        external=is_external(env),
        views=view_dirs(env),
    )
    if change.noop:
        say(f"Nothing to do: the key(s) of '{kb}' are already revoked.")
        return change
    say(f"Revoked: {', '.join(f'{kb}-{r}' for r in change.roles)} (marked in keys.json).")
    say(f"SeaweedFS reads its identities only at start: run `{_RESTART_S3}` to apply it.")
    if "obsidian" in change.roles:
        say(
            f"Obsidian devices using it stop syncing after that. For a new key: "
            f"`a2kb bootstrap rotate {kb} --role obsidian`."
        )
    return change


def run_rotate(
    env: Mapping[str, str], kb: str, role: Optional[str], say: Callable[[str], None] = print
) -> KeyChange:
    change = rotate_credentials(
        secrets_dir(env), kb, role, external=is_external(env), views=view_dirs(env)
    )
    what = [f"{kb} MCP token" if r == "mcp" else f"{kb}-{r}" for r in change.roles]
    say(f"Rotated: {', '.join(what)}.")
    steps = []
    if change.s3_json_changed:
        steps.append(
            f"`{_RESTART_S3}`: SeaweedFS reads its keys only at start, so the old key "
            "stops working then"
        )
    if "writer" in change.roles:
        steps.append(
            "`docker compose run --rm kb-provision`: stores the new writer key in the KB config "
            "(the cron jobs use it from their next run)"
        )
    if "writer" in change.roles or "mcp" in change.roles:
        steps.append("`docker compose restart kb-mcp`: it reads its key or token only at start")
    if steps:
        say("To apply it, in this order:")
    for n, step in enumerate(steps, 1):
        say(f"  {n}. {step}")
    if "obsidian" in change.roles:
        say(
            f"Then show the new Obsidian key: `docker compose exec kb a2kb connect obsidian {kb} "
            "--show-secret`, and enter it in Remotely Save on every device."
        )
    return change


# ─── public addresses (what clients outside Docker use) ─────────────

_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def is_loopback_host(host: str) -> bool:
    host = host.strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    return is_loopback_host(urlsplit(url).hostname or "")


def public_host(env: Mapping[str, str]) -> str:
    return (env.get("KB_PUBLIC_HOST") or "127.0.0.1").strip()


def public_s3_endpoint(env: Mapping[str, str]) -> str:
    """The S3 URL for devices outside Docker: ``https://s3.<DOMAIN>`` or ``http://host:port``."""
    explicit = (env.get("KB_PUBLIC_S3_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    domain = domain_of(env)
    if domain:
        return f"https://s3.{domain}"
    port = (env.get("KB_S3_PORT") or str(DEFAULT_S3_PORT)).strip()
    host = public_host(env)
    shown = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{shown}:{port}"


def public_mcp_url(env: Mapping[str, str], kb: str, port: int) -> str:
    """The MCP URL of one KB: ``https://mcp.<DOMAIN>/<kb>/mcp`` or ``http://host:<port>/mcp``."""
    explicit = (env.get("KB_PUBLIC_MCP_URL") or "").strip().rstrip("/")
    if explicit:
        return f"{explicit}/{kb}/mcp"
    domain = domain_of(env)
    if domain:
        return f"https://mcp.{domain}/{kb}/mcp"
    host = public_host(env)
    shown = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{shown}:{port}/mcp"


# ─── Caddyfile ──────────────────────────────────────────────────────


def render_caddyfile(
    domain: str, email: str, mcp: Mapping[str, int], *, bundled_s3: bool = True
) -> str:
    """Caddyfile: ``s3.<domain>`` -> SeaweedFS, ``mcp.<domain>/<kb>/...`` -> that KB's server.

    Caddy keeps the client's ``Host`` header (the S3 signature and the MCP host guard both
    need it) and obtains certificates by itself.
    """
    if not _DOMAIN_RE.fullmatch(domain):
        raise BootstrapError(f"DOMAIN '{domain}' is not a host name like kb.example.com.")
    if email and not _EMAIL_RE.fullmatch(email):
        raise BootstrapError(f"ACME_EMAIL '{email}' is not an e-mail address.")
    lines = ["# Generated by `a2kb bootstrap init`; rewritten on every `docker compose up`."]
    if email:
        lines += ["{", f"\temail {email}", "}"]
    if bundled_s3:
        lines += ["", f"s3.{domain} {{", "\treverse_proxy seaweedfs:8333", "}"]
    lines += ["", f"mcp.{domain} {{"]
    for name, port in mcp.items():
        lines += [
            f"\thandle_path /{name}/* {{",
            f"\t\treverse_proxy kb-mcp:{port} {{",
            "\t\t\tflush_interval -1",
            "\t\t}",
            "\t}",
        ]
    lines += ['\trespond "not found: use /<kb>/mcp" 404', "}", ""]
    return "\n".join(lines)


# ─── init ───────────────────────────────────────────────────────────


def run_init(env: Mapping[str, str], say: Callable[[str], None] = print) -> InitResult:
    """``a2kb bootstrap init``: identities, MCP tokens, and the Caddyfile when DOMAIN is set."""
    names = kb_names(env)
    mcp = mcp_names(env, names)
    external = is_external(env)
    result = ensure_identities(
        secrets_dir(env),
        names,
        mcp,
        bucket=bucket_name(env),
        external=external,
        port_base=mcp_port_base(env),
        views=view_dirs(env),
    )
    if result.added:
        say(f"Generated: {', '.join(result.added)} (in {secrets_dir(env)}, mode 0600).")
    else:
        say("Identities already present; nothing generated.")
    if not external:
        say(
            "SeaweedFS identities "
            + (
                "updated: it must (re)start to load them."
                if result.s3_json_changed
                else "unchanged."
            )
        )
    for name in result.unlisted:
        say(
            f"WARNING: '{name}' is not in KB_NAMES but its keys still work (nothing is revoked "
            f"automatically, so its data stays reachable). If it is gone for good: "
            f"docker compose run --rm kb-init a2kb bootstrap revoke {name}  (then {_RESTART_S3})"
        )
    for item in result.revoked_in_use:
        kb, _, role = item.rpartition("-")
        say(
            f"NOTE: the {role} key of '{kb}' is revoked. For a new one: "
            f"docker compose run --rm kb-init a2kb bootstrap rotate {kb} --role {role}"
        )
    domain = domain_of(env)
    out = proxy_dir(env) / "Caddyfile"
    if domain:
        ports = {n: int(result.keys["mcp"][n]["port"]) for n in mcp}
        text = render_caddyfile(
            domain, (env.get("ACME_EMAIL") or "").strip(), ports, bundled_s3=not external
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        say(f"Wrote {out} for s3.{domain} and mcp.{domain}.")
    elif out.exists():
        out.unlink()
    return result


# ─── provision (after SeaweedFS answers) ────────────────────────────


@dataclass
class ProvisionResult:
    config_path: Path
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    bucket_created: bool = False
    rekeyed: list[str] = field(default_factory=list)  # existing KBs given a rotated writer key
    #: ``.env`` settings that differ from the stored config (secrets named, never shown).
    mismatches: list[str] = field(default_factory=list)
    reconfigured: bool = False  # KB_RECONFIGURE=1 applied them


_TRUE = ("1", "true", "yes")

#: SetupOptions flag -> the .env variable that feeds it.
_ENV_LABELS = {
    "--llm": "KB_LLM",
    "--model": "KB_LLM_MODEL",
    "--language": "KB_LANGUAGE",
}

#: Keys of the stored ``llm:`` block that only make sense for the provider they were written
#: for; dropped when KB_RECONFIGURE switches provider or CLI.
_PROVIDER_BOUND = {
    "compile_provider",
    "cli_command",
    "default_model",
    "ollama_host",
    "cli_reasoning_effort",
    "aux_model",
    "aux_effort",
    "cli_fallback_command",
    "cli_fallback_model",
    "cli_fallback_effort",
}


def _env(env: Mapping[str, str], name: str) -> Optional[str]:
    return (env.get(name) or "").strip() or None


def _llm_target(llm: dict) -> tuple:
    """(provider, cli command) of a normalised llm block."""
    provider = str(llm.get("compile_provider") or "groq")
    return provider, (llm.get("cli_command") or "claude") if provider == "cli" else None


def _env_target(choice: str) -> tuple:
    return ("cli", choice) if choice in ("claude", "codex") else (choice, None)


def env_mismatches(raw: dict, config_path: Path, env: Mapping[str, str]) -> list[str]:
    """``.env`` LLM/language settings that the stored config does not reflect.

    Only variables that are set are compared. API keys are compared, never printed.
    """
    from agent2kb import setup_wizard as sw

    choice = (_env(env, "KB_LLM") or "").lower() or None
    opts = sw.SetupOptions(
        llm=choice, model=_env(env, "KB_LLM_MODEL"), language=_env(env, "KB_LANGUAGE")
    )
    out = sw.stored_conflicts(
        {k: raw[k] for k in ("llm", "defaults") if raw.get(k)}, config_path, opts, _ENV_LABELS
    )
    llm = config_v2.normalize_llm(raw.get("llm") or {}, "llm")
    key = _env(env, "KB_LLM_API_KEY")
    if choice in ("gemini", "groq", "openrouter") and key:
        stored = llm.get(f"{choice}_api_key")
        if not stored:
            out.append(f"KB_LLM_API_KEY is set but no {choice} API key is stored in {config_path}")
        elif stored != key:
            out.append(f"KB_LLM_API_KEY differs from the {choice} API key stored in {config_path}")
    host = _env(env, "KB_LLM_OLLAMA_HOST")
    if host and choice == "ollama" and llm.get("compile_provider") == "ollama":
        if host != llm.get("ollama_host"):
            out.append(
                f"KB_LLM_OLLAMA_HOST {host!r} conflicts with the ollama_host stored in "
                f"{config_path} ({llm.get('ollama_host')!r})"
            )
    return out


def reconfigured_raw(raw: dict, env: Mapping[str, str], console) -> dict:
    """``raw`` with its ``llm:`` block and ``output_language`` taken from ``.env``.

    Storage, KBs (and their keys), privacy switches and every other setting are kept. With
    the same provider only the variables that are set change; a stored API key survives an
    empty KB_LLM_API_KEY.
    """
    from agent2kb.language import effective_language

    out = dict(raw)
    old = config_v2.normalize_llm(raw.get("llm") or {}, "llm")
    choice = (_env(env, "KB_LLM") or "").lower()
    if choice and _env_target(choice) != _llm_target(old):
        fresh = config_v2.normalize_llm(_llm_block(env, console), "llm")
        llm = {k: v for k, v in old.items() if k not in _PROVIDER_BOUND}
        if not _env(env, "KB_LLM_API_KEY"):
            fresh = {k: v for k, v in fresh.items() if not k.endswith("_api_key")}
        llm.update(fresh)
    else:
        llm = dict(old)
        if _env(env, "KB_LLM_MODEL"):
            llm["default_model"] = _env(env, "KB_LLM_MODEL")
        if choice in ("gemini", "groq", "openrouter") and _env(env, "KB_LLM_API_KEY"):
            llm[f"{choice}_api_key"] = _env(env, "KB_LLM_API_KEY")
        if llm.get("compile_provider") == "ollama" and _env(env, "KB_LLM_OLLAMA_HOST"):
            llm["ollama_host"] = _env(env, "KB_LLM_OLLAMA_HOST")
    short = {v: k for k, v in config_v2.LLM_ALIASES.items()}
    out["llm"] = {short.get(k, k): v for k, v in llm.items()}

    language = _env(env, "KB_LANGUAGE")
    defaults = dict(raw.get("defaults") or {})
    if language and effective_language(language) != effective_language(
        defaults.get("output_language")
    ):
        defaults["output_language"] = language
        out["defaults"] = defaults
    return out


def _report_mismatches(
    result: "ProvisionResult", raw: dict, config_path: Path, env: Mapping[str, str], console
) -> dict:
    """Warn about ``.env`` settings the config ignores, or apply them on KB_RECONFIGURE=1."""
    from rich.markup import escape

    result.mismatches = env_mismatches(raw, config_path, env)
    if not result.mismatches:
        return raw
    if (env.get("KB_RECONFIGURE") or "").strip().lower() in _TRUE:
        new = reconfigured_raw(raw, env, console)
        config_v2.write_v2(config_path, new)
        result.reconfigured = True
        console.print(
            "[green]✓[/green] KB_RECONFIGURE=1: applied the .env LLM and language settings to "
            f"{escape(str(config_path))} (previous file kept as {escape(config_path.name)}.bak; "
            "storage, KBs and data unchanged). Remove KB_RECONFIGURE from .env again."
        )
        return new
    console.print(
        "[yellow]![/yellow] .env differs from the saved config, which is kept (it is only "
        "generated once):"
    )
    for line in result.mismatches:
        console.print(f"    - {escape(line)}")
    console.print(
        "  To apply .env (only the llm block and output_language; storage, KBs and data stay): "
        "set KB_RECONFIGURE=1 in .env, run `docker compose up -d`, then remove it again. If "
        "the saved config is what you want, change or remove those lines in .env instead. "
        "`docker compose exec kb a2kb config show` shows the effective values."
    )
    return raw


def _storage_for(env: Mapping[str, str], keys: dict, endpoint: Optional[str], console) -> dict:
    """The v2 ``storage:`` block: bundled SeaweedFS (no shared key) or the external S3."""
    from agent2kb import setup_wizard as sw

    if not is_external(env):
        return sw.storage_block(
            sw.PRESETS["seaweedfs"],
            bucket=keys["bucket"],
            endpoint=endpoint,
            region=DEFAULT_REGION,
        )
    bucket = (env.get("KB_EXT_S3_BUCKET") or "").strip()
    endpoint_ext = (env.get("KB_EXT_S3_ENDPOINT") or "").strip() or None
    preset = (env.get("KB_EXT_S3_PRESET") or "").strip() or ("other" if endpoint_ext else "aws")
    opts = sw.SetupOptions(
        preset=preset,
        endpoint=endpoint_ext,
        region=(env.get("KB_EXT_S3_REGION") or "").strip() or None,
        bucket=bucket or None,
        access_key=(env.get("KB_EXT_S3_ACCESS_KEY") or "").strip() or None,
        secret_key=(env.get("KB_EXT_S3_SECRET_KEY") or "").strip() or None,
    )
    try:
        return sw.ask_storage(opts, interactive=False, console=console)
    except sw.SetupAbort as e:
        raise BootstrapError(
            f"KB_S3_MODE=external: {e} Set KB_EXT_S3_BUCKET, KB_EXT_S3_ACCESS_KEY and "
            "KB_EXT_S3_SECRET_KEY (and KB_EXT_S3_ENDPOINT unless it is AWS)."
        ) from None


def _llm_block(env: Mapping[str, str], console) -> dict:
    from agent2kb import setup_wizard as sw

    choice = (env.get("KB_LLM") or "claude").strip().lower()
    opts = sw.SetupOptions(
        llm=choice,
        model=(env.get("KB_LLM_MODEL") or "").strip() or None,
        api_key=(env.get("KB_LLM_API_KEY") or "").strip() or None,
    )
    try:
        block = sw.ask_llm(opts, interactive=False, console=console)
    except sw.SetupAbort as e:
        raise BootstrapError(f"KB_LLM: {e}") from None
    if choice == "ollama":
        # localhost inside the container is the container: default to the Docker host.
        block["ollama_host"] = (
            env.get("KB_LLM_OLLAMA_HOST") or "http://host.docker.internal:11434"
        ).strip()
    if choice in ("gemini", "groq", "openrouter") and not block.get(f"{choice}_api_key"):
        console.print(
            f"[yellow]![/yellow] KB_LLM={choice} but KB_LLM_API_KEY is empty: compile will not "
            "work until you add the key to the config."
        )
    return block


def _defaults_block(env: Mapping[str, str], console) -> dict:
    from agent2kb import setup_wizard as sw

    opts = sw.SetupOptions(
        language=(env.get("KB_LANGUAGE") or "en").strip(), allow_jina=False, allow_vision=False
    )
    return sw.ask_defaults(opts, interactive=False, console=console)


def wait_for_s3(client, *, timeout: float, sleep: Callable[[float], None] = time.sleep):
    """Wait until the endpoint answers; returns whether the bucket exists.

    Connection failures and timeouts are retried; anything else (bad keys, no permission)
    stops at once with the translated message.
    """
    from agent2kb.errors import translate

    deadline = time.monotonic() + timeout
    while True:
        try:
            return client.bucket_exists()
        except Exception as e:  # noqa: BLE001 - classified below
            friendly = translate(e, client.profile)
            code = friendly.code if friendly else ""
            if code in ("unreachable", "timeout", "bad_endpoint") and time.monotonic() < deadline:
                sleep(2)
                continue
            if code in ("unreachable", "timeout", "bad_endpoint"):
                raise BootstrapError(
                    f"S3 at {client.profile.s3_endpoint or 'AWS'} did not answer within "
                    f"{int(timeout)}s: {friendly.message}"
                ) from None
            if code == "bad_credentials":
                raise BootstrapError(
                    "S3 rejected the admin key. SeaweedFS reads its identities only at start: "
                    "if you changed KB_NAMES, run `docker compose restart seaweedfs`."
                ) from None
            if friendly is None:
                raise
            raise BootstrapError(f"{friendly.message} {friendly.hint}") from None


def provision(
    env: Mapping[str, str],
    *,
    config_path: Path,
    console,
    endpoint: Optional[str] = INTERNAL_ENDPOINT,
    wait: float = 120,
    sleep: Callable[[float], None] = time.sleep,
    client_factory=None,
) -> ProvisionResult:
    """Create the bucket, the config and every KB in ``KB_NAMES`` that is not there yet.

    Existing KBs, storage, LLM and defaults in the config are not changed; ``.env`` LLM and
    language settings that differ are reported, and applied only with KB_RECONFIGURE=1
    (``llm:`` and ``output_language`` only). New KBs go
    through ``setup_wizard.create_kb`` (what ``a2kb new`` runs), signed with the admin key
    because the per-KB keys cannot call HeadBucket; afterwards each KB's own key is tried.
    """
    from rich.markup import escape

    from agent2kb import setup_wizard as sw
    from agent2kb.errors import translate
    from agent2kb.models import ProfileConfig
    from agent2kb.s3_client import S3Client

    make_client = client_factory or S3Client
    names = kb_names(env)
    external = is_external(env)
    keys = load_keys(secrets_dir(env) / KEYS_FILE)
    if not keys.get("bucket"):
        raise BootstrapError(
            f"No {secrets_dir(env) / KEYS_FILE}: run `a2kb bootstrap init` first (the kb-init "
            "service does this)."
        )
    if not external:
        missing = [n for n in names if n not in keys.get("kbs", {})]
        if missing:
            raise BootstrapError(
                f"No keys for {', '.join(missing)} in {secrets_dir(env) / KEYS_FILE}: "
                "`a2kb bootstrap init` has not seen these names yet."
            )
        for n in names:
            if is_revoked(keys["kbs"][n].get("writer", {})):
                raise BootstrapError(
                    f"The writer key of KB '{n}' is revoked. Issue a new one: "
                    f"`a2kb bootstrap rotate {n} --role writer`, then `{_RESTART_S3}`."
                )

    # What goes into a brand-new file; an existing file keeps its own.
    raw = config_v2.read_raw(config_path) if config_path.is_file() else {}
    if raw and not config_v2.is_v2(raw):
        raise BootstrapError(
            f"{config_path} is an old-format config (profiles:); a2kb bootstrap only manages v2 "
            "files. Remove it from the kb-home volume or use another KB_CONFIG."
        )
    existing = config_v2.parse_v2(raw, config_path) if raw else None
    result = ProvisionResult(config_path=config_path)
    if existing:
        raw = _report_mismatches(result, raw, config_path, env, console)
        existing = config_v2.parse_v2(raw, config_path)
    storage = (existing.raw.get("storage") if existing else None) or _storage_for(
        env, keys, endpoint, console
    )
    llm = (existing.raw.get("llm") if existing else None) or _llm_block(env, console)
    defaults = (existing.raw.get("defaults") if existing else None) or _defaults_block(env, console)

    # The key that creates the bucket and the skeletons.
    st = config_v2.normalize_storage(storage, "storage")
    admin = keys.get("admin") or {}
    admin_profile = ProfileConfig(
        name="bootstrap",
        prefix="kbs/",
        **{
            **st,
            "access_key": admin.get("access_key") or st.get("access_key"),
            "secret_key": admin.get("secret_key") or st.get("secret_key"),
        },
    )
    admin_client = make_client(admin_profile)
    exists = wait_for_s3(admin_client, timeout=wait, sleep=sleep)
    if not exists:
        if external and (env.get("KB_EXT_S3_CREATE_BUCKET") or "") not in ("1", "true", "yes"):
            raise BootstrapError(
                f"Bucket '{admin_profile.bucket}' does not exist at the external S3. Create it "
                "there, or set KB_EXT_S3_CREATE_BUCKET=1."
            )
        try:
            admin_client.create_bucket()
        except Exception as e:  # noqa: BLE001
            friendly = translate(e, admin_profile)
            if friendly is None:
                raise
            raise BootstrapError(f"{friendly.message} {friendly.hint}") from None
        result.bucket_created = True
        console.print(f"  [green]✓[/green] Created bucket '{escape(admin_profile.bucket)}'")

    def admin_factory(profile: ProfileConfig):
        return make_client(
            profile.model_copy(
                update={
                    "access_key": admin_profile.access_key,
                    "secret_key": admin_profile.secret_key,
                }
            )
        )

    known = set(existing.kbs) if existing else set()
    for name in names:
        if name in known:
            result.existing.append(name)
            continue
        extra = None
        if not external:
            cred = keys["kbs"][name]["writer"]
            extra = {
                "storage": {"access_key": cred["access_key"], "secret_key": cred["secret_key"]}
            }
        base = None if config_path.is_file() else config_v2.new_raw(storage, llm, defaults)
        try:
            sw.create_kb(
                config_path,
                name,
                adopt=True,
                console=console,
                client_factory=admin_factory,
                base_raw=base,
                extra=extra,
            )
        except ConfigError as e:
            raise BootstrapError(str(e)) from None
        result.created.append(name)

    if not external:
        result.rekeyed = _sync_writer_keys(config_path, keys, [n for n in names if n in known])
        _verify_writers(env, config_path, names, make_client)
    return result


def _sync_writer_keys(config_path: Path, keys: dict, names: list[str]) -> list[str]:
    """After ``rotate``: copy a changed writer key into the KB's own entry in the config."""
    raw = config_v2.read_raw(config_path)
    changed = []
    for name in names:
        entry = (raw.get("kbs") or {}).get(name)
        cred = keys["kbs"][name]["writer"]
        storage = entry.get("storage") if isinstance(entry, dict) else None
        if not isinstance(storage, dict) or not storage.get("access_key"):
            continue
        if (storage["access_key"], storage.get("secret_key")) != (
            cred["access_key"],
            cred["secret_key"],
        ):
            storage["access_key"], storage["secret_key"] = cred["access_key"], cred["secret_key"]
            changed.append(name)
    if changed:
        config_v2.write_v2(config_path, raw)
    return changed


def _verify_writers(env, config_path: Path, names: list[str], make_client) -> None:
    """Each KB's own key must be accepted by S3 (SeaweedFS has loaded the identities)."""
    from agent2kb.errors import translate

    cfg = config_v2.parse_v2(config_v2.read_raw(config_path), config_path)
    for name in names:
        profile = config_v2.build_profile(cfg, name)
        try:
            make_client(profile).list_one()
        except Exception as e:  # noqa: BLE001
            friendly = translate(e, profile)
            if friendly is not None and friendly.code == "bad_credentials":
                raise BootstrapError(
                    f"S3 does not know the key of KB '{name}' yet. SeaweedFS reads its "
                    "identities only when it starts: run `docker compose restart seaweedfs`, "
                    "then `docker compose up -d` again."
                ) from None
            raise BootstrapError(
                f"KB '{name}': its own key cannot list kbs/{name}/ "
                f"({friendly.message if friendly else e})."
            ) from None


# ─── crontab ────────────────────────────────────────────────────────

#: Same jobs as docker/crontab.example. Models are never named here: the KB's config decides.
_CRON_HEADER = """\
# Generated by `a2kb bootstrap crontab` when the container starts, from the KBs in the
# config. To use your own file instead, mount it at /etc/kb/crontab (docs/docker.md).
# The profile decides which provider and model run; nothing here names one.
"""


def render_crontab(kbs: list[str], *, digest: bool = False) -> str:
    """supercronic crontab: compile, lint, backup, stubs (and the digest) for each KB.

    Start minutes are staggered so several KBs do not compile at the same moment.
    """
    lines = [_CRON_HEADER]
    scripts = "/opt/kb/scripts"
    for i, name in enumerate(kbs):
        off = (i * 3) % 15
        compile_minutes = ",".join(str(off + 15 * k) for k in range(4))
        arg = f"/data/{name} {name}"
        lines += [
            f"# {name}",
            f"{compile_minutes} * * * * {scripts}/kb-cron-compile.sh {arg}",
            f"{i % 60} 9 * * * {scripts}/kb-cron-lint.sh {arg}",
            f"{(30 + i) % 60} 2,14 * * * {scripts}/kb-cron-backup.sh {arg}",
            f"{i % 60} 3 * * * {scripts}/kb-cron-stubs.sh {arg}",
        ]
        if digest:
            hour = 4 + (i // 2) % 18
            lines.append(f"{30 * (i % 2)} {hour} * * 0 {scripts}/kb-cron-digest.sh {arg}")
        lines.append("")
    return "\n".join(lines)


def config_kbs(config_path: Path) -> list[str]:
    """Member KBs of the v2 config, in file order (admin KBs are not scheduled)."""
    cfg = config_v2.load_v2(config_path)
    if cfg is None:
        raise BootstrapError(
            f"No v2 config at {config_path}: `a2kb bootstrap provision` (service kb-provision) "
            "creates it."
        )
    return [n for n in cfg.kbs if config_v2.kb_role(cfg, n) != "admin"]


def run_crontab(env: Mapping[str, str], config_path: Path, out: Path) -> list[str]:
    kbs = config_kbs(config_path)
    if not kbs:
        raise BootstrapError(f"{config_path} has no KBs to schedule.")
    digest = (env.get("KB_CRON_DIGEST") or "") in ("1", "true", "yes")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_crontab(kbs, digest=digest))
    return kbs


# ─── MCP servers ────────────────────────────────────────────────────


@dataclass
class McpProc:
    kb: str
    port: int
    argv: list[str]
    env: dict[str, str]


def mcp_proc(env: Mapping[str, str], kb: str, port: int, token: str) -> McpProc:
    """The ``a2kb mcp --http`` command for one KB. The token goes in the environment, not argv."""
    hosts = [f"kb-mcp:{port}"]
    if (env.get("KB_PUBLIC_HOST") or "").strip():
        hosts.append(f"{public_host(env)}:{port}")
    domain = domain_of(env)
    if domain:
        hosts.append(f"mcp.{domain}")
    explicit = (env.get("KB_PUBLIC_MCP_URL") or "").strip()
    if explicit:
        hosts.append(urlsplit(explicit).netloc)
    bind = (env.get("KB_MCP_BIND") or "0.0.0.0").strip()
    argv = [sys.executable, "-m", "agent2kb.main", "-p", kb, "mcp", "--http", f"{bind}:{port}"]
    for h in dict.fromkeys(hosts):
        argv += ["--allow-host", h]
    if (env.get("KB_MCP_ALLOW_LLM") or "") in ("1", "true", "yes"):
        argv.append("--allow-llm")
    if (env.get("KB_MCP_ALLOW_WRITE") or "") in ("1", "true", "yes"):
        argv.append("--allow-write")
        for dom in parse_domains(env.get("KB_MCP_INGEST_DOMAINS")):
            argv += ["--ingest-allow-domain", dom]
    child_env = dict(os.environ)
    child_env["KB_MCP_TOKEN"] = token
    return McpProc(kb=kb, port=port, argv=argv, env=child_env)


def parse_domains(value: Optional[str]) -> list[str]:
    return [d for d in _NAME_SPLIT.split((value or "").strip().lower()) if d]


def mcp_procs(env: Mapping[str, str], config_path: Path) -> list[McpProc]:
    names = kb_names(env)
    served = mcp_names(env, names)
    keys = load_keys(mcpkeys_dir(env) / MCP_FILE)
    have = set(config_kbs(config_path))
    procs = []
    for kb in served:
        entry = keys.get("mcp", {}).get(kb)
        if not entry:
            raise BootstrapError(f"No MCP port/token for KB '{kb}': run `a2kb bootstrap init`.")
        if kb not in have:
            raise BootstrapError(f"KB '{kb}' is in KB_MCP_KBS but not in the config.")
        procs.append(mcp_proc(env, kb, int(entry["port"]), entry["token"]))
    return procs


def supervise(
    procs: list[McpProc],
    *,
    say: Callable[[str], None] = print,
    popen=subprocess.Popen,
    poll: float = 0.5,
    grace: float = 10,
) -> int:
    """Run every server; when one exits or SIGTERM arrives, stop the rest. Returns an exit code."""
    if not procs:
        say("kb-mcp: no KB to serve (KB_MCP_KBS=none); idling.")
        stop = {"now": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
        while not stop["now"]:
            time.sleep(poll)
        return 0
    children = []
    stop = {"now": False}

    def on_signal(signum, _frame):
        stop["now"] = True

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        for p in procs:
            children.append((p, popen(p.argv, env=p.env)))
            say(f"kb-mcp: KB '{p.kb}' on port {p.port}")
        code = None
        while code is None and not stop["now"]:
            for _p, child in children:
                rc = child.poll()
                if rc is not None:
                    code = rc if rc != 0 else 1  # a server must not exit by itself
                    say(f"kb-mcp: server for '{_p.kb}' exited ({rc}); stopping the others")
                    break
            else:
                time.sleep(poll)
        for _p, child in children:
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + grace
        for _p, child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
        return 0 if code is None else code
    finally:
        for s, h in previous.items():
            signal.signal(s, h)


def health_failures(report, kb: str) -> list[str]:
    """One line per failed doctor check, with its fix hint."""
    lines = [f"Unhealthy: `a2kb -p {kb} doctor --read-only` found a problem:"]
    for c in report.checks:
        if c.status == "fail":
            hint = f" -> {c.hint}" if c.hint else ""
            lines.append(f"  FAIL {c.section} / {c.name}: {c.detail}{hint}")
    return lines


def check_mcp_ports(ports: list[int], host: str = "127.0.0.1") -> list[int]:
    """Ports that do not answer HTTP (a 401 without the token is the healthy answer)."""
    import http.client

    bad = []
    for port in ports:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/mcp")
            conn.getresponse().read()
            conn.close()
        except (OSError, http.client.HTTPException):
            bad.append(port)
    return bad
