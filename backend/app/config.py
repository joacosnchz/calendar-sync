from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://calsync:calsync@db:5432/calsync"
    redis_url: str = "redis://redis:6379/0"

    # URL Microsoft Graph uses to reach /webhooks/* (must be public HTTPS in production).
    public_api_url: str = "http://localhost:8000"
    # Browser-facing URL of the Next.js app. OAuth redirect URIs live under it.
    public_web_url: str = "http://localhost:3000"

    session_secret: str = Field(min_length=32)
    # Comma-separated Fernet keys. The first encrypts; all decrypt (supports rotation).
    data_encryption_keys: str

    # Identity platform. login_base_url is called server-side; the browser variant is
    # used for redirects (they differ only when running against the local mock).
    login_base_url: str = "https://login.microsoftonline.com"
    login_browser_base_url: str | None = None
    graph_base_url: str = "https://graph.microsoft.com/v1.0"

    # Multi-tenant app users sign in with. Delegated permissions only (openid, profile,
    # email, offline_access, User.Read, Calendars.Read), each user consents for their own
    # mailbox; no application permissions and no admin consent. Confidential client: a
    # secret or (preferred) a certificate. Redirect URI: {public_web_url}/auth/callback.
    source_app_client_id: str
    source_app_client_secret: str | None = None
    source_app_cert_path: str | None = None
    source_app_cert_thumbprint: str | None = None

    operator_emails: str = ""

    sync_window_past_days: int = 90
    sync_window_future_days: int = 365
    # Outlook event subscriptions last at most 4230 minutes.
    subscription_lifetime_minutes: int = 4200
    subscription_renew_interval_hours: int = 24
    delta_resync_interval_hours: int = 6
    # A stored deltaLink is tied to the window it was created with. Re-baseline with a
    # fresh window once it is this old so the rolling window keeps moving.
    delta_rebaseline_hours: int = 24

    session_ttl_hours: int = 24 * 7
    metrics_port: int = 9100

    @property
    def operator_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.operator_emails.split(",") if e.strip()}

    @property
    def encryption_keys(self) -> list[str]:
        return [k.strip() for k in self.data_encryption_keys.split(",") if k.strip()]

    @property
    def browser_login_base_url(self) -> str:
        return (self.login_browser_base_url or self.login_base_url).rstrip("/")

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_web_url.rstrip('/')}/auth/callback"

    @property
    def cookies_secure(self) -> bool:
        return self.public_web_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
