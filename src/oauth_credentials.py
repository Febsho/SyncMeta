"""Resolve OAuth application credentials without mixing them with user tokens."""
from __future__ import annotations
import os

_PROVIDERS = frozenset({"trakt", "simkl", "anilist", "mdblist"})

def get_oauth_app_credentials(provider: str, profile_credentials: dict | None = None) -> dict[str, str | bool]:
    """Prefer process-only hosted credentials over legacy profile app credentials."""
    provider = str(provider or "").strip().lower()
    if provider not in _PROVIDERS:
        raise ValueError(f"Unknown OAuth provider: {provider}")
    prefix = provider.upper()
    client_id = str(os.getenv(f"{prefix}_CLIENT_ID", "")).strip()
    client_secret = str(os.getenv(f"{prefix}_CLIENT_SECRET", "")).strip()
    if client_id and client_secret:
        return {"client_id": client_id, "client_secret": client_secret, "hosted": True}
    row = (profile_credentials or {}).get(provider, {}) if isinstance(profile_credentials, dict) else {}
    return {"client_id": str(row.get("client_id", "")).strip(), "client_secret": str(row.get("client_secret", "")).strip(), "hosted": False}


def get_simkl_v2_app_credentials(profile_credentials: dict | None = None) -> dict[str, str | bool]:
    """Resolve the separately registered AUTH V2 app without falling back to V1."""
    client_id = str(os.getenv("SIMKL_V2_CLIENT_ID", "")).strip()
    client_secret = str(os.getenv("SIMKL_V2_CLIENT_SECRET", "")).strip()
    if client_id:
        return {"client_id": client_id, "client_secret": client_secret, "hosted": True}
    simkl = (profile_credentials or {}).get("simkl", {}) if isinstance(profile_credentials, dict) else {}
    return {"client_id": str(simkl.get("v2_client_id", "")).strip(),
            "client_secret": str(simkl.get("v2_client_secret", "")).strip(), "hosted": False}

def hosted_oauth_status() -> dict[str, bool]:
    """The safe browser-facing representation of server OAuth setup."""
    status = {provider: bool(get_oauth_app_credentials(provider)["hosted"]) for provider in sorted(_PROVIDERS)}
    # SIMKL's V2 registration is deliberately separate from V1.  The UI only
    # needs to know that its server can start a connection, never the ID/secret.
    status["simkl"] = status["simkl"] or bool(str(os.getenv("SIMKL_V2_CLIENT_ID", "")).strip())
    return status
