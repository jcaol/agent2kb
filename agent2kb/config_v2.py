"""Config format v2: one user-level file holds the storage, the LLM and every KB.

    version: 2
    storage:   {endpoint, region, bucket, access_key, secret_key, checksum_mode, ...}
    llm:       {provider, command, model, effort, aux_model, fallback_command, ...}
    defaults:  {output_language, privacy_mode, allow_jina_reader, ...}   # any profile field
    kbs:
      research: {}                       # prefix defaults to kbs/research/
      acme:
        prefix: clients/acme/
        role: admin                      # replaces the magic profile name "super"
        storage: {access_key: ..., secret_key: ...}    # per-KB keys, merged over storage
        output_language: de

Each KB maps onto the existing ``ProfileConfig`` so the rest of agent2kb is unchanged.
Layers merge in this order (later wins): defaults, storage, llm, then the KB's own
``storage``, ``llm`` and direct fields. Secrets belong in this file only; a project
directory points at a KB with ``.kb/config.yaml`` containing just ``kb: <name>``.

Old formats (flat project ``.kb/config.yaml``, user-level ``profiles:``) are not handled
here — see ``agent2kb.config``. Prefix auto-generation applies to v2 KBs only.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from agent2kb.language import DEFAULT_LANGUAGE
from agent2kb.models import ProfileConfig

V2_VERSION = 2
POINTER_KEY = "kb"

#: Names for KBs created with ``a2kb new`` / ``a2kb setup``.
KB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
#: Hand-edited files may hold older, looser names (e.g. migrated profiles with ``_``).
_LOOSE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_TOP_KEYS = {"version", "storage", "llm", "defaults", "kbs", "profiles"}
_KB_ONLY_KEYS = {"prefix", "role", "storage", "llm"}

#: ``storage:`` key -> ProfileConfig field.
STORAGE_KEYS = {
    "endpoint": "s3_endpoint",
    "s3_endpoint": "s3_endpoint",
    "region": "region",
    "bucket": "bucket",
    "access_key": "access_key",
    "secret_key": "secret_key",
    "checksum_mode": "checksum_mode",
    "addressing_style": "addressing_style",
    "aws_profile": "aws_profile",
}
STORAGE_FIELDS = set(STORAGE_KEYS.values())

#: ``llm:`` short key -> ProfileConfig field. Other LLM-related profile fields are
#: accepted under their own names.
LLM_ALIASES = {
    "provider": "compile_provider",
    "command": "cli_command",
    "model": "default_model",
    "effort": "cli_reasoning_effort",
    "fallback_command": "cli_fallback_command",
    "fallback_model": "cli_fallback_model",
    "fallback_effort": "cli_fallback_effort",
}
LLM_FIELDS = set(LLM_ALIASES.values()) | {
    "aux_model",
    "aux_effort",
    "ollama_host",
    "groq_api_key",
    "openrouter_api_key",
    "gemini_api_key",
    "openai_api_key",
    "voyage_api_key",
    "embedding_provider",
    "embedding_model",
    "rerank_model",
    "rerank_enabled",
    "concept_threshold",
    "recent_days",
}

_PROFILE_FIELDS = set(ProfileConfig.model_fields)
#: Fields that identify the KB; never valid inside ``defaults`` or as plain KB settings.
_IDENTITY_FIELDS = {"name", "role", "prefix"}

#: Never printed in full by ``a2kb config show`` / ``a2kb config migrate`` dry-runs.
SECRET_FIELDS = {
    "access_key",
    "secret_key",
    "groq_api_key",
    "openrouter_api_key",
    "gemini_api_key",
    "openai_api_key",
    "voyage_api_key",
    "google_safe_browsing_key",
}


class ConfigError(ValueError):
    """A config file is unusable; the message says which file and what to change."""


def mask(field_name: str, value: Any) -> Any:
    """``***`` for secrets (set/unset stays visible), the value itself otherwise."""
    if field_name in SECRET_FIELDS and value not in (None, ""):
        return "***"
    return value


def mask_dict(data: dict) -> dict:
    """Deep copy of a (v2 or flat) config dict with every secret value masked."""
    out: dict = {}
    for k, v in data.items():
        out[k] = mask_dict(v) if isinstance(v, dict) else mask(STORAGE_KEYS.get(k, k), v)
    return out


# ─── reading / writing the file ──────────────────────────────────────


def read_raw(path: Path) -> dict:
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return data


def is_v2(raw: dict) -> bool:
    """A v2 file says ``version: 2`` or has a ``kbs:`` section."""
    return "kbs" in raw or "version" in raw


def is_legacy_only(raw: dict) -> bool:
    """An old-format user-level file: ``profiles:`` and nothing v2."""
    return bool(raw) and not is_v2(raw)


def write_v2(path: Path, raw: dict) -> None:
    """Write a v2 file atomically with mode 0600; keep a ``.bak`` of the previous one.

    PyYAML drops comments, so the previous file is copied to ``<name>.bak`` first.
    """
    path = Path(path)
    new_dir = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if new_dir:
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    ordered: dict = {"version": V2_VERSION}
    for key in ("storage", "llm", "defaults", "kbs", "profiles"):
        if key in raw:
            ordered[key] = raw[key]
    for key, value in raw.items():
        ordered.setdefault(key, value)
    body = (
        "# agent2kb config (v2). Holds secrets: keep it private (chmod 600), never commit it.\n"
        + yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, default_flow_style=False)
    )
    # An explicit null overrides a value inherited from storage:/llm:/defaults:; say so.
    body = re.sub(
        r"^(\s+[\w-]+: null)$",
        r"\1  # unset for this KB (overrides the value inherited from storage/llm/defaults)",
        body,
        flags=re.M,
    )
    if path.exists():
        write_private(path.with_name(path.name + ".bak"), path.read_bytes())
    write_private(path, body.encode("utf-8"))


def write_private(path: Path, data: bytes) -> None:
    """Replace ``path`` atomically with ``data``; the file is 0600 from the moment it exists.

    The temporary file is created 0600 (never with the umask's mode) and renamed over
    ``path``, so a reader never sees a partial file and no other user can open the secret.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    os.chmod(path, 0o600)


# ─── parsing ─────────────────────────────────────────────────────────


@dataclass
class V2Config:
    path: Path
    raw: dict
    storage: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    kbs: dict[str, dict] = field(default_factory=dict)
    #: Old-format ``profiles:`` sharing the same file (read-only for agent2kb's writers).
    legacy_profiles: dict = field(default_factory=dict)


def _as_dict(value: Any, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping.")
    return value


def _normalize(data: dict, where: str, allowed: dict[str, str], what: str) -> dict:
    out: dict = {}
    for key, value in data.items():
        if key not in allowed:
            raise ConfigError(
                f"{where}: unknown {what} key '{key}'. Allowed: {', '.join(sorted(allowed))}."
            )
        out[allowed[key]] = value
    return out


def normalize_storage(data: Any, where: str) -> dict:
    return _normalize(_as_dict(data, where), where, STORAGE_KEYS, "storage")


def normalize_llm(data: Any, where: str) -> dict:
    allowed = {**LLM_ALIASES, **{f: f for f in LLM_FIELDS}}
    return _normalize(_as_dict(data, where), where, allowed, "llm")


def normalize_fields(data: dict, where: str, *, kb: bool) -> dict:
    """Plain profile fields (``defaults:`` or directly inside a KB)."""
    out: dict = {}
    for key, value in data.items():
        if kb and key in _KB_ONLY_KEYS:
            continue
        if key in STORAGE_KEYS:
            raise ConfigError(f"{where}: '{key}' is a storage setting; put it under 'storage:'.")
        if key in _IDENTITY_FIELDS or key not in _PROFILE_FIELDS and key not in LLM_ALIASES:
            what = "is not allowed here" if key in _IDENTITY_FIELDS else "is not a known setting"
            raise ConfigError(f"{where}: '{key}' {what}.")
        out[LLM_ALIASES.get(key, key)] = value
    return out


def validate_kb_name(name: Any, *, strict: bool = False) -> str:
    pattern = KB_NAME_RE if strict else _LOOSE_NAME_RE
    if not isinstance(name, str) or not pattern.fullmatch(name):
        raise ConfigError(
            f"Invalid KB name {name!r}: use lowercase letters, digits and '-' "
            "(2-63 chars, starting with a letter or digit)."
            if strict
            else f"Invalid KB name {name!r} in the config file."
        )
    if name == "super":
        raise ConfigError(
            "'super' is reserved for old-style configs. For admin powers in a v2 file use "
            "any other name with `role: admin`."
        )
    return name


def validate_prefix(prefix: str, *, allow_empty: bool = False) -> str:
    if prefix == "" and allow_empty:
        return ""
    parts = prefix.split("/")
    if (
        not prefix or prefix.startswith("/") or ".." in parts or "" in parts[:-1]  # '//' inside
    ):
        raise ConfigError(
            f"Invalid prefix {prefix!r}: use a relative path like 'kbs/research/' "
            "(no leading '/', no '//', no '..')."
        )
    return prefix if prefix.endswith("/") else prefix + "/"


def prefixes_overlap(a: str, b: str) -> bool:
    """True if one prefix contains the other (the empty prefix contains everything)."""
    a = a if a.endswith("/") or not a else a + "/"
    b = b if b.endswith("/") or not b else b + "/"
    return a.startswith(b) or b.startswith(a)


def parse_v2(raw: dict, path: Path) -> V2Config:
    version = raw.get("version", V2_VERSION)
    if version != V2_VERSION:
        raise ConfigError(
            f"{path} says version {version!r}; this agent2kb understands config version "
            f"{V2_VERSION}. Upgrade agent2kb or fix the file."
        )
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise ConfigError(
            f"{path}: unknown top-level key(s) {sorted(unknown)}. Allowed: {sorted(_TOP_KEYS)}."
        )
    cfg = V2Config(
        path=path,
        raw=raw,
        storage=normalize_storage(raw.get("storage"), f"{path}: storage"),
        llm=normalize_llm(raw.get("llm"), f"{path}: llm"),
        defaults=normalize_fields(
            _as_dict(raw.get("defaults"), f"{path}: defaults"), f"{path}: defaults", kb=False
        ),
        legacy_profiles=_as_dict(raw.get("profiles"), f"{path}: profiles"),
    )
    kbs = _as_dict(raw.get("kbs"), f"{path}: kbs")
    for name, entry in kbs.items():
        validate_kb_name(name)
        if name in cfg.legacy_profiles:
            raise ConfigError(f"{path}: '{name}' is defined both under 'kbs:' and 'profiles:'.")
        cfg.kbs[name] = _as_dict(entry, f"{path}: kbs.{name}")
    return cfg


def load_v2(path: Path) -> Optional[V2Config]:
    """Parse ``path`` if it exists and is a v2 file; ``None`` for a missing/old-format file."""
    if not Path(path).is_file():
        return None
    raw = read_raw(path)
    if not is_v2(raw):
        return None
    return parse_v2(raw, Path(path))


def kb_role(cfg: V2Config, name: str) -> str:
    role = cfg.kbs[name].get("role", "member")
    if role not in ("member", "admin"):
        raise ConfigError(f"{cfg.path}: kbs.{name}.role must be 'member' or 'admin'.")
    return role


def layers(cfg: V2Config, name: str) -> list[tuple[str, dict]]:
    """Ordered ``(label, fields)`` layers for KB ``name``; later layers win."""
    entry = cfg.kbs[name]
    where = f"{cfg.path}: kbs.{name}"
    return [
        ("defaults", cfg.defaults),
        ("storage", cfg.storage),
        ("llm", cfg.llm),
        (f"kbs.{name}.storage", normalize_storage(entry.get("storage"), f"{where}.storage")),
        (f"kbs.{name}.llm", normalize_llm(entry.get("llm"), f"{where}.llm")),
        (f"kbs.{name}", normalize_fields(entry, where, kb=True)),
    ]


def profile_data(cfg: V2Config, name: str) -> tuple[dict, dict[str, str]]:
    """Merged ProfileConfig fields for a KB, plus which layer each one came from."""
    if name not in cfg.kbs:
        raise ConfigError(f"No KB named '{name}' in {cfg.path}. Known: {sorted(cfg.kbs)}.")
    entry = cfg.kbs[name]
    data: dict = {}
    sources: dict[str, str] = {}
    for label, layer in layers(cfg, name):
        for k, v in layer.items():
            data[k] = v
            sources[k] = f"{label} ({cfg.path})"
    role = kb_role(cfg, name)
    if "prefix" in entry:
        prefix = entry["prefix"]
        if not isinstance(prefix, str):
            raise ConfigError(f"{cfg.path}: kbs.{name}.prefix must be a string.")
        prefix = validate_prefix(prefix, allow_empty=role == "admin")
        sources["prefix"] = f"kbs.{name} ({cfg.path})"
    elif role == "admin":
        prefix = ""
        sources["prefix"] = "default for admin (whole bucket)"
    else:
        prefix = f"kbs/{name}/"
        sources["prefix"] = "auto (kbs/<name>/)"
    data["prefix"] = prefix
    data["name"] = name
    sources["name"] = f"kbs.{name} ({cfg.path})"
    data["role"] = role
    sources["role"] = f"kbs.{name} ({cfg.path})" if "role" in entry else "default"
    if "bucket" not in data:
        raise ConfigError(
            f"KB '{name}' has no bucket. Set 'bucket:' under 'storage:' in {cfg.path} "
            "(or run `a2kb setup`)."
        )
    return data, sources


def build_profile(cfg: V2Config, name: str) -> ProfileConfig:
    data, _ = profile_data(cfg, name)
    return ProfileConfig(**data)


def v2_profiles(cfg: V2Config) -> dict[str, ProfileConfig]:
    """Every KB that validates; broken entries are skipped (``a2kb list`` reports them)."""
    out: dict[str, ProfileConfig] = {}
    for name in cfg.kbs:
        try:
            out[name] = build_profile(cfg, name)
        except Exception:  # noqa: BLE001 - one broken KB must not hide the others
            continue
    return out


def new_raw(
    storage: Optional[dict] = None, llm: Optional[dict] = None, defaults: Optional[dict] = None
) -> dict:
    raw: dict = {"version": V2_VERSION}
    if storage:
        raw["storage"] = storage
    if llm:
        raw["llm"] = llm
    if defaults:
        raw["defaults"] = defaults
    raw["kbs"] = {}
    return raw


def with_kb(raw: dict, name: str, entry: Optional[dict] = None) -> dict:
    """Copy of ``raw`` with KB ``name`` added; never touches the other KBs."""
    out = copy.deepcopy(raw)
    kbs = out.get("kbs")
    if kbs is None:
        kbs = out["kbs"] = {}
    if name in kbs or name in (out.get("profiles") or {}):
        raise ConfigError(f"KB '{name}' already exists in the config file.")
    kbs[name] = entry or {}
    return out


def find_prefix_conflict(cfg: V2Config, name: str, prefix: str, bucket: str) -> Optional[str]:
    """Name of another v2 KB whose prefix (same bucket) overlaps ``prefix``, if any."""
    for other in cfg.kbs:
        if other == name:
            continue
        try:
            data, _ = profile_data(cfg, other)
        except ConfigError:
            continue
        if data.get("bucket") == bucket and prefixes_overlap(data["prefix"], prefix):
            return other
    return None


# ─── project pointer ─────────────────────────────────────────────────

_CREDENTIAL_HINT_FIELDS = STORAGE_FIELDS | SECRET_FIELDS | {"endpoint"}


def is_pointer(data: Any) -> bool:
    return isinstance(data, dict) and POINTER_KEY in data


def pointer_name(data: dict, path: Path, user_config: Path) -> str:
    """The KB a project pointer selects; error if it holds anything but ``kb:``."""
    extra = sorted(k for k in data if k != POINTER_KEY)
    secrets = [k for k in extra if k in _CREDENTIAL_HINT_FIELDS or k.endswith("_api_key")]
    name = data[POINTER_KEY]
    if secrets:
        raise ConfigError(
            f"{path} points at a KB (`kb: {name}`) but also contains credentials "
            f"({', '.join(secrets)}).\n"
            f"Move them to the user-level config ({user_config}) and keep only `kb: <name>` "
            "in that file."
        )
    if extra:
        raise ConfigError(
            f"{path} is a project pointer and may contain only `kb: <name>` "
            f"(found: {', '.join(extra)}).\n"
            f"Remove those keys; settings belong in the user-level config ({user_config}) "
            f"under kbs.{name}."
        )
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{path}: `kb:` must be a KB name.")
    return name.strip()


def write_pointer(directory: Path, name: str) -> Path:
    """Write ``<directory>/.kb/config.yaml`` with ``kb: <name>`` (no secrets, no overwrite)."""
    target = Path(directory) / ".kb" / "config.yaml"
    if target.exists():
        existing = read_raw(target)
        if existing == {POINTER_KEY: name}:
            return target
        raise ConfigError(
            f"{target} already exists; not overwriting it. "
            f"Edit it by hand to contain only `kb: {name}`."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{POINTER_KEY}: {name}\n")
    return target


# ─── migration from an old profile ───────────────────────────────────


def _field_defaults() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, info in ProfileConfig.model_fields.items():
        default = None if info.is_required() else info.default
        out[key] = default.value if isinstance(default, Enum) else default
    return out


def _json(profile: ProfileConfig) -> dict:
    return profile.model_dump(mode="json")


def migration_entry(
    old: ProfileConfig, name: str, existing: Optional[dict] = None
) -> tuple[dict, Optional[dict]]:
    """Build the v2 equivalent of an old profile.

    Returns ``(kb_entry, storage_block)``. ``storage_block`` is the top-level ``storage:``
    to create when the target file has none yet (otherwise ``None`` and any differing
    storage values sit in the KB's own ``storage:``). The result is verified: building the
    profile from it must give back every field of ``old`` (except the name), so migrated
    KBs keep their old behaviour even when the file's ``defaults:`` say otherwise. The
    S3 prefix is carried over unchanged.
    """
    raw = copy.deepcopy(existing) if existing else new_raw()
    want = _json(old)
    defaults = _field_defaults()
    admin = old.is_super
    entry: dict = {"prefix": old.prefix}
    if admin:
        entry["role"] = "admin"
    storage: dict = {}
    other: dict = {}
    for key, value in want.items():
        if key in ("name", "prefix", "role") or value == defaults.get(key) or value is None:
            continue
        if key == "default_model" and want["compile_provider"] == "cli":
            from agent2kb.cli_models import default_model_for

            if value == default_model_for(want["cli_command"]):
                continue  # unpinned in the old profile: the CLI's own default
        if key in STORAGE_FIELDS:
            storage[key] = value
        else:
            other[key] = value

    if existing:
        # Leave out what the file's own defaults:/llm: blocks already say.
        parsed = parse_v2(raw, Path("<migration>"))
        inherited = {**parsed.defaults, **parsed.llm}
        other = {k: v for k, v in other.items() if inherited.get(k) != v}

    create_storage = "storage" not in raw
    storage_block: Optional[dict] = None
    storage_keys = {v: k for k, v in STORAGE_KEYS.items() if k != "s3_endpoint"}
    if create_storage:
        storage_block = {storage_keys[k]: v for k, v in storage.items()}
        raw["storage"] = storage_block
    else:
        base = normalize_storage(raw["storage"], "storage")
        own = {storage_keys[k]: v for k, v in storage.items() if base.get(k) != v}
        if own:
            entry["storage"] = own
    entry.update(other)

    def build(entry_: dict) -> ProfileConfig:
        trial = copy.deepcopy(raw)
        trial.setdefault("kbs", {})[name] = entry_
        return build_profile(parse_v2(trial, Path("<migration>")), name)

    # Verify and patch: any field that would come out different is pinned explicitly.
    for _ in range(2):
        got = _json(build(entry))
        diff = {k: want[k] for k in want if k not in ("name", "role") and got.get(k) != want[k]}
        if not diff:
            break
        for key, value in diff.items():
            if key == "output_language" and value is None:
                value = DEFAULT_LANGUAGE  # pin what unset means, so a v2 default cannot change it
            if key in STORAGE_FIELDS:
                entry.setdefault("storage", {})[storage_keys[key]] = value
            else:
                entry[key] = value
    return entry, storage_block
