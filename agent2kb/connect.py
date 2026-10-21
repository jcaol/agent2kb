"""``a2kb connect obsidian|agent <kb>``: print the settings that attach a client to a KB.

Works for the Docker backend (keys and MCP tokens come from the secrets volume, addresses
from the public settings in .env) and for any other install (the resolved profile's own
endpoint and keys; for agents, a stdio registration and the HTTP recipe).

Remotely Save labels below are from its 0.5.25 settings page ("Remote For S3 or
compatible"); a newer version may word them differently.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from typing import Mapping, Optional

from agent2kb import bootstrap
from agent2kb.models import ProfileConfig


class ConnectError(Exception):
    """Nothing to print; the message says what is missing."""


#: Shown instead of credentials for a KB that has no key of its own.
PLACEHOLDER_ACCESS_KEY = "<access key ID of a separate Obsidian key>"
PLACEHOLDER_SECRET_KEY = "<the secret access key of that key>"


@dataclass
class ObsidianSettings:
    kb: str
    endpoint: str
    region: str
    bucket: str
    access_key: str
    secret_key: str
    prefix: str
    path_style: bool
    #: Where ``secret_key`` can be read again (shown instead of the secret by default).
    secret_source: str
    #: ``docker`` when a dedicated Obsidian key from the secrets volume is used.
    source: str
    warnings: list[str] = field(default_factory=list)
    #: False when the KB has no key of its own (AWS credential chain): the sheet then shows
    #: placeholders for a separate device key instead of any machine credential.
    credentials_known: bool = True


def _keys(env: Mapping[str, str]) -> dict:
    try:
        return bootstrap.load_keys(bootstrap.connect_dir(env) / bootstrap.CONNECT_FILE)
    except bootstrap.BootstrapError as e:
        raise ConnectError(str(e)) from None


def obsidian_settings(
    profile: ProfileConfig, env: Optional[Mapping[str, str]] = None, config_path: str = ""
) -> ObsidianSettings:
    """Remotely Save settings for ``profile``'s KB."""
    env = os.environ if env is None else env
    keys = _keys(env)
    entry = (keys.get("kbs") or {}).get(profile.name, {}).get("obsidian")
    warnings: list[str] = []
    credentials_known = True
    if entry and bootstrap.is_revoked(entry):
        raise ConnectError(
            f"The Obsidian key of KB '{profile.name}' is revoked. Issue a new one with "
            f"`a2kb bootstrap rotate {profile.name} --role obsidian` (docs/docker.md, "
            '"Rotating or revoking keys").'
        )
    if entry:
        endpoint = bootstrap.public_s3_endpoint(env)
        access, secret = entry["access_key"], entry["secret_key"]
        source = "docker"
        secret_source = (
            f"{bootstrap.connect_dir(env) / bootstrap.CONNECT_FILE} (kbs.{profile.name}."
            "obsidian.secret_key; the master copy is keys.json in the kb-secrets volume); "
            f"docker compose exec kb a2kb connect obsidian {profile.name} --show-secret"
        )
        if bootstrap.is_loopback_url(endpoint):
            warnings.append(
                f"{endpoint} only works on this machine. A phone or another computer cannot "
                "reach it: set DOMAIN (HTTPS) or KB_PUBLIC_HOST in .env and bind with "
                "KB_BIND (docs/docker.md)."
            )
    else:
        endpoint = profile.s3_endpoint or (
            f"https://s3.{profile.region or bootstrap.DEFAULT_REGION}.amazonaws.com"
        )
        source = "profile"
        if not profile.access_key or not profile.secret_key:
            # The machine signs with the AWS credential chain (often a temporary session).
            # Never hand that to a device: print the non-secret settings with placeholders.
            access, secret = PLACEHOLDER_ACCESS_KEY, PLACEHOLDER_SECRET_KEY
            credentials_known = False
            secret_source = (
                "a separate Obsidian key that you create at your storage provider (the "
                "secret is shown once, when the key is created)"
            )
            warnings.append(
                f"KB '{profile.name}' has no access key in its config (it uses the AWS "
                "credential chain). Create an access key for Obsidian at your storage "
                f"provider, limited to this KB's prefix '{profile.prefix}', and enter that in "
                "Remotely Save. Do not copy this machine's AWS login or temporary session "
                "into the plugin (docs/obsidian.md)."
            )
        else:
            access, secret = profile.access_key, profile.secret_key
            secret_source = f"the KB's own key in {config_path or 'your a2kb config'}"
            warnings.append(
                "This is the same key agent2kb itself uses for this KB. Better: create a "
                "separate key for Obsidian at your storage provider, limited to "
                f"'{profile.prefix}', so you can revoke it on its own."
            )
        host = endpoint.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
        if bootstrap.is_loopback_url(endpoint):
            warnings.append(
                f"{endpoint} is a loopback address: a phone or another computer cannot "
                "reach it. Use the address those devices use."
            )
        elif "." not in host:
            warnings.append(
                f"'{host}' looks like an internal name (a Docker service?). Remotely Save "
                "needs an address your Obsidian device can reach."
            )
    aws = profile.s3_endpoint is None and source == "profile"
    return ObsidianSettings(
        kb=profile.name,
        endpoint=endpoint,
        region=profile.region or bootstrap.DEFAULT_REGION,
        bucket=profile.bucket,
        access_key=access,
        secret_key=secret,
        prefix=profile.prefix,
        path_style=not aws,
        secret_source=secret_source,
        source=source,
        warnings=warnings,
        credentials_known=credentials_known,
    )


def render_obsidian(s: ObsidianSettings, *, show_secret: bool = False) -> str:
    if show_secret or not s.credentials_known:
        secret = s.secret_key
    else:
        secret = "(hidden, add --show-secret to print it)"
    lines = [
        f"Obsidian (Remotely Save plugin) for KB '{s.kb}'",
        "",
        'Plugin settings, Remote Service = "S3 or compatible":',
        f"  Endpoint                    {s.endpoint}",
        f"  Region                      {s.region}",
        f"  Access Key ID               {s.access_key}",
        f"  Secret Access Key           {secret}",
        f"  Bucket Name                 {s.bucket}",
        f"  S3 URL style                {'Path Style' if s.path_style else 'default'}",
        "  Bypass CORS Issue Locally   on (the default)",
        f"  Change The Remote Prefix    {s.prefix}   (type it, then press Confirm)",
        "",
    ]
    if not s.credentials_known:
        lines += [f"Access key and secret: {s.secret_source}.", ""]
    elif not show_secret:
        lines += [f"The secret is in: {s.secret_source}.", ""]
    for w in s.warnings:
        lines += [f"WARNING: {w}", ""]
    lines += [
        "How the vault maps to the bucket",
        f'  Remotely Save has no "folder" setting for S3, only the remote prefix. With '
        f"'{s.prefix}' the vault root IS",
        f"  '{s.prefix}' of bucket '{s.bucket}': its raw/, wiki/ and outputs/ folders appear in",
        "  Obsidian, and notes or Web Clipper files you save in Clippings/ are uploaded under",
        "  the same prefix, where `a2kb compile` imports them. Without the prefix the vault would",
        "  be the whole bucket (every KB); this key cannot list that, so sync would fail.",
        "",
        "Steps",
        '  1. Install and enable the community plugin "Remotely Save" in a vault',
        "     (a new, empty vault is the safest start: the first sync downloads the KB).",
        "  2. Enter the settings above. Set the remote prefix and press Confirm.",
        '  3. Do not rely on "Check" (connectivity): it lists the whole bucket, which a',
        f"     key limited to '{s.prefix}' is not allowed to do, so it may report a failure",
        "     even though syncing works. Run a sync instead (ribbon icon or command palette).",
        '  4. Leave "sync config folder" off unless you want .obsidian/ uploaded as well.',
        "  5. Leave the plugin's encryption password empty for this prefix: agent2kb reads",
        "     plain Markdown and cannot decrypt files the plugin has encrypted.",
        "",
        "Sync direction, key permissions and timing: docs/obsidian.md.",
    ]
    return "\n".join(lines)


@dataclass
class AgentSettings:
    kb: str
    name: str
    #: HTTP URL and bearer token, when the backend serves this KB over MCP.
    url: Optional[str] = None
    token: Optional[str] = None
    docker: bool = False
    warnings: list[str] = field(default_factory=list)


def agent_settings(
    profile: ProfileConfig,
    env: Optional[Mapping[str, str]] = None,
    *,
    url: Optional[str] = None,
) -> AgentSettings:
    env = os.environ if env is None else env
    keys = _keys(env)
    name = f"kb-{profile.name}"
    entry = (keys.get("mcp") or {}).get(profile.name)
    if keys:
        if not entry:
            raise ConnectError(
                f"KB '{profile.name}' is not served over MCP. Add it to KB_MCP_KBS in .env and run "
                "`docker compose up -d`."
            )
        found = AgentSettings(
            kb=profile.name,
            name=name,
            url=bootstrap.public_mcp_url(env, profile.name, int(entry["port"])),
            token=entry["token"],
            docker=True,
        )
        if bootstrap.is_loopback_url(found.url or ""):
            found.warnings.append(
                f"{found.url} only works on the machine running Docker. For agents elsewhere "
                "set DOMAIN (HTTPS) or KB_PUBLIC_HOST in .env (docs/docker.md)."
            )
        elif (found.url or "").startswith("http://"):
            found.warnings.append(
                "This URL is plain HTTP: the token and the KB content cross the network "
                "unencrypted. Use DOMAIN (HTTPS) or a private network (WireGuard, Tailscale)."
            )
        return found
    return AgentSettings(kb=profile.name, name=name, url=url)


def render_agent(a: AgentSettings, *, show_secret: bool = False) -> str:
    lines = [f"Agent access to KB '{a.kb}' (MCP, read-only unless the server enables more)", ""]
    if a.docker:
        token = a.token if show_secret else "<token>"
        header = shlex.quote(f"Authorization: Bearer {token}")
        lines += [
            "Claude Code, remote server over HTTP:",
            f"  claude mcp add --transport http {a.name} {a.url} --header {header}",
            "",
            "Other MCP clients (.mcp.json style):",
            f'  {{"mcpServers": {{"{a.name}": {{"type": "http", "url": "{a.url}",',
            f'    "headers": {{"Authorization": "Bearer {token}"}}}}}}}}',
            "",
            "Same machine as Docker, without opening a port (stdio through docker compose):",
            f"  claude mcp add {a.name} -- docker compose -f <path>/docker-compose.yml exec -T kb "
            f"a2kb -p {a.kb} mcp",
            "",
            "The token is stored in connect.json (and mcp.json) in the Docker volumes. It gives "
            "read access to this KB only.",
        ]
        if not show_secret:
            lines += [
                "",
                "The token is hidden: run the same command with --show-secret to print it "
                "(keep it out of shared terminals and logs).",
            ]
    else:
        base = a.url or "http://127.0.0.1:8765/mcp"
        host_port = base.split("://", 1)[-1].split("/", 1)[0]
        lines += [
            "On the machine that has a2kb installed (stdio, nothing listens on the network):",
            f"  claude mcp add {a.name} -- a2kb -p {a.kb} mcp",
            "",
            "Or as a server over HTTP (for agents on other machines):",
            '  export KB_MCP_TOKEN="$(openssl rand -hex 24)"   # keep it; clients need it',
            f"  a2kb -p {a.kb} mcp --http {host_port}",
            f"  claude mcp add --transport http {a.name} {base} "
            "--header 'Authorization: Bearer <that token>'",
            "",
            "Put TLS in front before exposing HTTP beyond a trusted network (docs/mcp.md). "
            "The Docker backend (docs/docker.md) does all of this for you.",
        ]
    for w in a.warnings:
        lines += ["", f"WARNING: {w}"]
    return "\n".join(lines)
