import { NextResponse } from "next/server";

import { proxyToBackend } from "@/app/lib/backend";

export const dynamic = "force-dynamic";

const ALLOWED_LEVELS = ["low", "medium", "high"];

export async function GET(request: Request) {
  const requestedLevel = new URL(request.url).searchParams.get("level");
  const level = ALLOWED_LEVELS.includes(requestedLevel ?? "")
    ? `?level=${encodeURIComponent(requestedLevel!)}`
    : "";

  try {
    const { body, status } = await proxyToBackend(
      `/api/live-crowd-conditions${level}`,
      { timeoutMs: 12_000 },
    );
    return NextResponse.json(body, { status });
  } catch {
    return NextResponse.json(
      { detail: "Unable to reach the CityFlow API." },
      { status: 502 },
    );
  }
}
