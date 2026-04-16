"""
Sherlock Graph API authentication.

Uses MSAL (Microsoft Authentication Library) with client-credentials flow
for headless operation on the Mac Mini. The Sherlock service account
authenticates once, gets a token, and the token auto-refreshes.

Config is read from firm.yaml → firm.email section:
    email:
      provider: office365
      tenant_id: "..."
      client_id: "..."
      client_secret: "..."          # or use SHERLOCK_O365_SECRET env var
      service_account: "sherlock@dennislaw.com"
      monitored_mailboxes:
        - "sam@dennislaw.com"

Token cache is persisted to ~/Sherlock/data/secrets/msal_cache.bin so
restarts don't require re-auth.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# MSAL is the only external dependency for auth. It's a pure-Python library
# with no compiled extensions.
try:
    import msal
except ImportError:
    msal = None  # type: ignore
    log.warning("msal not installed — Graph API auth disabled. Run: pip install msal")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/.default"]  # client-credentials uses .default

SECRETS_DIR = Path(os.environ.get(
    "SHERLOCK_SECRETS_DIR",
    Path("~/Sherlock/data/secrets").expanduser(),
))
CACHE_PATH = SECRETS_DIR / "msal_cache.bin"


def _load_email_config() -> dict:
    """Load email config from firm.yaml."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from primary_law.registry import load_firm, CONFIG_DIR
    import yaml

    firm_path = CONFIG_DIR / "firm.yaml"
    with open(firm_path) as f:
        data = yaml.safe_load(f)

    email = data.get("firm", {}).get("email", {})
    if not email:
        raise ValueError("firm.yaml missing firm.email section")

    # Allow env var override for the secret (don't put secrets in YAML if you can avoid it)
    if not email.get("client_secret"):
        email["client_secret"] = os.environ.get("SHERLOCK_O365_SECRET", "")

    required = ["tenant_id", "client_id", "client_secret"]
    missing = [k for k in required if not email.get(k)]
    if missing:
        raise ValueError(f"firm.yaml firm.email missing: {missing}. "
                         f"Set SHERLOCK_O365_SECRET env var for client_secret.")
    return email


class GraphClient:
    """Thin wrapper around MSAL + urllib for Microsoft Graph API calls.

    Uses client-credentials flow (no user interaction required).
    Token is cached to disk and auto-refreshed by MSAL.
    """

    def __init__(self, config: dict | None = None):
        if msal is None:
            raise RuntimeError("msal not installed. Run: pip install msal")

        self.config = config or _load_email_config()
        self.tenant_id = self.config["tenant_id"]
        self.client_id = self.config["client_id"]
        self.client_secret = self.config["client_secret"]
        self.service_account = self.config.get("service_account", "")
        self.monitored_mailboxes = self.config.get("monitored_mailboxes", [])

        # Set up MSAL with persistent token cache
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        self._cache = msal.SerializableTokenCache()
        if CACHE_PATH.exists():
            self._cache.deserialize(CACHE_PATH.read_text())

        self._app = msal.ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            token_cache=self._cache,
        )

    def _save_cache(self):
        if self._cache.has_state_changed:
            CACHE_PATH.write_text(self._cache.serialize())

    def get_token(self) -> str:
        """Acquire a valid access token (from cache or fresh)."""
        result = self._app.acquire_token_silent(SCOPES, account=None)
        if not result:
            result = self._app.acquire_token_for_client(scopes=SCOPES)
        self._save_cache()

        if "access_token" not in result:
            error = result.get("error_description", result.get("error", "unknown"))
            raise RuntimeError(f"Graph auth failed: {error}")

        return result["access_token"]

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        """GET from Graph API. Returns parsed JSON."""
        import urllib.request
        import urllib.parse

        token = self.get_token()
        url = endpoint if endpoint.startswith("http") else f"{GRAPH_BASE}{endpoint}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def get_all_pages(self, endpoint: str, params: dict | None = None,
                      max_pages: int = 50) -> list[dict]:
        """Follow @odata.nextLink pagination, collecting all items."""
        items: list[dict] = []
        data = self.get(endpoint, params)
        items.extend(data.get("value", []))

        pages = 1
        while "@odata.nextLink" in data and pages < max_pages:
            data = self.get(data["@odata.nextLink"])
            items.extend(data.get("value", []))
            pages += 1

        return items
