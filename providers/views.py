from __future__ import annotations

import difflib
import json
import os
import shutil
from datetime import datetime

from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import ConfigEditForm
from .models import PathOverride, Profile, OAuthConfig
from .registry import PROVIDER_MAP, all_providers, default_path, get_provider, get_schema
from .services import (
    ConfigParseError,
    backup_file,
    find_backup,
    list_backups,
    mask_text,
    parse_text,
    read_text,
    serialize,
    validate_text,
    write_text,
    get_oauth_status,
)
from .connection import test_provider as run_connection_test
from .structured import apply_post, build_context


def _file_signature(provider) -> dict:
    """Return mtime + size for the provider's config file (or zero if missing)."""
    if not provider.path.exists():
        return {"mtime": 0, "size": 0}
    st = provider.path.stat()
    return {"mtime": int(st.st_mtime), "size": st.st_size}


def index(request):
    items = []
    for p in all_providers():
        size = p.path.stat().st_size if p.exists else 0
        backups = list_backups(p)
        profiles = list(Profile.objects.filter(provider_key=p.key))
        items.append(
            {
                "provider": p,
                "size": size,
                "backup_count": len(backups),
                "profile_count": len(profiles),
                "profiles": profiles,
            }
        )
    return render(request, "providers/index.html", {"items": items})


def _read_pi_files(base_path):
    """Read Pi's 3 config files: settings.json, models.json, mcp.json.
    Returns dict of {key: raw_text}. All are JSON.
    """
    pi_files = {}
    for fname in ["settings.json", "models.json", "mcp.json"]:
        fpath = base_path.parent / fname
        key = fname.replace('.json', '')
        if fpath.exists():
            try:
                pi_files[key] = fpath.read_text(encoding="utf-8")
            except Exception:
                pi_files[key] = ""
        else:
            pi_files[key] = ""
    return pi_files


def _read_pi_file_text(provider, file_type: str) -> str:
    """Read a single Pi sub-file's current text from disk."""
    if provider is None or provider.key != "pi":
        return ""
    if file_type not in ("settings", "models", "mcp"):
        return ""
    fpath = provider.path.parent / f"{file_type}.json"
    if not fpath.exists():
        return ""
    try:
        return fpath.read_text(encoding="utf-8")
    except Exception:
        return ""


def detail(request, key: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404(f"Unknown provider: {key}")

    if request.method == "POST":
        form = ConfigEditForm(request.POST)
        if form.is_valid():
            text = form.cleaned_data["content"] or ""
            create_backup = form.cleaned_data["create_backup"]
            ok, msg = validate_text(text, provider.format)
            if not ok:
                messages.error(request, f"Invalid {provider.format.upper()}: {msg}")
            else:
                try:
                    backup = write_text(provider, text, do_backup=create_backup)
                except OSError as exc:
                    messages.error(request, f"Failed to write file: {exc}")
                else:
                    if backup:
                        messages.success(request, f"Saved. Backup at {backup.name}")
                    else:
                        messages.success(request, "Saved.")
                    return redirect(reverse("provider_detail", args=[key]))
    else:
        form = ConfigEditForm(initial={"content": read_text(provider)})

    raw_text = form["content"].value() or ""
    masked_preview = mask_text(raw_text, provider.format) if raw_text else ""
    structured_ctx = build_context(provider, raw_text)
    file_sig = _file_signature(provider)

    # OAuth status for providers that support it (e.g., Claude Code).
    # Also checks OAuthConfig in DB so any provider can be OAuth-enabled via Settings.
    oauth_status = None
    schema = get_schema(provider.key)
    if schema and schema.has_oauth:
        oauth_status = get_oauth_status(provider.key)
    else:
        try:
            from .models import OAuthConfig
            if OAuthConfig.objects.filter(provider_key=provider.key, enabled=True).exists():
                oauth_status = get_oauth_status(provider.key)
        except Exception:
            pass

    # MCP support flag
    from .mcp import get_mcp_key
    has_mcp = get_mcp_key(key) is not None

    # Pi multi-file support
    pi_files = {}
    pi_masked = {}
    pi_models_json = "[]"
    pi_backups = {}
    if key == "pi":
        pi_files = _read_pi_files(provider.path)
        for fkey, ftext in pi_files.items():
            pi_masked[fkey] = mask_text(ftext, provider.format) if ftext else ""
        # Parse models.json for structured UI
        if pi_files.get("models"):
            try:
                models_data = json.loads(pi_files["models"])
                pi_models_json = json.dumps(models_data, ensure_ascii=False)
            except Exception:
                pi_models_json = "{}"
        # Get backup lists for each Pi file
        from .services import list_backups_for_path
        for fname in ["settings.json", "models.json", "mcp.json"]:
            fpath = provider.path.parent / fname
            key_name = fname.replace('.json', '')
            pi_backups[key_name] = list_backups_for_path(fpath)

    return render(
        request,
        "providers/detail.html",
        {
            "provider": provider,
            "form": form,
            "raw_text": raw_text,
            "masked_preview": masked_preview,
            "backups": list_backups(provider),
            "profiles": Profile.objects.filter(provider_key=provider.key),
            "structured": structured_ctx,
            "file_sig": file_sig,
            "oauth_status": oauth_status,
            "has_mcp": has_mcp,
            "pi_files": pi_files,
            "pi_masked": pi_masked,
            "pi_models_json": pi_models_json,
            "pi_backups": pi_backups,
        },
    )


@require_POST
def validate(request, key: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    text = request.POST.get("content", "")
    ok, msg = validate_text(text, provider.format)
    return JsonResponse({"ok": ok, "message": msg, "format": provider.format})


def _build_diff(old: str, new: str, label: str) -> str:
    diff_lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{label}",
        tofile=f"b/{label}",
        n=3,
    )
    return "".join(diff_lines)


@require_POST
def diff_preview(request, key: str):
    """Return a unified diff between proposed raw content and disk."""
    provider = get_provider(key)
    if provider is None:
        raise Http404
    new_text = request.POST.get("content", "")
    old_text = read_text(provider)
    diff = _build_diff(old_text, new_text, provider.path.name)
    return JsonResponse(
        {
            "ok": True,
            "diff": diff,
            "unchanged": old_text == new_text,
            "format": provider.format,
        }
    )


@require_POST
def structured_diff(request, key: str):
    """Apply structured POST to current file in-memory and return a diff."""
    provider = get_provider(key)
    if provider is None:
        raise Http404
    current = read_text(provider)
    ok, msg, new_text = apply_post(provider, current, request.POST)
    if not ok:
        return JsonResponse({"ok": False, "message": msg})
    diff = _build_diff(current, new_text, provider.path.name)
    return JsonResponse(
        {
            "ok": True,
            "diff": diff,
            "unchanged": current == new_text,
            "format": provider.format,
        }
    )


def file_signature(request, key: str):
    """GET: return current mtime+size for change detection polling."""
    provider = get_provider(key)
    if provider is None:
        raise Http404
    return JsonResponse(_file_signature(provider))


def connection_test(request, key: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    text = read_text(provider)
    results = run_connection_test(provider, text)
    return JsonResponse(
        {
            "results": [
                {
                    "label": r.label,
                    "url": r.url,
                    "status": r.status,
                    "http_code": r.http_code,
                    "latency_ms": r.latency_ms,
                    "message": r.message,
                }
                for r in results
            ]
        }
    )


def download(request, key: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    text = read_text(provider)
    if not text:
        text = serialize({}, provider.format)
    response = HttpResponse(text, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{provider.path.name}"'
    return response


@require_POST
def reload_from_disk(request, key: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    messages.info(request, "Reloaded from disk.")
    return redirect(reverse("provider_detail", args=[key]))


@require_POST
def save_structured(request, key: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    current = read_text(provider)
    ok, msg, new_text = apply_post(provider, current, request.POST)
    if not ok:
        messages.error(request, msg)
        return redirect(reverse("provider_detail", args=[key]))
    create_backup = request.POST.get("create_backup") in ("on", "true", "1")
    try:
        backup = write_text(provider, new_text, do_backup=create_backup)
    except OSError as exc:
        messages.error(request, f"Failed to write file: {exc}")
        return redirect(reverse("provider_detail", args=[key]))
    if backup:
        messages.success(request, f"Saved structured changes. Backup at {backup.name}")
    else:
        messages.success(request, "Saved structured changes.")
    return redirect(reverse("provider_detail", args=[key]))


@require_POST
def generate_template(request, key: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    template = STARTER_TEMPLATES.get(key, {})
    text = serialize(template, provider.format)
    try:
        write_text(provider, text, do_backup=True)
    except OSError as exc:
        messages.error(request, f"Failed to write template: {exc}")
    else:
        messages.success(request, "Template generated.")
    return redirect(reverse("provider_detail", args=[key]))


# ---------------------------------------------------------------------------
# Backup actions
# ---------------------------------------------------------------------------


@require_POST
def restore_backup(request, key: str, filename: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    backup_path = find_backup(provider, filename)
    if backup_path is None:
        raise Http404("Backup not found")
    # Backup current state before restoring, so the action is reversible.
    if provider.path.exists():
        backup_file(provider.path)
    provider.path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, provider.path)
    messages.success(request, f"Restored from {backup_path.name}")
    return redirect(reverse("provider_detail", args=[key]))


@require_POST
def delete_backup(request, key: str, filename: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    backup_path = find_backup(provider, filename)
    if backup_path is None:
        raise Http404("Backup not found")
    backup_path.unlink()
    messages.success(request, f"Deleted backup {backup_path.name}")
    return redirect(reverse("provider_detail", args=[key]))


# ---------------------------------------------------------------------------
# Profile actions
# ---------------------------------------------------------------------------


@require_POST
def save_profile(request, key: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    name = (request.POST.get("name") or "").strip()
    note = (request.POST.get("note") or "").strip()
    content = request.POST.get("content") or read_text(provider)

    if not name:
        messages.error(request, "Profile name is required.")
        return redirect(reverse("provider_detail", args=[key]))

    ok, msg = validate_text(content, provider.format)
    if not ok:
        messages.error(request, f"Cannot save profile: invalid {provider.format.upper()}: {msg}")
        return redirect(reverse("provider_detail", args=[key]))

    profile, created = Profile.objects.update_or_create(
        provider_key=key,
        name=name,
        defaults={"content": content, "fmt": provider.format, "note": note},
    )
    messages.success(
        request,
        f"Profile '{profile.name}' {'created' if created else 'updated'}.",
    )
    return redirect(reverse("provider_detail", args=[key]))


@require_POST
def apply_profile(request, key: str, pid: int):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    profile = get_object_or_404(Profile, pk=pid, provider_key=key)
    backup = write_text(provider, profile.content, do_backup=True)
    if backup:
        messages.success(
            request,
            f"Applied profile '{profile.name}'. Previous file backed up to {backup.name}.",
        )
    else:
        messages.success(request, f"Applied profile '{profile.name}'.")
    return redirect(reverse("provider_detail", args=[key]))


@require_POST
def delete_profile(request, key: str, pid: int):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    profile = get_object_or_404(Profile, pk=pid, provider_key=key)
    name = profile.name
    profile.delete()
    messages.success(request, f"Deleted profile '{name}'.")
    return redirect(reverse("provider_detail", args=[key]))


# ---------------------------------------------------------------------------
# Starter templates per provider — minimal, sane defaults.
# ---------------------------------------------------------------------------

def connection_test(request, key: str):
    provider = get_provider(key)
    if provider is None:
        raise Http404
    text = read_text(provider)
    results = run_connection_test(provider, text)
    return JsonResponse(
        {
            "results": [
                {
                    "label": r.label,
                    "url": r.url,
                    "status": r.status,
                    "http_code": r.http_code,
                    "latency_ms": r.latency_ms,
                    "message": r.message,
                }
                for r in results
            ]
        }
    )


# ---------------------------------------------------------------------------
# Settings page — custom paths per provider
# ---------------------------------------------------------------------------


def settings_view(request):
    """List all providers with current path + override status + OAuth config."""
    overrides = {o.provider_key: o for o in PathOverride.objects.all()}
    oauth_configs = {o.provider_key: o for o in OAuthConfig.objects.all()}
    rows = []
    for base in PROVIDER_MAP.values():
        eff = get_provider(base.key) or base
        ovr = overrides.get(base.key)
        oa = oauth_configs.get(base.key)
        rows.append(
            {
                "key": base.key,
                "name": base.name,
                "format": base.format,
                "default_path": str(default_path(base.key) or ""),
                "current_path": str(eff.path),
                "is_override": bool(ovr),
                "exists": eff.exists,
                "oauth_enabled": oa.enabled if oa else False,
                "oauth_command": oa.command if oa else "",
                "oauth_email_path": oa.json_path_email if oa else "email",
                "oauth_plan_path": oa.json_path_plan if oa else "subscriptionType",
                "oauth_org_path": oa.json_path_org if oa else "orgName",
                "oauth_logged_in_path": oa.json_path_logged_in if oa else "loggedIn",
            }
        )
    import platform as _platform
    return render(
        request,
        "providers/settings.html",
        {
            "rows": rows,
            "system": _platform.system(),
        },
    )


@require_POST
def settings_save(request):
    key = request.POST.get("provider_key", "").strip()
    raw_path = (request.POST.get("path") or "").strip()
    if key not in PROVIDER_MAP:
        messages.error(request, f"Unknown provider: {key}")
        return redirect(reverse("settings"))
    if not raw_path:
        # Empty input means "reset to default"
        PathOverride.objects.filter(provider_key=key).delete()
        messages.success(request, f"Reset {key} to default path.")
        return redirect(reverse("settings"))
    expanded = os.path.expanduser(os.path.expandvars(raw_path))
    PathOverride.objects.update_or_create(
        provider_key=key,
        defaults={"path": expanded},
    )
    messages.success(request, f"Saved custom path for {key}: {expanded}")
    return redirect(reverse("settings"))


@require_POST
def settings_reset(request, key: str):
    if key not in PROVIDER_MAP:
        raise Http404
    deleted, _ = PathOverride.objects.filter(provider_key=key).delete()
    if deleted:
        messages.success(request, f"Reset {key} to default path.")
    return redirect(reverse("settings"))


@require_POST
def oauth_save(request):
    """Save OAuth configuration for a provider."""
    key = request.POST.get("provider_key", "").strip()
    if key not in PROVIDER_MAP:
        messages.error(request, f"Unknown provider: {key}")
        return redirect(reverse("settings"))

    enabled = request.POST.get("oauth_enabled") == "1"
    command = request.POST.get("oauth_command", "").strip()
    email_path = request.POST.get("oauth_email_path", "email").strip()
    plan_path = request.POST.get("oauth_plan_path", "subscriptionType").strip()
    org_path = request.POST.get("oauth_org_path", "orgName").strip()
    logged_in_path = request.POST.get("oauth_logged_in_path", "loggedIn").strip()

    if enabled:
        OAuthConfig.objects.update_or_create(
            provider_key=key,
            defaults={
                "enabled": True,
                "command": command or "claude auth status",
                "json_path_email": email_path or "email",
                "json_path_plan": plan_path or "subscriptionType",
                "json_path_org": org_path,
                "json_path_logged_in": logged_in_path or "loggedIn",
            },
        )
        messages.success(request, f"OAuth config saved for {key}.")
    else:
        OAuthConfig.objects.filter(provider_key=key).delete()
        messages.success(request, f"OAuth disabled for {key}.")

    return redirect(reverse("settings"))


@require_POST
def oauth_reset(request, key: str):
    """Reset OAuth config to factory default."""
    if key not in PROVIDER_MAP:
        raise Http404
    OAuthConfig.objects.filter(provider_key=key).delete()
    messages.success(request, f"OAuth config reset for {key}.")
    return redirect(reverse("settings"))


STARTER_TEMPLATES: dict[str, dict] = {
    "claude": {
        "env": {
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_AUTH_TOKEN": "sk-...",
        },
        "model": "sonnet",
        "permissions": {
            "allow": ["Read(**)"],
            "deny": ["Bash(rm -rf *)"],
            "defaultMode": "plan",
        },
    },
    "qwen": {
        "env": {"DASHSCOPE_API_KEY": "sk-..."},
        "modelProviders": {
            "openai": [
                {
                    "id": "qwen-max",
                    "name": "Qwen Max",
                    "baseUrl": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                    "envKey": "DASHSCOPE_API_KEY",
                }
            ]
        },
    },
    "codex": {
        "model_reasoning_effort": "high",
        "projects": {},
    },
    "opencode": {
        "$schema": "https://opencode.ai/config.json",
        "model": "anthropic/claude-sonnet-4",
    },
    "gemini": {
        "apiKey": "...",
        "model": "gemini-2.5-pro",
    },
    "cursor": {
        "ai.provider": "anthropic",
        "ai.apiKey": "sk-...",
    },
    "kilo": {
        "apiProvider": "anthropic",
        "apiKey": "sk-...",
    },
    "qwenpaw": {
        "providers": [],
    },
    "pi": {
        "lastChangelogVersion": "0.80.3",
        "theme": "dark",
        "quietStartup": False,
        "enableInstallTelemetry": False,
        "defaultProvider": "anthropic",
        "defaultModel": "claude-sonnet-4",
        "packages": [
            "npm:context-mode",
            "npm:pi-subagents",
            "npm:pi-web-access",
            "npm:pi-mcp-adapter",
        ],
        "terminal": {
            "showTerminalProgress": True
        },
        "hideThinkingBlock": False,
        "defaultProjectTrust": "ask",
        "defaultThinkingLevel": "medium"
    },
    "pi-models": {
        "providers": {
            "anthropic": {
                "baseUrl": "https://api.anthropic.com/v1",
                "api": "openai-completions",
                "apiKey": "sk-...",
                "models": [
                    {
                        "id": "claude-sonnet-4",
                        "name": "Claude Sonnet 4"
                    }
                ]
            }
        }
    },
    "pi-mcp": {
        "mcpServers": {
            "context-mode": {
                "command": "context-mode",
                "directTools": True
            }
        }
    },
}


# ---------------------------------------------------------------------------
# Extensions page — list skills/agents/MCP/hooks/plugins across all providers
# ---------------------------------------------------------------------------

def extensions_view(request):
    """Display all extensions grouped by provider, with type sub-sections."""
    from .extensions import discover_extensions, EXTENSION_LABELS

    all_extensions = discover_extensions()

    # Group by provider first, then by type
    providers_data = {}
    for ext in all_extensions:
        if ext.provider_key not in providers_data:
            provider = get_provider(ext.provider_key)
            providers_data[ext.provider_key] = {
                "key": ext.provider_key,
                "name": provider.name if provider else ext.provider_key,
                "extensions": {
                    "skill": [],
                    "agent": [],
                    "mcp": [],
                    "hook": [],
                    "plugin": [],
                },
                "total": 0,
            }
        providers_data[ext.provider_key]["extensions"][ext.ext_type].append(ext)
        providers_data[ext.provider_key]["total"] += 1

    # Add type labels to each provider's sections
    for prov_key, prov_data in providers_data.items():
        prov_data["sections"] = []
        for ext_type, label_info in EXTENSION_LABELS.items():
            items = prov_data["extensions"].get(ext_type, [])
            prov_data["sections"].append({
                "type": ext_type,
                "title": label_info["title"],
                "description": label_info["description"],
                "example": label_info["example"],
                "format": label_info["format"],
                "items": items,
                "count": len(items),
            })

    # Sort providers by name
    providers_list = sorted(providers_data.values(), key=lambda p: p["name"])

    return render(
        request,
        "providers/extensions.html",
        {
            "providers": providers_list,
            "total_count": len(all_extensions),
        },
    )


# ── Sessions ───────────────────────────────────────────────────────────────

PROVIDERS_WITH_SESSIONS = {"claude", "qwen", "codex"}


def sessions_view(request):
    """All sessions across providers — /sessions/."""
    from .sessions import read_all_sessions
    from .registry import all_providers

    all_prov = all_providers()
    all_sessions = read_all_sessions()

    providers_data = []
    for prov in all_prov:
        sessions = all_sessions.get(prov.key, [])
        has_data = prov.key in PROVIDERS_WITH_SESSIONS and len(sessions) > 0
        providers_data.append({
            "key": prov.key,
            "name": prov.name,
            "has_data": prov.key in PROVIDERS_WITH_SESSIONS,
            "has_sessions": has_data,
            "count": len(sessions),
            "sessions": [s.to_dict() for s in sessions],
        })

    # Default tab
    default_tab = request.GET.get("tab", "")
    if not default_tab:
        active = next((p for p in providers_data if p["has_sessions"]), None)
        default_tab = active["key"] if active else "claude"

    return render(request, "providers/sessions.html", {
        "providers_data": providers_data,
        "default_tab": default_tab,
    })


def provider_sessions(request, key: str):
    """Sessions for a single provider — /p/<key>/sessions/."""
    from .sessions import read_all_sessions
    from .registry import get_provider

    prov = get_provider(key)
    if not prov:
        return redirect("provider_index")

    all_sessions = read_all_sessions()
    sessions = all_sessions.get(key, [])

    return render(request, "providers/sessions.html", {
        "providers_data": [{
            "key": prov.key,
            "name": prov.name,
            "has_data": key in PROVIDERS_WITH_SESSIONS,
            "has_sessions": key in PROVIDERS_WITH_SESSIONS and len(sessions) > 0,
            "count": len(sessions),
            "sessions": [s.to_dict() for s in sessions],
        }],
        "default_tab": key,
        "single_provider": True,
    })


def session_detail(request, provider: str, session_id: str):
    """Full chat history for a single session — /sessions/<provider>/<session_id>/."""
    from .sessions import read_all_sessions
    from .registry import all_providers

    if provider not in PROVIDERS_WITH_SESSIONS:
        return redirect("sessions")

    all_sessions = read_all_sessions()
    provider_sessions_list = all_sessions.get(provider, [])

    session = next((s for s in provider_sessions_list if s.session_id == session_id), None)
    if not session:
        return redirect("sessions")

    # Load full chat history for this session
    full_messages = _load_full_chat(provider, session_id, session.cwd)

    # Provider display name
    all_prov = all_providers()
    prov_name = next((p.name for p in all_prov if p.key == provider), provider.title())

    return render(request, "providers/session_detail.html", {
        "session": session.to_dict(),
        "provider": provider,
        "provider_name": prov_name,
        "full_messages": full_messages,
    })


def _load_full_chat(provider: str, session_id: str, cwd: str) -> list[dict]:
    """Load all messages for a session, formatted for display."""
    from pathlib import Path
    from .sessions import _parse_jsonl, _parse_qwen_chat

    HOME = Path.home()
    messages: list[dict] = []

    if provider == "claude":
        history_path = HOME / ".claude" / "history.jsonl"
        if history_path.exists():
            try:
                for line in history_path.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = entry.get("sessionId") or entry.get("session_id")
                    if sid and sid != session_id:
                        continue
                    # Extract display text
                    display = entry.get("display", "")
                    if not display:
                        msg = entry.get("message", {})
                        if isinstance(msg, dict):
                            parts = msg.get("parts", [])
                            for p in parts:
                                if isinstance(p, dict) and "text" in p:
                                    display = p["text"]
                                    break
                    entry_type = entry.get("type", "unknown")
                    messages.append({
                        "type": entry_type,
                        "display": display[:500] if display else "",
                        "timestamp": entry.get("timestamp", 0),
                    })
            except OSError:
                pass

    elif provider == "qwen":
        projects_dir = HOME / ".qwen" / "projects"
        if projects_dir.exists():
            for proj in projects_dir.iterdir():
                if not proj.is_dir():
                    continue
                chats_dir = proj / "chats"
                if not chats_dir.exists():
                    continue
                chat_file = chats_dir / f"{session_id}.jsonl"
                if chat_file.exists():
                    try:
                        for line in chat_file.read_text().splitlines():
                            if not line.strip():
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            entry_type = entry.get("type", "unknown")
                            display = ""
                            if entry_type == "user":
                                parts = entry.get("message", {}).get("parts", [])
                                for p in parts:
                                    if isinstance(p, dict) and "text" in p:
                                        display = p["text"]
                                        break
                            elif entry_type == "model":
                                parts = entry.get("message", {}).get("parts", [])
                                for p in parts:
                                    if isinstance(p, dict) and "text" in p:
                                        display = p["text"]
                                        break
                            elif entry_type == "system":
                                display = entry.get("subtype", "system")
                            ts_str = entry.get("timestamp", "")
                            ts_ms = 0
                            if ts_str:
                                try:
                                    ts_ms = int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() * 1000)
                                except (ValueError, TypeError):
                                    pass
                            messages.append({
                                "type": entry_type,
                                "display": display[:500] if display else "",
                                "timestamp": ts_ms,
                            })
                    except OSError:
                        pass

    elif provider == "codex":
        history_path = HOME / ".codex" / "history.jsonl"
        msgs, _, _ = _parse_jsonl(history_path, session_id, limit=9999)
        if history_path.exists():
            try:
                for line in history_path.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = entry.get("session_id")
                    if sid and sid != session_id:
                        continue
                    messages.append({
                        "type": "text",
                        "display": entry.get("text", "")[:500],
                        "timestamp": int(entry.get("ts", 0) * 1000),
                    })
            except OSError:
                pass

    return messages


# ── MCP Servers ────────────────────────────────────────────────────────────

def mcp_view(request, key: str):
    """Manage MCP servers for a provider — /p/<key>/mcp/."""
    from .registry import get_provider
    from .mcp import get_mcp_key, read_mcp_servers

    prov = get_provider(key)
    if not prov:
        return redirect("provider_index")

    mcp_key = get_mcp_key(key)
    if not mcp_key:
        return redirect("provider_detail", key=key)

    servers = read_mcp_servers(prov)

    # Pre-serialize complex fields for template use
    def _extract_port(srv: "MCPServer") -> str:
        """Extract port from env values that look like http://host:port."""
        import re
        for v in srv.env.values():
            if isinstance(v, str) and "localhost" in v or ":" in v:
                m = re.search(r":(\d+)", v)
                if m:
                    return m.group(1)
        return ""

    def _server_dict(s):
        return {
            "name": s.name,
            "command": s.command,
            "args": s.args,
            "args_str": "\n".join(s.args),
            "env": s.env,
            "env_str": "\n".join(f"{k}={v}" for k, v in s.env.items()),
            "cwd": s.cwd,
            "server_type": s.server_type,
            "port": _extract_port(s),
        }

    return render(request, "providers/mcp.html", {
        "provider": prov,
        "mcp_key": mcp_key,
        "servers": [_server_dict(s) for s in servers],
    })


def mcp_save(request, key: str):
    """Add / edit / delete MCP server — POST only."""
    from django.views.decorators.http import require_POST
    from .registry import get_provider
    from .mcp import MCPServer, get_mcp_key, read_mcp_servers, write_mcp_servers

    prov = get_provider(key)
    if not prov:
        return redirect("provider_index")

    mcp_key = get_mcp_key(key)
    if not mcp_key:
        return redirect("provider_detail", key=key)

    action = request.POST.get("action", "")
    servers = read_mcp_servers(prov)

    if action == "add":
        name = request.POST.get("name", "").strip()
        command = request.POST.get("command", "").strip()
        if name and command:
            # Parse args (newline or comma separated)
            args_raw = request.POST.get("args", "").strip()
            args = [a.strip() for a in args_raw.replace(",", "\n").split("\n") if a.strip()]
            # Parse env vars (key=value lines)
            env_raw = request.POST.get("env", "").strip()
            env = {}
            for line in env_raw.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
            cwd = request.POST.get("cwd", "").strip()
            servers.append(MCPServer(name=name, command=command, args=args, env=env, cwd=cwd,
                                     server_type=request.POST.get("server_type", "")))
            write_mcp_servers(prov, servers)
            messages.success(request, f"MCP server '{name}' added.")

    elif action == "edit":
        orig_name = request.POST.get("orig_name", "").strip()
        name = request.POST.get("name", "").strip()
        command = request.POST.get("command", "").strip()
        if orig_name and name and command:
            args_raw = request.POST.get("args", "").strip()
            args = [a.strip() for a in args_raw.replace(",", "\n").split("\n") if a.strip()]
            env_raw = request.POST.get("env", "").strip()
            env = {}
            for line in env_raw.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
            cwd = request.POST.get("cwd", "").strip()
            # Replace in list
            new_servers = []
            for s in servers:
                if s.name == orig_name:
                    new_servers.append(MCPServer(name=name, command=command, args=args, env=env, cwd=cwd,
                                         server_type=request.POST.get("server_type", "")))
                else:
                    new_servers.append(s)
            write_mcp_servers(prov, new_servers)
            messages.success(request, f"MCP server '{name}' updated.")

    elif action == "delete":
        name = request.POST.get("name", "").strip()
        servers = [s for s in servers if s.name != name]
        write_mcp_servers(prov, servers)
        messages.success(request, f"MCP server '{name}' deleted.")

    return redirect("provider_mcp", key=key)


@require_POST
def pi_save_file(request, key: str):
    """Save Pi multi-file (settings.json, models.json, mcp.json)."""
    from django.views.decorators.http import require_POST

    provider = get_provider(key)
    if provider is None or key != "pi":
        return JsonResponse({"ok": False, "message": "Invalid provider"})

    file_type = request.POST.get("file_type", "").strip()
    content = request.POST.get("content", "")

    if file_type not in ["settings", "models", "mcp"]:
        return JsonResponse({"ok": False, "message": "Invalid file type"})

    filename = f"{file_type}.json"
    file_path = provider.path.parent / filename

    # Validate JSON
    try:
        import json
        json.loads(content)
    except Exception as e:
        return JsonResponse({"ok": False, "message": f"Invalid JSON: {str(e)}"})

    # Create backup
    backup_name = None
    if file_path.exists():
        from datetime import datetime
        backup_name = f"{filename}.bak.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        backup_path = file_path.parent / backup_name
        try:
            backup_path.write_text(file_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            backup_name = None

    # Write file
    try:
        file_path.write_text(content, encoding="utf-8")
        return JsonResponse({
            "ok": True,
            "message": f"{filename} saved successfully",
            "backup": backup_name
        })
    except Exception as e:
        return JsonResponse({"ok": False, "message": f"Failed to write file: {str(e)}"})


@require_POST
def pi_validate(request, key: str, file_type: str):
    """Validate JSON for a specific Pi sub-file (settings/models/mcp)."""
    provider = get_provider(key)
    if provider is None or provider.key != "pi":
        raise Http404
    if file_type not in ("settings", "models", "mcp"):
        return JsonResponse({"ok": False, "message": f"Unknown file: {file_type}", "format": "json"})
    text = request.POST.get("content", "")
    ok, msg = validate_text(text, "json")
    return JsonResponse({"ok": ok, "message": msg, "format": "json"})


@require_POST
def pi_diff(request, key: str, file_type: str):
    """Return unified diff between proposed content and on-disk Pi sub-file."""
    provider = get_provider(key)
    if provider is None or provider.key != "pi":
        raise Http404
    if file_type not in ("settings", "models", "mcp"):
        return JsonResponse({"ok": False, "diff": "", "unchanged": True, "format": "json"})
    new_text = request.POST.get("content", "")
    old_text = _read_pi_file_text(provider, file_type)
    diff = _build_diff(old_text, new_text, f"{file_type}.json")
    return JsonResponse(
        {
            "ok": True,
            "diff": diff,
            "unchanged": old_text == new_text,
            "format": "json",
        }
    )
