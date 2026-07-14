"""Read / write helpers for provider config files.

Always backs up the existing file to ``<file>.bak.<timestamp>`` before
overwriting. Supports JSON and TOML.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import tomli_w

from .registry import Provider


class ConfigParseError(Exception):
    """Raised when a config file cannot be parsed."""


# JSONC: JSON with // line and /* block */ comments. Some tools (Qwen, VSCode)
# write files in this dialect.

_JSONC_LINE = re.compile(r"//[^\n]*")
_JSONC_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_JSONC_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _strip_jsonc(text: str) -> str:
    """Remove // line comments, /* */ block comments and trailing commas.

    Naive but good enough for editor inputs. Strings with `//` are not
    common in API config files.
    """
    text = _JSONC_BLOCK.sub("", text)
    out_lines: list[str] = []
    for line in text.splitlines():
        in_string = False
        escape = False
        cut = None
        i = 0
        while i < len(line):
            ch = line[i]
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
            i += 1
        out_lines.append(line if cut is None else line[:cut])
    text = "\n".join(out_lines)
    text = _JSONC_TRAILING_COMMA.sub(r"\1", text)
    return text


# ---------------------------------------------------------------------------
# Read / parse / serialize / validate
# ---------------------------------------------------------------------------


def read_text(provider: Provider) -> str:
    if not provider.path.exists():
        return ""
    return provider.path.read_text(encoding="utf-8")


def parse_text(text: str, fmt: str) -> Any:
    if not text.strip():
        return {}
    try:
        if fmt == "json":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # Fall back to JSONC (strip comments + trailing commas)
                return json.loads(_strip_jsonc(text))
        if fmt == "toml":
            return tomllib.loads(text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigParseError(str(exc)) from exc
    raise ConfigParseError(f"Unknown format: {fmt}")


def has_jsonc_artifacts(text: str) -> bool:
    """Return True if text contains // or /* */ comments (best-effort)."""
    if not text:
        return False
    if _JSONC_BLOCK.search(text):
        return True
    # Skip "//" inside strings - approximate by checking outside-of-string //.
    return _strip_jsonc(text) != text


def serialize(data: Any, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if fmt == "toml":
        if not isinstance(data, dict):
            raise ConfigParseError("TOML root must be a table/dict.")
        return tomli_w.dumps(data)
    raise ConfigParseError(f"Unknown format: {fmt}")


def validate_text(text: str, fmt: str) -> tuple[bool, str]:
    if not text.strip():
        return True, "empty"
    try:
        parse_text(text, fmt)
    except ConfigParseError as exc:
        return False, str(exc)
    return True, "ok"


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupEntry:
    path: Path
    timestamp: str
    size: int

    @property
    def display_time(self) -> str:
        # 20260530-191234 -> 2026-05-30 19:12:34
        ts = self.timestamp
        if len(ts) == 15 and ts[8] == "-":
            d, t = ts.split("-")
            return f"{d[0:4]}-{d[4:6]}-{d[6:8]} {t[0:2]}:{t[2:4]}:{t[4:6]}"
        return ts


_BACKUP_RE = re.compile(r"\.bak\.(\d{8}-\d{6})$")


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{timestamp}")
    shutil.copy2(path, backup)
    return backup


def list_backups_for_path(file_path: Path) -> list[BackupEntry]:
    """Return all backup files for a given file path, newest first."""
    parent = file_path.parent
    if not parent.exists():
        return []
    base = file_path.name
    out: list[BackupEntry] = []
    for child in parent.iterdir():
        if not child.is_file():
            continue
        if not child.name.startswith(base + "."):
            continue
        match = _BACKUP_RE.search(child.name)
        if not match:
            continue
        out.append(
            BackupEntry(
                path=child,
                timestamp=match.group(1),
                size=child.stat().st_size,
            )
        )
    out.sort(key=lambda b: b.timestamp, reverse=True)
    return out


def list_backups(provider: Provider) -> list[BackupEntry]:
    """Return all backup files for a provider, newest first."""
    return list_backups_for_path(provider.path)


def find_backup(provider: Provider, filename: str) -> Path | None:
    """Resolve a backup filename safely (must live next to provider.path)."""
    parent = provider.path.parent
    candidate = (parent / filename).resolve()
    try:
        candidate.relative_to(parent.resolve())
    except ValueError:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    if not _BACKUP_RE.search(candidate.name):
        return None
    return candidate


def write_text(provider: Provider, text: str, *, do_backup: bool = True) -> Path | None:
    backup: Path | None = None
    if do_backup and provider.path.exists():
        backup = backup_file(provider.path)
    provider.path.parent.mkdir(parents=True, exist_ok=True)
    provider.path.write_text(text, encoding="utf-8")
    return backup


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------

_SECRET_KEY_HINTS = (
    "key",
    "token",
    "secret",
    "password",
    "auth",
    "api_key",
    "apikey",
)


def _looks_secret(key: str) -> bool:
    k = key.lower()
    return any(hint in k for hint in _SECRET_KEY_HINTS)


def looks_secret(key: str) -> bool:
    """Public alias for ``_looks_secret`` so other modules can reuse the heuristic."""
    return _looks_secret(key)


def _mask_value(value: str) -> str:
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * (len(value) - 8) + value[-4:]


def mask_secrets(data: Any) -> Any:
    """Recursively mask secret-looking string values in parsed config data."""
    if isinstance(data, dict):
        return {
            k: (_mask_value(v) if _looks_secret(k) and isinstance(v, str) else mask_secrets(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [mask_secrets(x) for x in data]
    return data


def mask_text(text: str, fmt: str) -> str:
    """Best-effort masking of secrets in serialized text."""
    if not text.strip():
        return text
    try:
        data = parse_text(text, fmt)
    except ConfigParseError:
        return text  # don't break invalid files
    return serialize(mask_secrets(data), fmt)


# ---------------------------------------------------------------------------
# OAuth status detection (Claude Code `claude auth login`)
# ---------------------------------------------------------------------------

@dataclass
class OAuthStatus:
    logged_in: bool
    auth_method: str = ""
    email: str = ""
    subscription_type: str = ""
    org_name: str = ""
    raw: dict | None = None

    @property
    def summary(self) -> str:
        if not self.logged_in:
            return "Not logged in via OAuth"
        parts = [f"✅ {self.email}"]
        if self.subscription_type:
            parts.append(f"({self.subscription_type})")
        if self.org_name:
            parts.append(f"· {self.org_name}")
        return " ".join(parts)


def get_oauth_status(provider_key: str) -> OAuthStatus:
    """Detect OAuth login status for a provider.

    Reads configuration from ``OAuthConfig`` model (if available) and
    executes the configured CLI command to parse JSON output.
    Falls back gracefully when the CLI is not installed or config is missing.
    """
    # Load OAuthConfig from DB (if available)
    cmd_parts: list[str] = []
    paths: dict[str, str] = {}
    enabled = False

    try:
        from .models import OAuthConfig

        cfg = OAuthConfig.objects.filter(provider_key=provider_key).first()
        if cfg and cfg.enabled and cfg.command.strip():
            import shlex
            cmd_parts = shlex.split(cfg.command)
            paths = {
                "email": cfg.json_path_email or "email",
                "plan": cfg.json_path_plan or "subscriptionType",
                "org": cfg.json_path_org,
                "logged_in": cfg.json_path_logged_in or "loggedIn",
            }
            enabled = True
    except Exception:
        pass  # DB not ready or model doesn't exist yet

    # Fallback: Claude Code built-in OAuth (when no DB override)
    if not enabled and provider_key == "claude":
        cmd_parts = ["claude", "auth", "status"]
        paths = {"email": "email", "plan": "subscriptionType", "org": "orgName", "logged_in": "loggedIn"}
        enabled = True

    if not enabled or not cmd_parts:
        return OAuthStatus(logged_in=False)

    import subprocess

    def _deep_get(d: dict, key_path: str) -> Any:
        for part in key_path.split("."):
            if isinstance(d, dict):
                d = d.get(part)
            else:
                return None
        return d

    try:
        result = subprocess.run(
            cmd_parts,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return OAuthStatus(logged_in=False)
        data: dict[str, Any] = json.loads(result.stdout)
        return OAuthStatus(
            logged_in=bool(_deep_get(data, paths["logged_in"])),
            auth_method="oauth",
            email=str(_deep_get(data, paths["email"]) or ""),
            subscription_type=str(_deep_get(data, paths["plan"]) or ""),
            org_name=str(_deep_get(data, paths.get("org", "")) or ""),
            raw=data,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, Exception):
        return OAuthStatus(logged_in=False)
