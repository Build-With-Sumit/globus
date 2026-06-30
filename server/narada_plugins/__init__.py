"""Narada plugin registry + auto-loader.

How plugins register: each plugin module defines a class implementing
one of the 5 protocols in `protocols.py`, instantiates it, and calls
`register()` at module-import time. The registry holds one instance
per slug per category.

How plugins load: `load_all_plugins()` walks this package, imports
every sibling module (e.g. `gmail_composio.py`, `prospeo.py`), which
triggers each plugin's `register()` call. Called once from
`globus_server.py` at boot, AFTER db_helpers is configured.

How callers use the registry: `get_sender("gmail")` returns the
Gmail plugin instance, or `None` if no plugin registered under that
slug. The Narada core never imports plugins directly — always via
the registry — so adding/removing plugins is a single-file change.
"""
from __future__ import annotations
import importlib
import pkgutil
import sys
from typing import Any

from .types import PluginCategory
from .protocols import (
    LeadSource, Verifier, Sender, CRM, LinkedInChannel,
)


# ─────────────────────────────────────────────────────────────────────
# The five category-keyed registries
# ─────────────────────────────────────────────────────────────────────

_LEAD_SOURCES: dict[str, LeadSource] = {}
_VERIFIERS: dict[str, Verifier] = {}
_SENDERS: dict[str, Sender] = {}
_CRMS: dict[str, CRM] = {}
_LINKEDIN_CHANNELS: dict[str, LinkedInChannel] = {}

_REGISTRY_BY_CATEGORY = {
    PluginCategory.LEAD_SOURCE: _LEAD_SOURCES,
    PluginCategory.VERIFIER:    _VERIFIERS,
    PluginCategory.SENDER:      _SENDERS,
    PluginCategory.CRM:         _CRMS,
    PluginCategory.LINKEDIN:    _LINKEDIN_CHANNELS,
}


# ─────────────────────────────────────────────────────────────────────
# Register — called from each plugin's module-level code
# ─────────────────────────────────────────────────────────────────────

def register(plugin: Any) -> None:
    """Register a plugin instance into the right category registry,
    keyed by `plugin.info().name`. Idempotent — re-registering the
    same slug overwrites (useful for hot-reload in dev)."""
    info = plugin.info()
    reg = _REGISTRY_BY_CATEGORY.get(info.category)
    if reg is None:
        raise ValueError(
            f"plugin {info.name!r} has unknown category "
            f"{info.category!r} — must be one of "
            f"{list(_REGISTRY_BY_CATEGORY)}")
    reg[info.name] = plugin
    print(f"[narada] registered {info.category.value} plugin: "
          f"{info.name} ({info.display_name})", flush=True)


# ─────────────────────────────────────────────────────────────────────
# Lookups
# ─────────────────────────────────────────────────────────────────────

def get_lead_source(name: str) -> LeadSource | None:
    return _LEAD_SOURCES.get(name)


def get_verifier(name: str) -> Verifier | None:
    return _VERIFIERS.get(name)


def get_sender(name: str) -> Sender | None:
    return _SENDERS.get(name)


def get_crm(name: str) -> CRM | None:
    return _CRMS.get(name)


def get_linkedin(name: str) -> LinkedInChannel | None:
    return _LINKEDIN_CHANNELS.get(name)


def list_plugins(category: PluginCategory | None = None) -> list:
    """List all registered plugins, optionally filtered by category.
    Used by the /members/narada credentials page to render setup
    sections + by the campaign builder to populate dropdowns."""
    if category is not None:
        return list(_REGISTRY_BY_CATEGORY[category].values())
    out = []
    for reg in _REGISTRY_BY_CATEGORY.values():
        out.extend(reg.values())
    return out


def list_available_for_member(category: PluginCategory,
                                member_email: str) -> list:
    """List plugins in a category that have credentials configured
    for this member. Drives the campaign builder's dropdowns — we
    don't show senders the marketer can't actually use."""
    reg = _REGISTRY_BY_CATEGORY[category]
    return [p for p in reg.values() if p.is_available(member_email)]


# ─────────────────────────────────────────────────────────────────────
# Auto-loader — called once at boot
# ─────────────────────────────────────────────────────────────────────

_LOADED = False


def load_all_plugins() -> None:
    """Import every sibling module in this package, which triggers each
    plugin's module-level `register()` call. Idempotent — safe to
    call multiple times (subsequent calls are no-ops)."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    skip = {"__init__", "protocols", "types"}
    pkg = sys.modules[__name__]
    for finder, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
        if modname in skip or ispkg:
            continue
        try:
            importlib.import_module(f"{__name__}.{modname}")
        except Exception as e:
            print(f"[narada] plugin {modname!r} failed to load: "
                  f"{type(e).__name__}: {e}", flush=True)
    print(f"[narada] plugins loaded: "
          f"leads={len(_LEAD_SOURCES)} "
          f"verifiers={len(_VERIFIERS)} "
          f"senders={len(_SENDERS)} "
          f"crms={len(_CRMS)} "
          f"linkedin={len(_LINKEDIN_CHANNELS)}",
          flush=True)


__all__ = [
    "register",
    "get_lead_source", "get_verifier", "get_sender",
    "get_crm", "get_linkedin",
    "list_plugins", "list_available_for_member",
    "load_all_plugins",
]
