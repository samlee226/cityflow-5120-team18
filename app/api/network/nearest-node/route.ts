import { NextResponse } from "next/server";

import { proxyToBackend } from "@/app/lib/backend";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const requested = new URL(request.url).searchParams;
  const lat = requested.get("lat");
  const lon = requested.get("lon");

  if (lat === null || lon === null) {
    return NextResponse.json(
      { detail: "lat and lon are required." },
      { status: 400 },
    );
  }

  // Only the parameters the backend accepts are forwarded.
  const params = new URLSearchParams({ lat, lon });
  const threshold = requested.get("threshold_m");
  if (threshold !== null) params.set("threshold_m", threshold);

  try {
    const { body, status } = await proxyToBackend(
      `/api/network/nearest-node?${params.toString()}`,
    );
    return NextResponse.json(body, { status });
  } catch {
    return NextResponse.json(
      { detail: "Unable to reach the CityFlow API." },
      { status: 502 },
    );
  }
}
