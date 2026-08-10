"use client";

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

type Point = { lat: number; lng: number } | { lat(): number; lng(): number };
type MapClickEvent = { latLng?: { lat(): number; lng(): number } };
type MapListener = { remove(): void };
type MapInstance = {
  fitBounds(bounds: unknown): void;
  addListener(
    event: string,
    handler: (event: MapClickEvent) => void,
  ): MapListener;
};
type PolylineInstance = { setMap(map: MapInstance | null): void };
type RouteLine = { id: "a" | "b" | "c"; line: PolylineInstance };
type CircleInstance = {
  setMap(map: MapInstance | null): void;
  addListener(event: string, handler: () => void): MapListener;
};
type RouteResult = {
  path?: Point[];
  distanceMeters?: number;
  durationMillis?: number;
  legs?: { steps?: { instructions?: string; distanceMeters?: number }[] }[];
};
type PositionMarker = {
  map: MapInstance | null;
  position: { lat: number; lng: number };
};
type MarkerLibrary = {
  AdvancedMarkerElement: new (options: {
    map: MapInstance;
    position: { lat: number; lng: number };
    title: string;
  }) => PositionMarker;
};
type MapsLibrary = {
  Map: new (
    element: HTMLElement,
    options: Record<string, unknown>,
  ) => MapInstance;
  Polyline: new (options: Record<string, unknown>) => PolylineInstance;
  Circle: new (options: Record<string, unknown>) => CircleInstance;
};
type CoreLibrary = { LatLngBounds: new () => { extend(point: Point): void } };
type GeocoderLibrary = {
  Geocoder: new () => {
    geocode(request: { address: string }): Promise<{
      results: Array<{
        geometry: { location: { lat(): number; lng(): number } };
      }>;
    }>;
  };
};
type RoutesLibrary = {
  Route: {
    computeRoutes(
      request: Record<string, unknown>,
    ): Promise<{ routes?: RouteResult[] }>;
  };
};
type Coordinate = { lat: number; lng: number };
type NearestNodeResponse = {
  node_id: number;
  distance_m: number;
  within_threshold: boolean;
  threshold_m: number;
};
type LowCrowdRouteResponse = {
  start_node: number;
  end_node: number;
  total_cost?: number;
  total_distance?: number;
  total_distance_m?: number;
  node_ids?: number[];
  coords?: unknown;
  crowd_score?: number;
  crowd_coverage_ratio?: number;
  crowd_data_status?: string;
  sensory_friendly_radius_m?: number;
  route_geometry?: unknown;
};
type BackendErrorResponse = { detail?: string };
type GoogleMaps = { importLibrary(name: string): Promise<unknown> };
declare global {
  interface Window {
    google?: { maps: GoogleMaps };
    __cityFlowMaps?: Promise<GoogleMaps>;
  }
}

const key = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY;
// Backend requests go to same-origin route handlers under /api, which forward
// them server-side. Keeping the browser on its own origin avoids mixed-content
// blocking and removes the need for cross-origin headers.

function parseCoordinate(value: string): Coordinate | null {
  const parts = value.split(",").map((part) => part.trim());
  if (parts.length !== 2 || parts.some((part) => part === "")) return null;
  const lat = Number(parts[0]);
  const lng = Number(parts[1]);
  if (
    !Number.isFinite(lat) ||
    !Number.isFinite(lng) ||
    lat < -90 ||
    lat > 90 ||
    lng < -180 ||
    lng > 180
  )
    return null;
  return { lat, lng };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function normalizeRouteCoordinates(value: unknown): Coordinate[] | null {
  let candidate = value;
  if (
    isRecord(candidate) &&
    candidate.type === "LineString" &&
    Array.isArray(candidate.coordinates)
  )
    candidate = candidate.coordinates;
  if (!Array.isArray(candidate) || candidate.length < 2) return null;

  const points: Coordinate[] = [];
  for (const item of candidate) {
    if (
      Array.isArray(item) &&
      item.length >= 2 &&
      typeof item[0] === "number" &&
      typeof item[1] === "number"
    ) {
      points.push({ lng: item[0], lat: item[1] });
      continue;
    }
    if (isRecord(item) && typeof item.lat === "number") {
      const longitude =
        typeof item.lng === "number"
          ? item.lng
          : typeof item.lon === "number"
            ? item.lon
            : null;
      if (longitude !== null) {
        points.push({ lat: item.lat, lng: longitude });
        continue;
      }
    }
    return null;
  }
  return points.every(
    ({ lat, lng }) =>
      Number.isFinite(lat) &&
      Number.isFinite(lng) &&
      lat >= -90 &&
      lat <= 90 &&
      lng >= -180 &&
      lng <= 180,
  )
    ? points
    : null;
}

function routeDistanceMeters(path: Point[]): number {
  const radians = (degrees: number) => (degrees * Math.PI) / 180;
  const coordinate = (point: Point): Coordinate => ({
    lat: typeof point.lat === "function" ? point.lat() : point.lat,
    lng: typeof point.lng === "function" ? point.lng() : point.lng,
  });
  let distance = 0;
  for (let index = 1; index < path.length; index += 1) {
    const previous = coordinate(path[index - 1]);
    const current = coordinate(path[index]);
    const latitudeDelta = radians(current.lat - previous.lat);
    const longitudeDelta = radians(current.lng - previous.lng);
    const value =
      Math.sin(latitudeDelta / 2) ** 2 +
      Math.cos(radians(previous.lat)) *
        Math.cos(radians(current.lat)) *
        Math.sin(longitudeDelta / 2) ** 2;
    distance += 6_371_000 * 2 * Math.atan2(Math.sqrt(value), Math.sqrt(1 - value));
  }
  return distance;
}

function pointCoordinate(point: Point): Coordinate {
  return {
    lat: typeof point.lat === "function" ? point.lat() : point.lat,
    lng: typeof point.lng === "function" ? point.lng() : point.lng,
  };
}

function pointDistanceMeters(first: Point, second: Point): number {
  return routeDistanceMeters([pointCoordinate(first), pointCoordinate(second)]);
}

/** Match routes on the same street corridor even when vertex counts differ. */
function routesAreEquivalent(first?: Point[] | null, second?: Point[] | null) {
  if (!first?.length || !second?.length) return false;
  const firstDistance = routeDistanceMeters(first);
  const secondDistance = routeDistanceMeters(second);
  if (
    Math.abs(firstDistance - secondDistance) /
      Math.max(firstDistance, secondDistance, 1) >
    0.12
  )
    return false;

  const followsSameCorridor = (source: Point[], target: Point[]) => {
    const stride = Math.max(1, Math.floor(source.length / 12));
    return source
      .filter((_, index) => index % stride === 0 || index === source.length - 1)
      .every((point) =>
        target.some((candidate) => pointDistanceMeters(point, candidate) <= 35),
      );
  };
  return followsSameCorridor(first, second) && followsSameCorridor(second, first);
}

async function readBackendResponse<T>(response: Response): Promise<T> {
  const body = (await response.json().catch(() => null)) as
    | T
    | BackendErrorResponse
    | null;
  if (!response.ok) {
    const errorBody: unknown = body;
    const detail = isRecord(errorBody) ? errorBody.detail : null;
    throw new Error(
      typeof detail === "string"
        ? detail
        : `The routing API returned ${response.status}.`,
    );
  }
  if (!body) throw new Error("The routing API returned an empty response.");
  return body as T;
}

async function resolveCoordinate(
  value: string,
  googleMaps: GoogleMaps,
): Promise<Coordinate | null> {
  const coordinate = parseCoordinate(value);
  if (coordinate) return coordinate;
  const { Geocoder } = (await googleMaps.importLibrary(
    "geocoding",
  )) as GeocoderLibrary;
  const response = await new Geocoder().geocode({ address: value });
  const location = response.results[0]?.geometry.location;
  return location ? { lat: location.lat(), lng: location.lng() } : null;
}

export type CrowdDensityPoint = {
  id: string;
  location: string;
  lat: number;
  lng: number;
  level: "low" | "moderate" | "high";
  pedestrianCount?: number;
  updatedAt?: string;
  crowdRatio?: number;
  source?: "live" | "historical" | "none";
  dataStatus?: "fresh" | "stale" | "no_data";
};
export type CalculatedRouteMetric = {
  distanceMeters: number;
  estimatedMinutes: number;
};
export type CalculatedRouteMetrics = {
  lowCrowd?: CalculatedRouteMetric;
  lowCrowdPending?: boolean;
  alternative?: CalculatedRouteMetric;
  shortest?: CalculatedRouteMetric;
};
const EMPTY_CROWD_DATA: CrowdDensityPoint[] = [];

export function loadGoogleMaps() {
  if (window.google?.maps) return Promise.resolve(window.google.maps);
  if (window.__cityFlowMaps) return window.__cityFlowMaps;
  window.__cityFlowMaps = new Promise((resolve, reject) => {
    const callback = `cityFlowMapsReady_${Date.now()}`;
    const target = window as unknown as Record<string, unknown>;
    target[callback] = () => {
      delete target[callback];
      if (window.google?.maps) {
        resolve(window.google.maps);
      } else {
        reject(new Error("Google Maps did not initialise."));
      }
    };
    const script = document.createElement("script");
    // Route.computeRoutes is exposed by the Maps JavaScript beta channel.
    script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key ?? "")}&loading=async&v=beta&callback=${callback}`;
    script.async = true;
    script.onerror = () => reject(new Error("Google Maps failed to load."));
    document.head.appendChild(script);
  });
  return window.__cityFlowMaps;
}

export default function GoogleRouteMap({
  origin,
  destination,
  selectedRouteId = "a",
  onRoutesCalculated,
  crowdData = EMPTY_CROWD_DATA,
}: {
  origin: string;
  destination: string;
  selectedRouteId?: "a" | "b" | "c";
  onRoutesCalculated?: (metrics: CalculatedRouteMetrics) => void;
  crowdData?: CrowdDensityPoint[];
}) {
  const reduceMotion = useReducedMotion();
  const container = useRef<HTMLDivElement>(null),
    map = useRef<MapInstance | null>(null),
    lines = useRef<RouteLine[]>([]),
    routeMetricsHandler = useRef(onRoutesCalculated),
    watchId = useRef<number | null>(null),
    positionMarker = useRef<PositionMarker | null>(null),
    layerCircles = useRef<CircleInstance[]>([]),
    layerListeners = useRef<MapListener[]>([]),
    sensoryRouteRef = useRef(true),
    selectedRouteRef = useRef(selectedRouteId);
  const [navigating, setNavigating] = useState(false),
    [instruction, setInstruction] = useState(
      "Route ready — start navigation when you are at the starting point.",
    );
  const [status, setStatus] = useState(
    key
      ? "Loading Google Maps…"
      : "Add a Google Maps API key to enable live routes.",
  );
  const [mapReady, setMapReady] = useState(false);
  const [sensoryRoute, setSensoryRoute] = useState(true);
  const [crowdHeatMap, setCrowdHeatMap] = useState(false);
  const [selectedCrowdArea, setSelectedCrowdArea] =
    useState<CrowdDensityPoint | null>(null);
  useEffect(() => {
    routeMetricsHandler.current = onRoutesCalculated;
  }, [onRoutesCalculated]);
  useEffect(() => {
    if (!key || !container.current) return;
    let cancelled = false;
    async function draw() {
      try {
        setStatus("Finding walking routes…");
        const googleMaps = await loadGoogleMaps();
        const [{ Map, Polyline }, { LatLngBounds }] = await Promise.all([
          googleMaps.importLibrary("maps") as Promise<MapsLibrary>,
          googleMaps.importLibrary("core") as Promise<CoreLibrary>,
        ]);
        if (cancelled || !container.current) return;
        if (!map.current)
          map.current = new Map(container.current, {
            center: { lat: -37.8136, lng: 144.9631 },
            zoom: 15,
            mapTypeControl: false,
            streetViewControl: false,
            fullscreenControl: false,
            gestureHandling: "cooperative",
            mapId: "DEMO_MAP_ID",
          });
        setMapReady(true);
        lines.current.forEach(({ line }) => line.setMap(null));
        lines.current = [];
        let startCoordinate = parseCoordinate(origin);
        let endCoordinate = parseCoordinate(destination);

        if (!startCoordinate || !endCoordinate) {
          setStatus("Locating your start and destination…");
          [startCoordinate, endCoordinate] = await Promise.all([
            resolveCoordinate(origin, googleMaps),
            resolveCoordinate(destination, googleMaps),
          ]);
          if (!startCoordinate || !endCoordinate)
            throw new Error(
              "Google Maps could not locate the start or destination.",
            );
        }

        if (startCoordinate && endCoordinate) {
          setStatus("Finding nearest walking network nodes…");
          const nearestNodeUrl = (point: Coordinate) => {
            const params = new URLSearchParams({
              lat: String(point.lat),
              lon: String(point.lng),
            });
            return `/api/network/nearest-node?${params.toString()}`;
          };
          const nearestRequest = (point: Coordinate) =>
            fetch(nearestNodeUrl(point), {
              signal: AbortSignal.timeout(12_000),
              headers: { Accept: "application/json" },
            });
          const [startResponse, endResponse] = await Promise.all([
            nearestRequest(startCoordinate),
            nearestRequest(endCoordinate),
          ]);
          const [startNode, endNode] = await Promise.all([
            readBackendResponse<NearestNodeResponse>(startResponse),
            readBackendResponse<NearestNodeResponse>(endResponse),
          ]);
          if (!startNode.within_threshold)
            throw new Error(
              `The start is ${Math.round(startNode.distance_m)} m from the walking network, outside the ${startNode.threshold_m} m limit.`,
            );
          if (!endNode.within_threshold)
            throw new Error(
              `The destination is ${Math.round(endNode.distance_m)} m from the walking network, outside the ${endNode.threshold_m} m limit.`,
            );

          setStatus("Calculating shortest and low-crowd routes…");
          const requestBackendRoute = async (endpoint: string) => {
            const response = await fetch(endpoint, {
              method: "POST",
              signal: AbortSignal.timeout(75_000),
              headers: {
                Accept: "application/json",
                "Content-Type": "application/json",
              },
              body: JSON.stringify({
                start_node: startNode.node_id,
                end_node: endNode.node_id,
              }),
            });
            return readBackendResponse<LowCrowdRouteResponse>(response);
          };
          const backendResults = Promise.allSettled([
            requestBackendRoute("/api/routes/low-crowd"),
            requestBackendRoute("/api/routes"),
          ]);
          let googleRoutes: RouteResult[] = [];
          try {
            const { Route } = (await googleMaps.importLibrary(
              "routes",
            )) as RoutesLibrary;
            const alternatives = await Route.computeRoutes({
              origin: startCoordinate,
              destination: endCoordinate,
              travelMode: "WALKING",
              computeAlternativeRoutes: true,
              fields: ["path", "distanceMeters", "durationMillis"],
            });
            googleRoutes =
              alternatives.routes?.filter((route) => route.path?.length) ?? [];
          } catch {
            googleRoutes = [];
          }
          const googleShortest = googleRoutes[0];
          // Do not present Google's primary route twice when no alternative exists.
          const alternativeRoute = googleRoutes[1];
          const googleMetric = (
            route: RouteResult | undefined,
          ): CalculatedRouteMetric | undefined =>
            route?.path?.length
              ? {
                  distanceMeters:
                    route.distanceMeters ?? routeDistanceMeters(route.path),
                  estimatedMinutes: route.durationMillis
                    ? Math.max(1, Math.ceil(route.durationMillis / 60_000))
                    : Math.max(
                        1,
                        Math.ceil(
                          (route.distanceMeters ??
                            routeDistanceMeters(route.path)) / 80,
                        ),
                      ),
                }
              : undefined;
          routeMetricsHandler.current?.({
            alternative: googleMetric(alternativeRoute),
            lowCrowdPending: true,
            shortest: googleMetric(googleShortest),
          });
          if (map.current && (googleShortest?.path || alternativeRoute?.path)) {
            const preliminaryBounds = new LatLngBounds();
            googleShortest?.path?.forEach((point) =>
              preliminaryBounds.extend(point),
            );
            alternativeRoute?.path?.forEach((point) =>
              preliminaryBounds.extend(point),
            );
            if (googleShortest?.path)
              lines.current.push({
                id: "c",
                line: new Polyline({
                  path: googleShortest.path,
                  map:
                    sensoryRouteRef.current && selectedRouteRef.current === "c"
                      ? map.current
                      : null,
                  strokeColor: "#dc4c56",
                  strokeOpacity: 0.95,
                  strokeWeight: 6,
                  zIndex: 30,
                }),
              });
            if (alternativeRoute?.path)
              lines.current.push({
                id: "b",
                line: new Polyline({
                  path: alternativeRoute.path,
                  map:
                    sensoryRouteRef.current && selectedRouteRef.current === "b"
                      ? map.current
                      : null,
                  strokeColor: "#ed9b32",
                  strokeOpacity: 0.95,
                  strokeWeight: 6,
                  zIndex: 30,
                }),
              });
            map.current.fitBounds(preliminaryBounds);
            setStatus("Standard routes ready; calculating low-crowd route…");
          }
          const [lowCrowdResult, shortestResult] = await backendResults;
          lines.current.forEach(({ line }) => line.setMap(null));
          lines.current = [];
          const lowCrowdRoute =
            lowCrowdResult.status === "fulfilled"
              ? lowCrowdResult.value
              : null;
          const shortestRoute =
            shortestResult.status === "fulfilled" ? shortestResult.value : null;
          const lowCrowdPath = normalizeRouteCoordinates(
            lowCrowdRoute?.coords ?? lowCrowdRoute?.route_geometry,
          );
          const shortestPath = normalizeRouteCoordinates(
            shortestRoute?.coords ?? shortestRoute?.route_geometry,
          );
          // Build comparison choices from genuinely different corridors. Route A is
          // always the backend low-crowd route; C is the shortest distinct route;
          // B is the next distinct Google/backend alternative when one exists.
          const comparisonCandidates = [
            ...(shortestPath
              ? [{ path: shortestPath as Point[], metric: undefined as CalculatedRouteMetric | undefined }]
              : []),
            ...googleRoutes
              .filter((candidate) => candidate.path?.length)
              .map((candidate) => ({
                path: candidate.path!,
                metric: googleMetric(candidate),
              })),
          ].filter(
            (candidate, index, candidates) =>
              !routesAreEquivalent(candidate.path, lowCrowdPath) &&
              candidates.findIndex((other) =>
                routesAreEquivalent(candidate.path, other.path),
              ) === index,
          );
          comparisonCandidates.sort(
            (first, second) =>
              (first.metric?.distanceMeters ?? routeDistanceMeters(first.path)) -
              (second.metric?.distanceMeters ?? routeDistanceMeters(second.path)),
          );
          const shortestComparison = comparisonCandidates[0];
          const alternativeComparison = comparisonCandidates[1];
          const comparisonPath = shortestComparison?.path;
          const alternativePath = alternativeComparison?.path;
          if (!lowCrowdPath && !comparisonPath && !alternativePath?.length) {
            const lowCrowdError =
              lowCrowdResult.status === "rejected" &&
              lowCrowdResult.reason instanceof Error
                ? lowCrowdResult.reason.message
                : "No supported low-crowd geometry was returned.";
            throw new Error(
              `The backend routes could not be displayed. ${lowCrowdError}`,
            );
          }
          if (cancelled || !map.current) return;

          const bounds = new LatLngBounds();
          comparisonPath?.forEach((point) => bounds.extend(point));
          lowCrowdPath?.forEach((point) => bounds.extend(point));
          alternativePath?.forEach((point) => bounds.extend(point));
          if (comparisonPath)
            lines.current.push(
              {
                id: "c",
                line: new Polyline({
                  path: comparisonPath,
                  map:
                    sensoryRouteRef.current && selectedRouteRef.current === "c"
                      ? map.current
                      : null,
                  strokeColor: "#dc4c56",
                  strokeOpacity: 0.95,
                  strokeWeight: 6,
                  zIndex: 30,
                }),
              },
            );
          if (alternativePath?.length)
            lines.current.push({
              id: "b",
              line: new Polyline({
                path: alternativePath,
                map:
                  sensoryRouteRef.current && selectedRouteRef.current === "b"
                    ? map.current
                    : null,
                strokeColor: "#ed9b32",
                strokeOpacity: 0.95,
                strokeWeight: 6,
                zIndex: 30,
              }),
            });
          if (lowCrowdPath)
            lines.current.push(
              {
                id: "a",
                line: new Polyline({
                  path: lowCrowdPath,
                  map:
                    sensoryRouteRef.current && selectedRouteRef.current === "a"
                      ? map.current
                      : null,
                  strokeColor: "#3b9e73",
                  strokeOpacity: 0.95,
                  strokeWeight: 6,
                  zIndex: 30,
                }),
              },
            );
          map.current.fitBounds(bounds);
          setStatus(
            lowCrowdPath && comparisonPath && alternativePath?.length
              ? "Three route options ready"
              : lowCrowdPath && comparisonPath
                ? "Low-crowd and high-sensory comparison routes ready"
              : lowCrowdPath
                ? "Low-crowd route ready"
                : "Shortest route ready; the low-crowd route was unavailable",
          );
          const routeDistance =
            lowCrowdRoute?.total_distance ?? lowCrowdRoute?.total_distance_m;
          const metricFor = (
            backendRoute: LowCrowdRouteResponse | null,
            path: Point[] | null | undefined,
          ): CalculatedRouteMetric | undefined => {
            const distance =
              backendRoute?.total_distance ??
              backendRoute?.total_distance_m ??
              (path?.length ? routeDistanceMeters(path) : undefined);
            return typeof distance === "number" && distance > 0
              ? {
                  distanceMeters: distance,
                  estimatedMinutes: Math.max(1, Math.ceil(distance / 80)),
                }
              : undefined;
          };
          routeMetricsHandler.current?.({
            lowCrowd: metricFor(lowCrowdRoute, lowCrowdPath),
            lowCrowdPending: false,
            alternative: alternativeComparison
              ? alternativeComparison.metric ??
                metricFor(null, alternativeComparison.path)
              : undefined,
            shortest: shortestComparison
              ? shortestComparison.metric ??
                metricFor(
                  shortestComparison.path === shortestPath ? shortestRoute : null,
                  shortestComparison.path,
                )
              : undefined,
          });
          setInstruction(
            routeDistance
              ? `Follow the low-crowd route · ${Math.round(routeDistance)} m`
              : "Follow the highlighted low-crowd route.",
          );
          return;
        }
        setStatus("Calculating walking routes…");
        const { Route } = (await googleMaps.importLibrary(
          "routes",
        )) as RoutesLibrary;
        if (!Route?.computeRoutes) {
          throw new Error(
            "Google walking routes are unavailable. Enable the Routes API for this key.",
          );
        }
        lines.current.forEach(({ line }) => line.setMap(null));
        lines.current = [];
        const response = await Route.computeRoutes({
          origin,
          destination,
          travelMode: "WALKING",
          computeAlternativeRoutes: true,
          fields: [
            "path",
            "distanceMeters",
            "durationMillis",
            "legs.steps.instructions",
            "legs.steps.distanceMeters",
          ],
        });
        if (cancelled || !map.current) return;
        const found =
          response.routes?.filter((route) => route.path?.length) ?? [];
        if (!found.length)
          throw new Error("No walking route was found for those places.");
        const bounds = new LatLngBounds();
        found.slice(0, 3).forEach((route, index) => {
          route.path?.forEach((point) => bounds.extend(point));
          const line = new Polyline({
            path: route.path,
            map: sensoryRouteRef.current ? map.current : null,
            strokeColor: index === 0 ? "#3b9e73" : "#386fc5",
            strokeOpacity: index === 0 ? 0.95 : 0.5,
            strokeWeight: index === 0 ? 7 : 5,
            zIndex: 30 - index,
          });
          lines.current.push({
            id: (["a", "b", "c"] as const)[index] ?? "c",
            line,
          });
        });
        map.current.fitBounds(bounds);
        setStatus(
          `${found.length} live walking route${found.length === 1 ? "" : "s"} found`,
        );
        const firstStep = found[0].legs?.[0]?.steps?.[0];
        if (firstStep?.instructions)
          setInstruction(
            `${firstStep.instructions}${firstStep.distanceMeters ? ` · ${firstStep.distanceMeters} m` : ""}`,
          );
      } catch (error) {
        setStatus(
          error instanceof Error
            ? error.message
            : "Unable to calculate this route.",
        );
      }
    }
    void draw();
    return () => {
      cancelled = true;
    };
  }, [origin, destination]);
  useEffect(() => {
    sensoryRouteRef.current = sensoryRoute;
    selectedRouteRef.current = selectedRouteId;
    lines.current.forEach(({ id, line }) =>
      line.setMap(sensoryRoute && id === selectedRouteId ? map.current : null),
    );
  }, [sensoryRoute, selectedRouteId]);
  useEffect(() => {
    if (!mapReady || !map.current) return;
    let cancelled = false;
    async function renderLayers() {
      const googleMaps = await loadGoogleMaps();
      const { Circle } = (await googleMaps.importLibrary(
        "maps",
      )) as MapsLibrary;
      if (cancelled || !map.current) return;
      layerListeners.current.forEach((item) => item.remove());
      layerCircles.current.forEach((item) => item.setMap(null));
      layerListeners.current = [];
      layerCircles.current = [];
      if (crowdHeatMap) {
        const colours = {
          low: { fill: "#38a169", stroke: "#247a4c" },
          moderate: { fill: "#ed9b32", stroke: "#a85b08" },
          high: { fill: "#dc4c56", stroke: "#9f2730" },
        };
        crowdData.forEach((point) => {
          const colour = colours[point.level];
          const circle = new Circle({
            map: map.current,
            center: { lat: point.lat, lng: point.lng },
            radius:
              point.level === "high"
                ? 125
                : point.level === "moderate"
                  ? 100
                  : 78,
            fillColor: colour.fill,
            fillOpacity: 0.28,
            strokeColor: colour.stroke,
            strokeOpacity: 0.78,
            strokeWeight: 2,
            clickable: true,
            zIndex: 5,
          });
          layerListeners.current.push(
            circle.addListener("mouseover", () => setSelectedCrowdArea(point)),
            circle.addListener("click", () => setSelectedCrowdArea(point)),
          );
          layerCircles.current.push(circle);
        });
      }
    }
    void renderLayers();
    return () => {
      cancelled = true;
      layerListeners.current.forEach((item) => item.remove());
      layerCircles.current.forEach((item) => item.setMap(null));
    };
  }, [mapReady, crowdHeatMap, crowdData]);
  useEffect(
    () => () => {
      if (watchId.current !== null)
        navigator.geolocation.clearWatch(watchId.current);
      if (positionMarker.current) positionMarker.current.map = null;
    },
    [],
  );
  async function startNavigation() {
    if (!navigator.geolocation) {
      setStatus("This browser does not support live location.");
      return;
    }
    setStatus("Waiting for location permission…");
    const googleMaps = await loadGoogleMaps();
    const { AdvancedMarkerElement } = (await googleMaps.importLibrary(
      "marker",
    )) as MarkerLibrary;
    watchId.current = navigator.geolocation.watchPosition(
      (position) => {
        const point = {
          lat: position.coords.latitude,
          lng: position.coords.longitude,
        };
        if (map.current) {
          if (positionMarker.current) {
            positionMarker.current.position = point;
          } else {
            positionMarker.current = new AdvancedMarkerElement({
              map: map.current,
              position: point,
              title: "Your live location",
            });
          }
        }
        setNavigating(true);
        setStatus(
          `GPS active · accuracy ${Math.round(position.coords.accuracy)} m`,
        );
      },
      (error) => {
        setNavigating(false);
        setStatus(
          error.code === 1
            ? "Location permission was denied."
            : "Your live location is unavailable.",
        );
      },
      { enableHighAccuracy: true, maximumAge: 3000, timeout: 15000 },
    );
  }
  function stopNavigation() {
    if (watchId.current !== null) {
      navigator.geolocation.clearWatch(watchId.current);
      watchId.current = null;
    }
    if (positionMarker.current) {
      positionMarker.current.map = null;
      positionMarker.current = null;
    }
    setNavigating(false);
    setStatus("Navigation stopped.");
  }
  if (!key)
    return (
      <div className="map-visual map-setup">
        <div>
          <strong>Google Maps is ready to connect</strong>
          <p>
            Add <code>NEXT_PUBLIC_GOOGLE_MAPS_API_KEY</code> to{" "}
            <code>.env.local</code>, then restart the development server.
          </p>
        </div>
      </div>
    );
  const mapsUrl = `https://www.google.com/maps/dir/?api=1&origin=${encodeURIComponent(origin)}&destination=${encodeURIComponent(destination)}&travelmode=walking`;
  const loading =
    status.includes("Loading") ||
    status.includes("Locating") ||
    status.includes("Finding") ||
    status.includes("Calculating") ||
    status.includes("Waiting") ||
    status.includes("Updating");
  return (
    <div className="google-map-shell">
      <div
        ref={container}
        className="google-map"
        aria-label={`Walking routes from ${origin} to ${destination}`}
      />
      <details className="map-layers-control">
        <summary>Map layers</summary>
        <div className="map-layer-list">
          <LayerToggle
            label="Sensory route"
            checked={sensoryRoute}
            onChange={setSensoryRoute}
            tone="route"
          />
          <LayerToggle
            label="Crowd heat map"
            checked={crowdHeatMap}
            onChange={setCrowdHeatMap}
            tone="crowd"
          />
          {crowdHeatMap && crowdData.length === 0 && (
            <p className="layer-data-note">Awaiting pedestrian-count data</p>
          )}
        </div>
      </details>
      <motion.div
        initial={reduceMotion ? false : { opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        className="navigation-overlay"
      >
        <motion.span
          animate={
            navigating && !reduceMotion ? { scale: [1, 1.06, 1] } : undefined
          }
          transition={{ duration: 2, repeat: Infinity }}
          className="turn-icon"
        >
          ↑
        </motion.span>
        <div>
          <small>
            {navigating ? "LIVE WALKING NAVIGATION" : "NEXT DIRECTION"}
          </small>
          <AnimatePresence mode="wait" initial={false}>
            <motion.strong
              key={instruction}
              initial={reduceMotion ? false : { opacity: 0, x: 5 }}
              animate={{ opacity: 1, x: 0 }}
              exit={reduceMotion ? undefined : { opacity: 0, x: -5 }}
              transition={{ duration: reduceMotion ? 0 : 0.22 }}
            >
              {instruction}
            </motion.strong>
          </AnimatePresence>
          <div className="navigation-actions">
            <AnimatePresence mode="wait" initial={false}>
              {navigating ? (
                <motion.button
                  key="stop"
                  initial={reduceMotion ? false : { opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  whileTap={reduceMotion ? undefined : { scale: 0.96 }}
                  onClick={stopNavigation}
                >
                  Stop navigation
                </motion.button>
              ) : (
                <motion.button
                  key="start"
                  initial={reduceMotion ? false : { opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  whileTap={reduceMotion ? undefined : { scale: 0.96 }}
                  onClick={() => void startNavigation()}
                >
                  Start live navigation
                </motion.button>
              )}
            </AnimatePresence>
            <motion.a
              whileHover={reduceMotion ? undefined : { y: -1 }}
              whileTap={reduceMotion ? undefined : { scale: 0.97 }}
              href={mapsUrl}
              target="_blank"
              rel="noreferrer"
            >
              Open in Google Maps
            </motion.a>
          </div>
        </div>
      </motion.div>
      {crowdHeatMap && (
        <div className="crowd-legend" aria-label="Crowd density legend">
          <strong>Crowd density</strong>
          <div className="legend-scale">
            <span className="low">Low</span>
            <span className="moderate">Moderate</span>
            <span className="high">High</span>
          </div>
        </div>
      )}
      <AnimatePresence>
        {crowdHeatMap && selectedCrowdArea && (
          <motion.aside
            className={`crowd-info-card ${selectedCrowdArea.level}`}
            initial={reduceMotion ? false : { opacity: 0, y: 8, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 6 }}
            aria-live="polite"
          >
            <button
              type="button"
              onClick={() => setSelectedCrowdArea(null)}
              aria-label="Close crowd-density details"
            >
              ×
            </button>
            <small>PEDESTRIAN DENSITY</small>
            <h3>{selectedCrowdArea.location}</h3>
            <p>
              <span>Level</span>
              <strong>
                {selectedCrowdArea.level === "moderate"
                  ? "Moderate"
                  : selectedCrowdArea.level === "high"
                    ? "High"
                    : "Low"}
              </strong>
            </p>
            <p>
              <span>Crowd ratio</span>
              <strong>
                {selectedCrowdArea.crowdRatio === undefined
                  ? "Not available"
                  : `${selectedCrowdArea.crowdRatio.toFixed(2)}× baseline`}
              </strong>
            </p>
            <p>
              <span>Data quality</span>
              <strong>
                {selectedCrowdArea.dataStatus?.replace("_", " ") ??
                  "Not available"}
              </strong>
            </p>
            <p>
              <span>Observed</span>
              <strong>{selectedCrowdArea.updatedAt ?? "Not available"}</strong>
            </p>
          </motion.aside>
        )}
      </AnimatePresence>
      <AnimatePresence mode="wait" initial={false}>
        <motion.p
          key={status}
          initial={reduceMotion ? false : { opacity: 0, y: 5 }}
          animate={{ opacity: 1, y: 0 }}
          exit={reduceMotion ? undefined : { opacity: 0, y: -4 }}
          transition={{ duration: reduceMotion ? 0 : 0.2 }}
          className={`map-status ${loading ? "is-loading" : ""}`}
          role="status"
        >
          {loading && (
            <motion.span
              className="loading-dot"
              animate={reduceMotion ? undefined : { opacity: [0.35, 1, 0.35] }}
              transition={{ duration: 1.2, repeat: Infinity }}
            />
          )}
          {status}
        </motion.p>
      </AnimatePresence>
    </div>
  );
}

function LayerToggle({
  label,
  checked,
  onChange,
  tone,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  tone: string;
}) {
  return (
    <label className="map-layer-toggle">
      <span className={`layer-dot ${tone}`} aria-hidden="true" />
      <span>{label}</span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="layer-switch" aria-hidden="true" />
    </label>
  );
}
