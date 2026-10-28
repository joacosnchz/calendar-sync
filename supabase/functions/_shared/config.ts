// Settings, read from the function's environment (supabase/functions/.env locally,
// `supabase secrets set` on a hosted project).

export interface Settings {
  environment: string;
  // Postgres. Defaults to SUPABASE_DB_URL, which Supabase provides to every function.
  databaseUrl: string;

  // URL Microsoft Graph uses to reach the `webhooks` function (public HTTPS in
  // production): {publicApiUrl}/webhooks/graph and /webhooks/lifecycle.
  publicApiUrl: string;
  // Browser-facing URL of the Next.js app. The OAuth redirect URI lives under it.
  publicWebUrl: string;

  sessionSecret: string;
  // Fernet keys. The first encrypts; all decrypt (supports rotation).
  encryptionKeys: string[];

  // Identity platform. loginBaseUrl is called server-side; browserLoginBaseUrl is used
  // for redirects (they differ only when running against the local mock).
  loginBaseUrl: string;
  browserLoginBaseUrl: string;
  graphBaseUrl: string;

  // Multi-tenant app users sign in with. Delegated permissions only (openid, profile,
  // email, offline_access, User.Read, Calendars.Read), each user consents for their own
  // mailbox; no application permissions and no admin consent. Confidential client: a
  // secret or (preferred) a certificate. Redirect URI: {publicWebUrl}/auth/callback.
  sourceAppClientId: string;
  sourceAppClientSecret: string | null;
  // PEM holding the PKCS#8 private key and its certificate.
  sourceAppCertPem: string | null;
  sourceAppCertThumbprint: string | null;

  operatorEmails: Set<string>;

  syncWindowPastDays: number;
  syncWindowFutureDays: number;
  // Outlook event subscriptions last at most 4230 minutes.
  subscriptionLifetimeMinutes: number;
  subscriptionRenewIntervalHours: number;
  deltaResyncIntervalHours: number;
  // A stored deltaLink is tied to the window it was created with. Re-baseline with a
  // fresh window once it is this old so the rolling window keeps moving.
  deltaRebaselineHours: number;

  sessionTtlHours: number;
  // Shared secret pg_cron sends to the `cron` function.
  cronSecret: string | null;

  oauthRedirectUri: string;
  cookiesSecure: boolean;
}

type Env = Record<string, string | undefined>;

export class ConfigError extends Error {}

function str(env: Env, key: string, fallback?: string): string {
  const value = env[key]?.trim();
  if (value) return value;
  if (fallback !== undefined) return fallback;
  throw new ConfigError(`${key} is required`);
}

function opt(env: Env, key: string): string | null {
  return env[key]?.trim() || null;
}

function int(env: Env, key: string, fallback: number): number {
  const raw = env[key]?.trim();
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value)) throw new ConfigError(`${key} must be an integer`);
  return value;
}

const strip = (url: string) => url.replace(/\/+$/, "");

export function loadSettings(env: Env = Deno.env.toObject()): Settings {
  const sessionSecret = str(env, "SESSION_SECRET");
  if (sessionSecret.length < 32) throw new ConfigError("SESSION_SECRET must be at least 32 characters");
  const encryptionKeys = str(env, "DATA_ENCRYPTION_KEYS")
    .split(",")
    .map((k) => k.trim())
    .filter(Boolean);
  if (!encryptionKeys.length) throw new ConfigError("DATA_ENCRYPTION_KEYS needs at least one key");

  const supabaseUrl = opt(env, "SUPABASE_URL");
  const publicWebUrl = strip(str(env, "PUBLIC_WEB_URL", "http://localhost:3000"));
  const loginBaseUrl = strip(str(env, "LOGIN_BASE_URL", "https://login.microsoftonline.com"));

  return {
    environment: str(env, "ENVIRONMENT", "development"),
    databaseUrl: str(env, "DATABASE_URL", env.SUPABASE_DB_URL?.trim() || undefined),
    publicApiUrl: strip(
      str(env, "PUBLIC_API_URL", supabaseUrl ? `${strip(supabaseUrl)}/functions/v1` : undefined),
    ),
    publicWebUrl,
    sessionSecret,
    encryptionKeys,
    loginBaseUrl,
    browserLoginBaseUrl: strip(opt(env, "LOGIN_BROWSER_BASE_URL") ?? loginBaseUrl),
    graphBaseUrl: strip(str(env, "GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0")),
    sourceAppClientId: str(env, "SOURCE_APP_CLIENT_ID"),
    sourceAppClientSecret: opt(env, "SOURCE_APP_CLIENT_SECRET"),
    sourceAppCertPem: opt(env, "SOURCE_APP_CERT_PEM")?.replaceAll("\\n", "\n") ?? null,
    sourceAppCertThumbprint: opt(env, "SOURCE_APP_CERT_THUMBPRINT"),
    operatorEmails: new Set(
      (env.OPERATOR_EMAILS ?? "")
        .split(",")
        .map((e) => e.trim().toLowerCase())
        .filter(Boolean),
    ),
    syncWindowPastDays: int(env, "SYNC_WINDOW_PAST_DAYS", 90),
    syncWindowFutureDays: int(env, "SYNC_WINDOW_FUTURE_DAYS", 365),
    subscriptionLifetimeMinutes: int(env, "SUBSCRIPTION_LIFETIME_MINUTES", 4200),
    subscriptionRenewIntervalHours: int(env, "SUBSCRIPTION_RENEW_INTERVAL_HOURS", 24),
    deltaResyncIntervalHours: int(env, "DELTA_RESYNC_INTERVAL_HOURS", 6),
    deltaRebaselineHours: int(env, "DELTA_REBASELINE_HOURS", 24),
    sessionTtlHours: int(env, "SESSION_TTL_HOURS", 24 * 7),
    cronSecret: opt(env, "CRON_SECRET"),
    oauthRedirectUri: `${publicWebUrl}/auth/callback`,
    cookiesSecure: publicWebUrl.startsWith("https://"),
  };
}
