// Outbound HTTP (Microsoft identity platform and Graph).

/** The request never got a response: DNS, connection, TLS or timeout. Always transient. */
export class TransportError extends Error {}

const TIMEOUT_MS = 30_000;

export async function httpFetch(url: string, init: RequestInit = {}): Promise<Response> {
  try {
    return await fetch(url, { ...init, signal: init.signal ?? AbortSignal.timeout(TIMEOUT_MS) });
  } catch (err) {
    throw new TransportError(`${init.method ?? "GET"} ${new URL(url).host}: ${(err as Error).message ?? err}`);
  }
}

export function formBody(data: Record<string, string>): URLSearchParams {
  return new URLSearchParams(data);
}

// deno-lint-ignore no-explicit-any
export async function jsonOrEmpty(resp: Response): Promise<Record<string, any>> {
  const text = await resp.text();
  if (!text) return {};
  try {
    const body = JSON.parse(text);
    return body && typeof body === "object" ? body : {};
  } catch {
    return {};
  }
}
