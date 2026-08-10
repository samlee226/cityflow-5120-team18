/**
 * Server-side helpers for reaching the CityFlow API.
 *
 * Browser code calls same-origin paths under /api and never contacts the
 * backend directly. The hop to the backend happens here, on the server, so a
 * page served over HTTPS can use a backend that is not, and no cross-origin
 * request is ever made from the browser.
 *
 * CITYFLOW_API_BASE_URL is deliberately not prefixed with NEXT_PUBLIC_, which
 * keeps the backend address out of the client bundle.
 */

const DEFAULT_CITYFLOW_API_URL = "https://finer-conform-radiance.ngrok-free.dev";

export function backendBaseUrl(): string {
  return (process.env.CITYFLOW_API_BASE_URL ?? DEFAULT_CITYFLOW_API_URL).replace(
    /\/$/,
    "",
  );
}

type ProxyOptions = {
  method?: "GET" | "POST";
  body?: string;
  timeoutMs?: number;
};

/**
 * Forwards a request to the backend and returns its JSON response and status
 * unchanged, so error details raised by the API still reach the caller.
 */
export async function proxyToBackend(
  path: string,
  { method = "GET", body, timeoutMs = 15_000 }: ProxyOptions = {},
): Promise<{ body: unknown; status: number }> {
  const response = await fetch(`${backendBaseUrl()}${path}`, {
    method,
    body,
    cache: "no-store",
    signal: AbortSignal.timeout(timeoutMs),
    headers: {
      Accept: "application/json",
      ...(body ? { "Content-Type": "application/json" } : {}),
      // Suppresses the interstitial served by tunnelled development backends.
      "ngrok-skip-browser-warning": "true",
    },
  });

  return {
    body: await response.json().catch(() => null),
    status: response.status,
  };
}
