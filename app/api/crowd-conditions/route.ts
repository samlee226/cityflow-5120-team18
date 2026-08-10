import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const DEFAULT_CITYFLOW_API_URL =
  "https://finer-conform-radiance.ngrok-free.dev";

export async function GET(request: Request) {
  const baseUrl = (
    process.env.CITYFLOW_API_BASE_URL ?? DEFAULT_CITYFLOW_API_URL
  ).replace(/\/$/, "");
  const requestedLevel = new URL(request.url).searchParams.get("level");
  const level = ["low", "medium", "high"].includes(requestedLevel ?? "")
    ? `?level=${encodeURIComponent(requestedLevel!)}`
    : "";

  try {
    const response = await fetch(
      `${baseUrl}/api/live-crowd-conditions${level}`,
      {
        cache: "no-store",
        signal: AbortSignal.timeout(12_000),
        headers: {
          Accept: "application/json",
          "ngrok-skip-browser-warning": "true",
        },
      },
    );
    const body = await response.json();
    return NextResponse.json(body, { status: response.status });
  } catch {
    return NextResponse.json(
      { detail: "Unable to reach the CityFlow API." },
      { status: 502 },
    );
  }
}
