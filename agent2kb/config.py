"""Configuration loading with multi-level resolution for agent2kb.

Resolution order (highest priority first):
  1. CLI flags      (--s3-endpoint, --access-key, etc.)
  2. ENV vars       (KB_S3_ENDPOINT, KB_ACCESS_KEY, etc.)
  3. Project-level  ./.kb/config.yaml  (only this project's credentials)
  4. User-level     ~/.kb/config.yaml  (super / personal profiles)

Project-level config is either a flat YAML with a single profile's credentials (old
format) or a pointer holding only ``kb: <name>`` (no secrets; selects a KB defined in the
user-level file). User-level config is either the multi-profile format with a "profiles:"
key (old) or the v2 format (``version: 2`` / ``kbs:``, see ``agent2kb.config_v2``). The
user-level file is ``--config``, else ``$KB_CONFIG``, else ``~/.kb/config.yaml``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger

from agent2kb import config_v2
from agent2kb.config_v2 import ConfigError
from agent2kb.models import KBConfig, LLMProvider, ProfileConfig

USER_CONFIG_DIR = Path.home() / ".kb"
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.yaml"
PROJECT_CONFIG_DIR_NAME = ".kb"
PROJECT_CONFIG_FILE = "config.yaml"

# Environment variable prefix
ENV_PREFIX = "KB_"

#: Points agent2kb at a user-level config file other than ``~/.kb/config.yaml``. Not a
#: profile field, so it is deliberately absent from ``ENV_MAP``.
CONFIG_ENV = "KB_CONFIG"

# Mapping: env var suffix → ProfileConfig field
ENV_MAP = {
    "S3_ENDPOINT": "s3_endpoint",
    "ACCESS_KEY": "access_key",
    "SECRET_KEY": "secret_key",
    "BUCKET": "bucket",
    "PREFIX": "prefix",
    "PROFILE": "name",
    "COMPILE_PROVIDER": "compile_provider",
    "GROQ_API_KEY": "groq_api_key",
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "GEMINI_API_KEY": "gemini_api_key",
    "OLLAMA_HOST": "ollama_host",
    "OUTPUT_LANGUAGE": "output_language",
    "RECENT_DAYS": "recent_days",
    "DEFAULT_MODEL": "default_model",
    "CLI_COMMAND": "cli_command",
    "CLI_REASONING_EFFORT": "cli_reasoning_effort",
    "AUX_MODEL": "aux_model",
    "AUX_EFFORT": "aux_effort",
    "CLI_FALLBACK_COMMAND": "cli_fallback_command",
    "CLI_FALLBACK_MODEL": "cli_fallback_model",
    "CLI_FALLBACK_EFFORT": "cli_fallback_effort",
    # provider-neutral S3
    "S3_REGION": "region",
    "S3_ADDRESSING_STYLE": "addressing_style",
    "S3_CHECKSUM_MODE": "checksum_mode",
    "AWS_PROFILE": "aws_profile",
    # privacy switches (booleans, see _BOOL_FIELDS)
    "PRIVACY_MODE": "privacy_mode",
    "ALLOW_JINA_READER": "allow_jina_reader",
    "ALLOW_CLOUD_VISION": "allow_cloud_vision",
    # embeddings, rerank and URL safety
    "OPENAI_API_KEY": "openai_api_key",
    "VOYAGE_API_KEY": "voyage_api_key",
    "GOOGLE_SAFE_BROWSING_KEY": "google_safe_browsing_key",
    "EMBEDDING_PROVIDER": "embedding_provider",
    "EMBEDDING_MODEL": "embedding_model",
    "RERANK_MODEL": "rerank_model",
    "RERANK_ENABLED": "rerank_enabled",
}

#: Profile fields read from the environment as booleans. Parsed strictly: a typo such as
#: ``KB_PRIVACY_MODE=ture`` must fail loudly, never silently turn a privacy switch off.
_BOOL_FIELDS = frozenset(
    {"privacy_mode", "allow_jina_reader", "allow_cloud_vision", "rerank_enabled"}
)
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def parse_env_bool(name: str, value: str) -> bool:
    """Parse 1/true/yes/on or 0/false/no/off (case-insensitive); anything else is an error."""
    word = value.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ConfigError(
        f"{name}={value!r} is not a valid boolean.\n"
        f"Set the environment variable {name} to 1, true, yes or on, or to 0, false, no or off, "
        "or unset it."
    )


#: Fields where an empty string means "not set" — a project config must not be able
#: to blank out values the user-level config supplies.
_BLANKABLE_FIELDS = {"access_key", "secret_key", "s3_endpoint", "region", "aws_profile"}


def _project_layer(data: dict) -> dict:
    """Drop None and blank credential/endpoint values from a project-config layer."""
    return {
        k: v for k, v in data.items() if v is not None and not (k in _BLANKABLE_FIELDS and v == "")
    }


def _enforce_credential_trust(
    profile_name: str,
    merged: dict,
    project_layer: dict,
    trusted_target: bool,
    project_path: Optional[Path],
) -> None:
    """Keep ambient AWS credentials away from untrusted project configs.

    A ``.kb/config.yaml`` is discovered from the cwd and may come from a cloned
    repo. If it could select boto3's default credential chain (or an
    ``aws_profile``) it could point agent2kb at an attacker's endpoint and have it
    sign requests with the user's AWS credentials. So the chain / ``aws_profile``
    are only honoured when the user-level config or env/CLI flags enable them.
    """
    if not project_layer:
        return
    where = project_path or "the project config"
    if project_layer.get("aws_profile"):
        raise PermissionError(
            f"'aws_profile' in {where} is not allowed: it selects ambient AWS credentials.\n"
            f"Remove it from that file and set it in the user-level config "
            f"({default_user_config_path()}) or via KB_AWS_PROFILE."
        )
    if merged.get("access_key") or merged.get("secret_key"):
        return
    if not trusted_target:
        raise PermissionError(
            f"Profile '{profile_name}' in {where} has no access_key/secret_key, and the AWS "
            "credential chain cannot be enabled from a project config.\n"
            "Put your own access_key and secret_key in that file, or define the profile "
            f"without keys in the user-level config ({default_user_config_path()})."
        )
    if project_layer.get("s3_endpoint") or project_layer.get("region"):
        raise PermissionError(
            f"{where} sets s3_endpoint/region while the profile uses the ambient AWS "
            "credential chain, which would send your AWS credentials to the endpoint it "
            "names.\n"
            "Remove s3_endpoint/region from that file and set them in the user-level config "
            f"({default_user_config_path()})."
        )


#: Settings that decide WHERE the KB's data goes. ``s3_endpoint``/``region`` change the host,
#: the rest the bucket or key prefix under the keys in use.
_TARGET_FIELDS = ("s3_endpoint", "region", "bucket", "prefix", "addressing_style")
_HOST_FIELDS = ("s3_endpoint", "region")


def _enforce_target_trust(
    profile_name: str,
    merged: dict,
    project_layer: dict,
    env_overrides: dict,
    cli_overrides: Optional[dict],
    project_path: Optional[Path],
) -> None:
    """A project config may retarget storage only with keys of its own.

    A cloned repo could otherwise ship ``s3_endpoint: https://attacker`` (keys sent to
    another host) or just ``bucket: attacker-bucket`` / another KB's ``prefix`` (your data
    written, with your keys, somewhere you never chose). So when the project layer sets
    ``s3_endpoint``, ``region``, ``bucket``, ``prefix`` or ``addressing_style`` (and the
    value is not overridden by env/flags), it must also supply its own access_key and
    secret_key. Keys the invoking process names itself (``KB_ACCESS_KEY``/``KB_SECRET_KEY``
    or ``--access-key``/``--secret-key``) count as explicitly chosen, but only for a
    project-chosen bucket/prefix/addressing_style: a project-chosen endpoint/region (the host
    that receives the signed requests) always needs the project layer's own keys, unless
    env/flags set that endpoint/region themselves. Keys inherited from the user-level
    config, or the ambient AWS credential chain, never count.
    """
    cli = cli_overrides or {}
    used = [
        f
        for f in _TARGET_FIELDS
        if f in project_layer and f not in env_overrides and cli.get(f) is None
    ]
    if not used:
        return
    own_keys = (project_layer.get("access_key"), project_layer.get("secret_key"))
    where = project_path or "the project config"
    host = [f for f in used if f in _HOST_FIELDS]
    if host:
        # A host named by the project file may only ever see that file's own keys: never
        # keys from the user-level config, the AWS chain, or env/flags (a CI job's
        # KB_ACCESS_KEY must not be signed toward a host a cloned repo chose). Set
        # KB_S3_ENDPOINT / KB_S3_REGION (or the flags) yourself to use env keys.
        if not all(own_keys):
            raise PermissionError(
                f"{where} sets {'/'.join(host)} for '{profile_name}' without its own "
                "access_key and secret_key, so your keys would be sent to the host it names.\n"
                "Put your own access_key and secret_key in that file, or remove "
                f"{'/'.join(host)} from it (set it in {default_user_config_path()} or via "
                "KB_S3_ENDPOINT / KB_S3_REGION)."
            )
        if (merged.get("access_key"), merged.get("secret_key")) != own_keys:
            raise PermissionError(
                f"{where} sets {'/'.join(host)} but the keys in use come from env/flags, not "
                "from that file, so they would be sent to the host it names.\n"
                "Set KB_S3_ENDPOINT / KB_S3_REGION yourself too, or drop the keys from "
                "env/flags."
            )
    if all(own_keys):
        return
    invoker = {**env_overrides, **{k: v for k, v in cli.items() if v is not None}}
    if invoker.get("access_key") and invoker.get("secret_key"):
        return  # bucket / prefix / addressing_style under keys the invoker named
    raise PermissionError(
        f"{where} sets {'/'.join(used)} for '{profile_name}' without its own access_key and "
        "secret_key, so it could send your data to a bucket or prefix you never chose.\n"
        "Put your own access_key and secret_key in that file, or remove "
        f"{'/'.join(used)} from it (set it in {default_user_config_path()})."
    )


def default_user_config_path() -> Path:
    """User-level config file: ``$KB_CONFIG`` if set, else ``~/.kb/config.yaml``."""
    env = os.environ.get(CONFIG_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return USER_CONFIG_PATH


def ensure_config_dir(path: Optional[Path] = None) -> Path:
    """Ensure config directory exists with secure permissions."""
    config_dir = path or USER_CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
    # Only chmod dirs we own (skip system dirs like /tmp)
    try:
        if config_dir.owner() == os.getenv("USER", ""):
            config_dir.chmod(0o700)
    except (OSError, KeyError):
        pass
    return config_dir


def _is_v2_file(path: Path) -> bool:
    try:
        return config_v2.is_v2(config_v2.read_raw(path))
    except (OSError, ConfigError):
        return False


def _find_project_config() -> Optional[Path]:
    """Walk up from cwd to find .kb/config.yaml in a parent directory.

    The walk reaches ``~/.kb/config.yaml``; a v2 user-level file found there is the
    user-level config, not a project config, so it is skipped.
    """
    current = Path.cwd()
    for parent in [current, *current.parents]:
        candidate = parent / PROJECT_CONFIG_DIR_NAME / PROJECT_CONFIG_FILE
        if candidate.is_file() and not _is_v2_file(candidate):
            return candidate
        # Stop at filesystem root or home
        if parent == Path.home() or parent == parent.parent:
            break
    return None


def _is_admin_name(name: str, config_path: Optional[Path] = None) -> bool:
    """True if the user-level config defines ``name`` with admin powers (``role: admin``)."""
    try:
        path = config_path or default_user_config_path()
        if not path.is_file():
            return False
        raw = config_v2.read_raw(path)
        if config_v2.is_v2(raw):
            cfg = config_v2.parse_v2(raw, path)
            if name in cfg.kbs:
                return config_v2.kb_role(cfg, name) == "admin"
        legacy = (raw.get("profiles") or {}).get(name)
        return isinstance(legacy, dict) and legacy.get("role") == "admin"
    except (OSError, ConfigError):
        return False


def sole_member_kb(config_path: Optional[Path] = None) -> Optional[str]:
    """The one KB of a v2 user-level file that defines exactly one KB, else ``None``.

    Never an admin KB, and never when old ``profiles:`` share the file (then there is more
    than one thing to choose from).
    """
    try:
        path = config_path or default_user_config_path()
        if not path.is_file():
            return None
        raw = config_v2.read_raw(path)
        if not config_v2.is_v2(raw):
            return None
        cfg = config_v2.parse_v2(raw, path)
        if len(cfg.kbs) != 1 or cfg.legacy_profiles:
            return None
        name = next(iter(cfg.kbs))
        return name if config_v2.kb_role(cfg, name) == "member" else None
    except (OSError, ConfigError):
        return None


def autodetect_profile(config_path: Optional[Path] = None) -> Optional[str]:
    """Find a profile name without an explicit ``--profile`` flag.

    Resolution order:
      1. ``KB_PROFILE`` environment variable. Most explicit — chat-bot
         and cron wrappers can ``export KB_PROFILE=<profile>`` per channel
         and then call plain ``a2kb query "..."``.
      2. ``kb:`` field of a project pointer, or ``name:`` field of an old-style
         project config, in the nearest ``.kb/config.yaml`` walking up
         from cwd. Lets ``cd /path/to/project && a2kb query "..."`` Just
         Work for humans / agents that are already in the project dir.

      3. With neither of those present, the only KB of a v2 user-level config
         (``a2kb setup`` creates exactly one). Old-style configs are not guessed.

    Returns ``None`` if nothing usable was found — the caller produces
    the user-facing error so the message can name the operation that
    needed a profile.

    Security: super profile is **never** auto-resolved. If the env or
    project config says ``super`` (or names a KB with ``role: admin``), we return
    ``None`` and force the caller to type ``--profile <name>`` explicitly. This
    preserves the invariant that elevating to admin requires an in-process CLI
    flag, not an env var or a checked-in file.

    Raises ``ConfigError`` for a project pointer that also holds credentials.
    """
    env_profile = os.environ.get(ENV_PREFIX + "PROFILE")
    if env_profile and env_profile != "super" and not _is_admin_name(env_profile, config_path):
        return env_profile
    if env_profile:
        # Surface the rejection so an operator sees why autodetect
        # 'failed' when their env says super.
        logger.warning(
            f"KB_PROFILE={env_profile} is ignored by autodetect (admin profiles must be "
            "explicit via --profile). Falling back to project config."
        )

    project_path = _find_project_config()
    if project_path:
        try:
            with open(project_path) as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            return None
        if isinstance(data, dict):
            if config_v2.is_pointer(data):
                name = config_v2.pointer_name(
                    data, project_path, config_path or default_user_config_path()
                )
                if name == "super" or _is_admin_name(name, config_path):
                    logger.warning(
                        f"{project_path} points at admin profile '{name}'; admin profiles must "
                        "be explicit via --profile."
                    )
                    return None
                return name
            name = data.get("name")
            if isinstance(name, str) and name and name != "super":
                if _is_admin_name(name, config_path):
                    logger.warning(
                        f"{project_path} names admin profile '{name}'; admin profiles must "
                        "be explicit via --profile."
                    )
                    return None
                return name
            # Multi-profile shape with exactly one entry → unambiguous.
            profiles = data.get("profiles")
            if isinstance(profiles, dict) and len(profiles) == 1:
                only = next(iter(profiles.keys()))
                if only != "super" and not _is_admin_name(only, config_path):
                    return only
    elif not env_profile:
        # Nothing selected a KB: a user-level file with a single member KB is unambiguous.
        return sole_member_kb(config_path)
    return None


def _load_project_config(path: Path) -> dict:
    """Load a project-level config: pointer, flat YAML (single profile) or ``profiles:``.

    Pointer format (no secrets; selects a KB from the user-level config):
        kb: research

    Flat project config format (old; holds this project's credentials):
        s3_endpoint: "http://seaweedfs:8333"
        access_key: "projA-key"
        secret_key: "projA-secret"
        bucket: "kb-bucket"
        prefix: "projects/projA/"
    """
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping.")

    if config_v2.is_pointer(data):
        return {"_pointer": data, "profiles": {}}

    # If it has a "profiles:" key, it's a multi-profile config (legacy/user-level)
    if "profiles" in data:
        return data

    # Otherwise it's a flat project config — wrap it
    return {"_project_flat": data, "profiles": {}}


def _load_env_overrides() -> dict:
    """Read KB_* environment variables into a dict of profile fields."""
    overrides = {}
    for env_suffix, field_name in ENV_MAP.items():
        env_key = ENV_PREFIX + env_suffix
        value = os.environ.get(env_key)
        if value is not None:
            overrides[field_name] = (
                parse_env_bool(env_key, value) if field_name in _BOOL_FIELDS else value
            )
    return overrides


def load_config(config_path: Optional[Path] = None) -> KBConfig:
    """Load the user-level file (old ``profiles:`` and/or v2 ``kbs:``) into profiles.

    A v2 KB that does not validate is skipped here (``a2kb list`` and resolving that KB
    by name report the problem), so one broken entry never hides the others.
    """
    path = config_path or default_user_config_path()
    if not path.exists():
        logger.debug(f"Config file not found at {path}.")
        return KBConfig()

    raw = config_v2.read_raw(path)

    profiles = {}
    for name, data in (raw.get("profiles") or {}).items():
        data["name"] = name
        if "compile_provider" in data:
            data["compile_provider"] = LLMProvider(data["compile_provider"])
        profiles[name] = ProfileConfig(**data)

    if config_v2.is_v2(raw):
        cfg = config_v2.parse_v2(raw, path)
        for name, profile in config_v2.v2_profiles(cfg).items():
            if name in profiles:
                raise ConfigError(f"{path}: '{name}' is defined both under 'kbs:' and 'profiles:'.")
            profiles[name] = profile

    return KBConfig(profiles=profiles)


@dataclass
class Resolved:
    """A resolved profile plus where each of its fields came from (``a2kb config show``)."""

    profile: ProfileConfig
    sources: dict[str, str] = field(default_factory=dict)
    project_path: Optional[Path] = None
    user_path: Optional[Path] = None
    layers: list[str] = field(default_factory=list)


def _forbid_role(layer: dict, project_path: Path) -> None:
    if "role" in layer:
        raise PermissionError(
            f"'role' in {project_path} is not allowed.\n"
            f"Remove it; admin powers can only be granted in the user-level config "
            f"({default_user_config_path()})."
        )


def resolve_profile(
    profile_name: str,
    config_path: Optional[Path] = None,
    cli_overrides: Optional[dict] = None,
) -> ProfileConfig:
    """Resolve a profile using multi-level config resolution.

    Priority: CLI flags > ENV vars > project-level config > user-level config.

    SECURITY: "super" profile (and any v2 KB / profile with ``role: admin``) is ONLY
    loaded from user-level config. Project-level, env vars, and CLI flags are completely
    ignored for it to prevent credential-injection attacks that produce is_super=True.
    """
    return resolve_profile_detailed(profile_name, config_path, cli_overrides).profile


def resolve_profile_detailed(
    profile_name: str,
    config_path: Optional[Path] = None,
    cli_overrides: Optional[dict] = None,
    *,
    use_env: bool = True,
) -> Resolved:
    """Like ``resolve_profile`` but also reports the source layer of every field.

    ``use_env=False`` ignores ``KB_*`` variables (used by ``a2kb config migrate``, which
    describes the files, not the current shell).
    """
    user_config_path = config_path or default_user_config_path()

    def _all_from(profile: ProfileConfig, label: str) -> Resolved:
        return Resolved(
            profile,
            {k: label for k in profile.model_dump()},
            user_path=user_config_path,
            layers=[label],
        )

    # ── Super profile: user-level config ONLY, no merging ──
    if profile_name == "super":
        if not user_config_path.is_file():
            raise PermissionError(f"Super profile requires user-level config at {user_config_path}")
        user_config = load_config(user_config_path)
        if "super" not in user_config.profiles:
            raise PermissionError(
                f"Super profile not found in {user_config_path}. "
                "Super can only be defined in user-level config."
            )
        logger.debug(f"Profile 'super' loaded from user config only ({user_config_path})")
        return _all_from(user_config.profiles["super"], f"user ({user_config_path})")

    # ── Normal profiles: multi-level resolution ──
    profile_data: dict = {}
    sources: dict[str, str] = {}
    source = "default"
    project_layer: dict = {}
    user_has_profile = False
    v2_selected = False

    def merge(layer: dict, label: str) -> None:
        profile_data.update(layer)
        for k in layer:
            sources[k] = label

    # Layer 4: User-level config (~/.kb/config.yaml, $KB_CONFIG or --config)
    v2 = None
    if user_config_path.is_file():
        raw = config_v2.read_raw(user_config_path)
        if config_v2.is_v2(raw):
            v2 = config_v2.parse_v2(raw, user_config_path)
    if v2 is not None and profile_name in v2.kbs:
        data, v2_sources = config_v2.profile_data(v2, profile_name)
        if data["role"] == "admin":
            profile = ProfileConfig(**data)
            logger.debug(f"Admin KB '{profile_name}' loaded from user config only")
            return Resolved(
                profile,
                v2_sources,
                user_path=user_config_path,
                layers=[f"user ({user_config_path})"],
            )
        merge(data, f"user ({user_config_path})")
        sources.update(v2_sources)
        source = f"user ({user_config_path})"
        user_has_profile = True
        v2_selected = True
    elif user_config_path.is_file():
        user_config = load_config(user_config_path)
        if profile_name in user_config.profiles:
            legacy = user_config.profiles[profile_name]
            if legacy.role == "admin":
                logger.debug(f"Admin profile '{profile_name}' loaded from user config only")
                return _all_from(legacy, f"user ({user_config_path})")
            merge(legacy.model_dump(mode="json"), f"user ({user_config_path})")
            source = f"user ({user_config_path})"
            user_has_profile = True

    # Layer 3: Project-level config (.kb/config.yaml in cwd or parent)
    project_config_path = _find_project_config()
    pointer_target: Optional[str] = None
    if project_config_path:
        proj = _load_project_config(project_config_path)
        if "_pointer" in proj:
            # A pointer only selects a KB; it contributes no values, and may hold no secrets.
            pointer_target = config_v2.pointer_name(
                proj["_pointer"], project_config_path, user_config_path
            )
        elif v2_selected and ("_project_flat" in proj or profile_name in proj.get("profiles", {})):
            raise PermissionError(
                f"{project_config_path} is an old-style project config, but '{profile_name}' "
                f"is a KB defined in {user_config_path}.\n"
                f"Replace that file with a pointer (`kb: {profile_name}`; settings and keys "
                "stay in the user-level file), or run a2kb from another folder."
            )
        elif "_project_flat" in proj:
            flat = proj["_project_flat"]
            if flat:
                _forbid_role(flat, project_config_path)
                flat.setdefault("name", profile_name)
                # Block any attempt to set name=super via project config
                if flat.get("name") == "super":
                    raise PermissionError("Project-level config cannot define a 'super' profile.")
                project_layer = _project_layer(flat)
                merge(project_layer, f"project ({project_config_path})")
                source = f"project ({project_config_path})"
        elif profile_name in proj.get("profiles", {}):
            pdata = proj["profiles"][profile_name]
            _forbid_role(pdata, project_config_path)
            pdata["name"] = profile_name
            project_layer = _project_layer(pdata)
            merge(project_layer, f"project ({project_config_path})")
            source = f"project ({project_config_path})"

    # Layer 2: Environment variables (KB_S3_ENDPOINT, KB_ACCESS_KEY, etc.)
    env_overrides = _load_env_overrides() if use_env else {}
    if v2_selected:
        # KB_PROFILE only chooses which KB; it must not rename the one that was found.
        env_overrides.pop("name", None)
    if env_overrides:
        # Block env var setting name=super
        if env_overrides.get("name") == "super":
            raise PermissionError("Cannot set KB_PROFILE=super via environment.")
        merge(env_overrides, "env")
        source = f"env + {source}"

    # Layer 1: CLI flags (highest priority)
    cli_values = {k: v for k, v in (cli_overrides or {}).items() if v is not None}
    if cli_values:
        merge(cli_values, "cli flag")
        source = f"cli + {source}"

    # Validate we have enough data
    if not profile_data:
        if pointer_target and pointer_target == profile_name:
            raise ConfigError(
                f"{project_config_path} selects KB '{pointer_target}', but no such KB is "
                f"defined in {user_config_path}.\n"
                f"Run `a2kb list` to see what exists, or `a2kb new {pointer_target}` to create it."
            )
        raise ValueError(
            f"Profile '{profile_name}' not found.\n"
            f"Looked in:\n"
            f"  1. CLI flags\n"
            f"  2. ENV vars (KB_S3_ENDPOINT, KB_ACCESS_KEY, etc.)\n"
            f"  3. Project config: {project_config_path or '(not found)'}\n"
            f"  4. User config: {user_config_path}"
        )

    if "name" not in profile_data:
        profile_data["name"] = profile_name
        sources["name"] = "selected profile name"

    # Final guard: resolved name must not be "super"
    if profile_data.get("name") == "super":
        raise PermissionError(
            "Resolved profile name is 'super' but was not loaded from user config. "
            "This is a security violation."
        )
    if profile_data.get("role") == "admin":
        raise PermissionError(
            "Resolved an admin role outside the user-level config. This is a security violation."
        )

    # The user chose the storage target themselves if their own config defines the
    # profile, or env/CLI name the bucket or endpoint.
    trusted_target = user_has_profile or any(
        src.get(k)
        for src in (env_overrides, cli_overrides or {})
        for k in ("bucket", "s3_endpoint")
    )
    _enforce_credential_trust(
        profile_name, profile_data, project_layer, trusted_target, project_config_path
    )
    _enforce_target_trust(
        profile_name,
        profile_data,
        project_layer,
        env_overrides,
        cli_overrides,
        project_config_path,
    )
    prefix_value = profile_data.get("prefix")
    if isinstance(prefix_value, str) and not prefix_value.strip("/"):
        raise ConfigError(
            f"Profile '{profile_name}' has an empty prefix.\n"
            "Only admin profiles may span the whole bucket; set a prefix such as "
            "'projects/<name>/'."
        )

    # Convert compile_provider string to enum if needed
    if "compile_provider" in profile_data and isinstance(profile_data["compile_provider"], str):
        profile_data["compile_provider"] = LLMProvider(profile_data["compile_provider"])

    logger.debug(f"Profile '{profile_name}' resolved from: {source}")
    final = ProfileConfig(**profile_data)
    if project_layer:
        final._project_source = str(project_config_path)
    return Resolved(
        final,
        sources,
        project_path=project_config_path,
        user_path=user_config_path,
        layers=[source],
    )


def save_config(config: KBConfig, config_path: Optional[Path] = None) -> None:
    """Save configuration to YAML file."""
    path = config_path or default_user_config_path()
    ensure_config_dir(path.parent)

    data = {"profiles": {}}
    for name, profile in config.profiles.items():
        profile_data = profile.model_dump(exclude_none=True, mode="json")
        profile_data.pop("name", None)
        data["profiles"][name] = profile_data

    # The file holds secrets: written atomically and 0600 from creation.
    body = yaml.dump(data, default_flow_style=False, allow_unicode=True)
    config_v2.write_private(path, body.encode("utf-8"))
    logger.info(f"Config saved to {path}")


# Kept for backward compatibility
def get_profile(profile_name: str, config_path: Optional[Path] = None) -> ProfileConfig:
    """Load config and return a specific profile. Legacy wrapper."""
    return resolve_profile(profile_name, config_path)
