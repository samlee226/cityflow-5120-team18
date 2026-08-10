import { NextResponse } from "next/server";

import { proxyToBackend } from "@/app/lib/backend";

export const dynamic = "force-dynamic";

const ROUTE_TIMEOUT_MS = 75_000;

export async function POST(request: Request) {
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return NextResponse.json(
      { detail: "A JSON body is required." },
      { status: 400 },
    );
  }

  try {
    const { body, status } = await proxyToBackend("/api/routes/low-crowd", {
      method: "POST",
      body: JSON.stringify(payload),
      timeoutMs: ROUTE_TIMEOUT_MS,
    });
    return NextResponse.json(body, { status });
  } catch {
    return NextResponse.json(
      { detail: "Unable to reach the CityFlow API." },
      { status: 502 },
    );
  }
}
