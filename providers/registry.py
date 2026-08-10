"""Registry of supported AI provider configuration files.

Default paths are computed in an OS-aware way:
- ``$XDG_CONFIG_HOME`` is honored on Linux/macOS for tools that use the XDG
  spec (OpenCode, Kilo).
- On Windows we fall back to ``%APPDATA%`` (typically ``C:\\Users\\<u>\\AppData\\Roaming``).
- Tools that put their config directly in ``~`` (Claude, Qwen, Codex, Gemini,
  Cursor, QwenPaw) keep that pattern across all OSes.

Users can override any path at runtime via the Settings page (stored in
``PathOverride``).
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

from .schema import SCHEMAS, ProviderSchema


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    path: Path
    format: str  # 'json' | 'toml'
    description: str

    @property
    def display_path(self) -> str:
        try:
            home = Path.home()
            return "~/" + str(self.path.relative_to(home))
        except ValueError:
            return str(self.path)

    @property
    def exists(self) -> bool:
        return self.path.exists()


def _xdg_config_home() -> Path:
    """Return the XDG config home directory, OS-aware."""
    system = platform.system()
    if system == "Windows":
        # Prefer APPDATA. Fallback to default Roaming dir.
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata)
        return Path.home() / "AppData" / "Roaming"
    # Linux + macOS use XDG spec; many tools default to ~/.config even on macOS.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".config"


HOME = Path.home()
CONFIG_HOME = _xdg_config_home()
PLATFORM = platform.system()


PROVIDERS: list[Provider] = [
    Provider(
        key="claude",
        name="Claude Code",
        path=HOME / ".claude" / "settings.json",
        format="json",
        description="Anthropic Claude Code CLI settings (env, permissions, hooks, model).",
    ),
    Provider(
        key="qwen",
        name="Qwen",
        path=HOME / ".qwen" / "settings.json",
        format="json",
        description="Qwen CLI settings with modelProviders array.",
    ),
    Provider(
        key="codex",
        name="Codex",
        path=HOME / ".codex" / "config.toml",
        format="toml",
        description="OpenAI Codex CLI configuration (TOML).",
    ),
    Provider(
        key="opencode",
        name="OpenCode",
        path=CONFIG_HOME / "opencode" / "opencode.json",
        format="json",
        description="OpenCode AI assistant configuration (XDG/APPDATA).",
    ),
    Provider(
        key="gemini",
        name="Gemini CLI",
        path=HOME / ".gemini" / "settings.json",
        format="json",
        description="Google Gemini CLI settings.",
    ),
    Provider(
        key="cursor",
        name="Cursor",
        path=HOME / ".cursor" / "settings.json",
        format="json",
        description="Cursor editor settings.",
    ),
    Provider(
        key="kilo",
        name="Kilo Code",
        path=CONFIG_HOME / "kilo" / "config.json",
        format="json",
        description="Kilo Code AI assistant configuration (XDG/APPDATA).",
    ),
    Provider(
        key="qwenpaw",
        name="QwenPaw",
        path=HOME / ".qwenpaw" / "config.json",
        format="json",
        description="QwenPaw agent configuration.",
    ),
    Provider(
        key="pi",
        name="Pi Coding Agent",
        path=HOME / ".pi" / "agent" / "settings.json",
        format="json",
        description="Pi coding agent (settings, models, MCP configuration).",
    ),
]


PROVIDER_MAP: dict[str, Provider] = {p.key: p for p in PROVIDERS}


def default_path(key: str) -> Path | None:
    p = PROVIDER_MAP.get(key)
    return p.path if p else None


def get_provider(key: str) -> Provider | None:
    """Return the provider with any user path override applied.

    Lazy import of ``PathOverride`` to avoid a circular import between
    ``registry`` and Django app models.
    """
    p = PROVIDER_MAP.get(key)
    if p is None:
        return None
    try:
        from .models import PathOverride
    except Exception:                                # pragma: no cover - app loading
        return p
    try:
        override = PathOverride.objects.filter(provider_key=key).first()
    except Exception:                                # pragma: no cover - DB not ready
        return p
    if override and override.path:
        from dataclasses import replace
        return replace(p, path=Path(os.path.expanduser(override.path)))
    return p


def all_providers() -> list[Provider]:
    """Return all providers with overrides applied (preserves declaration order)."""
    return [get_provider(p.key) or p for p in PROVIDERS]


def get_schema(key: str) -> ProviderSchema | None:
    """Return the schema for a provider key, or ``None``."""
    return SCHEMAS.get(key)
