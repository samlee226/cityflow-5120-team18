"use client";

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

type Point = { lat: number; lng: number } | { lat(): number; lng(): number };
type MapClickEvent = { latLng?: { lat(): number; lng(): number } };
type MapListener = { remove(): void };
type MapInstance = {
  fitBounds(bounds: unknown): void;
  panTo(point: { lat: number; lng: number }): void;
  setZoom(zoom: number): void;
  addListener(
    event: string,
    handler: (event: MapClickEvent) => void,
  ): MapListener;
};
type PolylineInstance = { setMap(map: MapInstance | null): void };
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
type RoutesLibrary = {
  Route: {
    computeRoutes(
      request: Record<string, unknown>,
    ): Promise<{ routes?: RouteResult[] }>;
  };
};
type GoogleMaps = { importLibrary(name: string): Promise<unknown> };
declare global {
  interface Window {
    google?: { maps: GoogleMaps };
    __cityFlowMaps?: Promise<GoogleMaps>;
  }
}

const key = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY;

export type CrowdDensityPoint = {
  id: string;
  location: string;
  lat: number;
  lng: number;
  level: "low" | "moderate" | "high";
  pedestrianCount?: number;
  updatedAt?: string;
};
export type MapAreaPoint = {
  id: string;
  name: string;
  lat: number;
  lng: number;
};
const EMPTY_CROWD_DATA: CrowdDensityPoint[] = [];
const EMPTY_CONSTRUCTION_DATA: MapAreaPoint[] = [];
const DEFAULT_QUIET_SPACES: MapAreaPoint[] = [
  { id: "flagstaff", name: "Flagstaff Gardens", lat: -37.8105, lng: 144.9544 },
  {
    id: "library",
    name: "State Library forecourt",
    lat: -37.8097,
    lng: 144.9652,
  },
];

function loadGoogleMaps() {
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
    script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key ?? "")}&loading=async&v=weekly&callback=${callback}`;
    script.async = true;
    script.onerror = () => reject(new Error("Google Maps failed to load."));
    document.head.appendChild(script);
  });
  return window.__cityFlowMaps;
}

export default function GoogleRouteMap({
  origin,
  destination,
  onLocationPick,
  crowdData = EMPTY_CROWD_DATA,
  quietSpaces = DEFAULT_QUIET_SPACES,
  constructionAreas = EMPTY_CONSTRUCTION_DATA,
}: {
  origin: string;
  destination: string;
  onLocationPick: (
    kind: "origin" | "destination",
    location: string,
    label: string,
  ) => void;
  crowdData?: CrowdDensityPoint[];
  quietSpaces?: MapAreaPoint[];
  constructionAreas?: MapAreaPoint[];
}) {
  const reduceMotion = useReducedMotion();
  const container = useRef<HTMLDivElement>(null),
    map = useRef<MapInstance | null>(null),
    lines = useRef<PolylineInstance[]>([]),
    listener = useRef<MapListener | null>(null),
    pickHandler = useRef(onLocationPick),
    watchId = useRef<number | null>(null),
    positionMarker = useRef<PositionMarker | null>(null),
    layerCircles = useRef<CircleInstance[]>([]),
    layerListeners = useRef<MapListener[]>([]),
    sensoryRouteRef = useRef(true);
  const [pickMode, setPickMode] = useState<"origin" | "destination">(
    "destination",
  );
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
  const [quietSpacesLayer, setQuietSpacesLayer] = useState(false);
  const [constructionLayer, setConstructionLayer] = useState(false);
  const [selectedCrowdArea, setSelectedCrowdArea] =
    useState<CrowdDensityPoint | null>(null);
  useEffect(() => {
    pickHandler.current = onLocationPick;
  }, [onLocationPick]);
  useEffect(() => {
    if (!key || !container.current) return;
    let cancelled = false;
    async function draw() {
      try {
        setStatus("Finding walking routes…");
        const googleMaps = await loadGoogleMaps();
        const [{ Map, Polyline }, { LatLngBounds }, { Route }] =
          await Promise.all([
            googleMaps.importLibrary("maps") as Promise<MapsLibrary>,
            googleMaps.importLibrary("core") as Promise<CoreLibrary>,
            googleMaps.importLibrary("routes") as Promise<RoutesLibrary>,
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
        lines.current.forEach((line) => line.setMap(null));
        lines.current = [];
        const response = await Route.computeRoutes({
          origin,
          destination,
          travelMode: "WALK",
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
          lines.current.push(line);
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
    lines.current.forEach((line) =>
      line.setMap(sensoryRoute ? map.current : null),
    );
  }, [sensoryRoute]);
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
      if (quietSpacesLayer) {
        quietSpaces.forEach((point) =>
          layerCircles.current.push(
            new Circle({
              map: map.current,
              center: { lat: point.lat, lng: point.lng },
              radius: 42,
              fillColor: "#38a169",
              fillOpacity: 0.34,
              strokeColor: "#247a4c",
              strokeOpacity: 0.9,
              strokeWeight: 2,
              zIndex: 8,
            }),
          ),
        );
      }
      if (constructionLayer) {
        constructionAreas.forEach((point) =>
          layerCircles.current.push(
            new Circle({
              map: map.current,
              center: { lat: point.lat, lng: point.lng },
              radius: 55,
              fillColor: "#ed9b32",
              fillOpacity: 0.3,
              strokeColor: "#a85b08",
              strokeOpacity: 0.9,
              strokeWeight: 2,
              zIndex: 9,
            }),
          ),
        );
      }
    }
    void renderLayers();
    return () => {
      cancelled = true;
      layerListeners.current.forEach((item) => item.remove());
      layerCircles.current.forEach((item) => item.setMap(null));
    };
  }, [
    mapReady,
    crowdHeatMap,
    quietSpacesLayer,
    constructionLayer,
    crowdData,
    quietSpaces,
    constructionAreas,
  ]);
  useEffect(() => {
    if (!map.current) return;
    listener.current?.remove();
    listener.current = map.current.addListener("click", (event) => {
      if (!event.latLng) return;
      const lat = event.latLng.lat(),
        lng = event.latLng.lng();
      const location = `${lat.toFixed(6)},${lng.toFixed(6)}`;
      const label = `Pinned ${pickMode === "origin" ? "start" : "destination"} (${lat.toFixed(4)}, ${lng.toFixed(4)})`;
      pickHandler.current(pickMode, location, label);
      setStatus(
        `${pickMode === "origin" ? "Start" : "Destination"} pinned. Updating routes…`,
      );
    });
    return () => listener.current?.remove();
  }, [pickMode, status]);
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
          map.current.panTo(point);
          map.current.setZoom(18);
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
    status.includes("Finding") ||
    status.includes("Waiting") ||
    status.includes("Updating");
  return (
    <div className="google-map-shell">
      <div
        ref={container}
        className={`google-map pick-${pickMode}`}
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
          <LayerToggle
            label="Quiet spaces"
            checked={quietSpacesLayer}
            onChange={setQuietSpacesLayer}
            tone="quiet"
          />
          <LayerToggle
            label="Construction"
            checked={constructionLayer}
            onChange={setConstructionLayer}
            tone="construction"
          />
          {crowdHeatMap && crowdData.length === 0 && (
            <p className="layer-data-note">Awaiting pedestrian-count data</p>
          )}
          {constructionLayer && constructionAreas.length === 0 && (
            <p className="layer-data-note">No construction data connected</p>
          )}
        </div>
      </details>
      <motion.div
        layout
        className="map-pick-controls"
        aria-label="Choose what to set with a map click"
      >
        <motion.button
          whileTap={reduceMotion ? undefined : { scale: 0.95 }}
          className={pickMode === "origin" ? "active" : ""}
          onClick={() => setPickMode("origin")}
          type="button"
        >
          Set start
        </motion.button>
        <motion.button
          whileTap={reduceMotion ? undefined : { scale: 0.95 }}
          className={pickMode === "destination" ? "active" : ""}
          onClick={() => setPickMode("destination")}
          type="button"
        >
          Set destination
        </motion.button>
      </motion.div>
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
              <span>Latest count</span>
              <strong>
                {selectedCrowdArea.pedestrianCount === undefined
                  ? "Not available"
                  : selectedCrowdArea.pedestrianCount.toLocaleString()}
              </strong>
            </p>
            <p>
              <span>Last updated</span>
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
