"use client";
import { FormEvent, useEffect, useRef, useState } from "react";
import GoogleRouteMap, {
  type CalculatedRouteMetric,
  type CalculatedRouteMetrics,
  type CrowdDensityPoint,
} from "./GoogleRouteMap";
import GooglePlaceInput from "./GooglePlaceInput";
import {
  AnimatePresence,
  motion,
  useMotionValue,
  useReducedMotion,
  useSpring,
} from "framer-motion";

const routes = [
  {
    id: "a",
    name: "Route A",
    label: "Low sensory",
    eta: "18 min",
    distance: "1.4 km",
    risk: "low",
    summary:
      "Expected to be less crowded and have fewer sensory triggers than the faster options.",
    metrics: ["Crowd: low", "Construction: low", "Open space: near"],
    scores: [
      [
        "Lower pedestrian density",
        "Avoids the highest-flow streets near major tram stops.",
        "Low",
        "low",
      ],
      [
        "Fewer construction points",
        "Uses a path with fewer active works and road noise triggers.",
        "1 minor",
        "medium",
      ],
      [
        "More open-space nearby",
        "Passes quiet spaces that can be used as calm pause points.",
        "2 nearby",
        "low",
      ],
    ],
    nav: [
      "Follow Route A calmly",
      "Walk 120 m along Swanston St.",
      "Sensory note: this section is usually quieter before 10:30.",
    ],
  },
  {
    id: "b",
    name: "Route B",
    label: "Walking alternative",
    eta: "20 min",
    distance: "1.6 km",
    risk: "medium",
    summary:
      "A distinct Google walking alternative shown for comparison. It is not crowd-scored by the CityFlow backend.",
    metrics: ["Crowd: not scored", "Route: alternative", "Source: Google"],
    scores: [
      [
        "Distinct walking path",
        "Uses a Google walking alternative when one is available.",
        "Alternative",
        "medium",
      ],
      [
        "Calculated travel estimate",
        "Distance and time update after the route request completes.",
        "Live",
        "medium",
      ],
      [
        "Crowd score unavailable",
        "CityFlow does not currently return a third crowd-weighted route.",
        "Not scored",
        "warning",
      ],
    ],
    nav: [
      "Follow Route B alternative",
      "Follow the highlighted orange walking route.",
      "Sensory note: this alternative is not crowd-scored by CityFlow.",
    ],
  },
  {
    id: "c",
    name: "Route C",
    label: "Fastest but busier",
    eta: "14 min",
    distance: "1.1 km",
    risk: "high",
    summary:
      "This is the shortest route, but it is more likely to include crowding, noise, and construction points.",
    metrics: ["Crowd: high", "Construction: mid", "Open space: limited"],
    scores: [
      [
        "Higher pedestrian density",
        "Cuts through the busiest part of the route area.",
        "High",
        "warning",
      ],
      [
        "More sensory triggers",
        "More tram activity, retail noise, and crossing pressure expected.",
        "More",
        "warning",
      ],
      [
        "Shortest time",
        "Best when speed matters more than sensory load.",
        "14 min",
        "medium",
      ],
    ],
    nav: [
      "Follow Route C only if speed matters",
      "Head south toward Melbourne Central.",
      "Sensory note: expect higher crowding around the next crossing.",
    ],
  },
] as const;
const prefs = ["Avoid crowds"];
const quietBreakPlaces = [
  {
    id: "flagstaff",
    name: "Flagstaff Gardens",
    amenity: "Open space",
    lat: -37.8105,
    lng: 144.9544,
  },
  {
    id: "library",
    name: "State Library forecourt",
    amenity: "Seating nearby",
    lat: -37.8097,
    lng: 144.9652,
  },
] as const;

function distanceBetween(
  first: { lat: number; lng: number },
  second: { lat: number; lng: number },
) {
  const radians = (degrees: number) => (degrees * Math.PI) / 180;
  const latitudeDelta = radians(second.lat - first.lat);
  const longitudeDelta = radians(second.lng - first.lng);
  const value =
    Math.sin(latitudeDelta / 2) ** 2 +
    Math.cos(radians(first.lat)) *
      Math.cos(radians(second.lat)) *
      Math.sin(longitudeDelta / 2) ** 2;
  return 6_371_000 * 2 * Math.atan2(Math.sqrt(value), Math.sqrt(1 - value));
}
function Mark() {
  return (
    <svg viewBox="0 0 32 32">
      <path
        d="M6 22c5 3 15 3 20-1M10 20V9l5-3v14M17 20V12l5 2v6"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
      <circle cx="23" cy="8" r="2.4" fill="currentColor" opacity=".42" />
    </svg>
  );
}
function ClockIcon() {
  return (
    <svg
      className="route-time-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </svg>
  );
}
function Heading({ over, title }: { over: string; title: string }) {
  const reduce = useReducedMotion();
  return (
    <div className="section-heading">
      <p className="eyebrow">{over}</p>
      <AnimatePresence mode="wait" initial={false}>
        <motion.h2
          key={title}
          initial={reduce ? false : { opacity: 0, y: 5 }}
          animate={{ opacity: 1, y: 0 }}
          exit={reduce ? undefined : { opacity: 0, y: -4 }}
          transition={{ duration: reduce ? 0 : 0.22 }}
        >
          {title}
        </motion.h2>
      </AnimatePresence>
    </div>
  );
}
export default function Home() {
  const reduceMotion = useReducedMotion();
  const routeOptionsPanelRef = useRef<HTMLElement>(null);
  const heroPointerX = useMotionValue(0),
    heroPointerY = useMotionValue(0);
  const heroFloatX = useSpring(heroPointerX, {
      stiffness: 28,
      damping: 18,
      mass: 1.1,
    }),
    heroFloatY = useSpring(heroPointerY, {
      stiffness: 28,
      damping: 18,
      mass: 1.1,
    });
  const [routeId, setRouteId] =
      useState<(typeof routes)[number]["id"]>("a"),
    [expandedRouteId, setExpandedRouteId] = useState<
      (typeof routes)[number]["id"] | null
    >(null),
    [routeOptionsPanelHeight, setRouteOptionsPanelHeight] = useState<
      number | null
    >(null),
    [selectedPrefs, setPrefs] = useState<string[]>([]),
    [from, setFrom] = useState(""),
    [to, setTo] = useState(""),
    [journey, setJourney] = useState<{
      origin: string;
      destination: string;
    } | null>(null),
    [, setNotice] = useState(""),
    [crowdData, setCrowdData] = useState<CrowdDensityPoint[]>([]),
    [, setCrowdStatus] = useState("Loading crowd conditions…"),
    [calculatedRoutes, setCalculatedRoutes] =
      useState<CalculatedRouteMetrics | null>(null),
    [routingStatus, setRoutingStatus] = useState("Loading Google Maps…"),
    [routeRequestId, setRouteRequestId] = useState(0);
  const routingIsActive = /loading|finding|locating|calculating|starting/i.test(
    routingStatus,
  );
  const metricForRoute = (id: string): CalculatedRouteMetric | undefined =>
    id === "a"
      ? calculatedRoutes?.lowCrowd
      : id === "b"
        ? calculatedRoutes?.alternative
      : id === "c"
        ? calculatedRoutes?.shortest
        : undefined;
  const baseRoute = routes.find((r) => r.id === routeId) ?? routes[0];
  const selectedMetric = metricForRoute(baseRoute.id);
  const route = selectedMetric
    ? {
        ...baseRoute,
        eta: `≈${selectedMetric.estimatedMinutes} min`,
        distance: `${(selectedMetric.distanceMeters / 1000).toFixed(1)} km`,
      }
    : {
        ...baseRoute,
        eta:
          baseRoute.id === "a" && calculatedRoutes?.lowCrowdPending
            ? "Calculating…"
            : calculatedRoutes || !routingIsActive
              ? "Unavailable"
              : "Calculating…",
        distance: "—",
      };
  const busy = crowdData.some((point) => point.level === "high");
  const quietPlaceDetail = (place: (typeof quietBreakPlaces)[number]) => {
    if (!crowdData.length)
      return {
        amenity: place.amenity,
        crowd: "Unavailable",
        proximity: null,
        isFresh: false,
      };
    const nearest = crowdData.reduce((closest, sensor) =>
      distanceBetween(place, sensor) < distanceBetween(place, closest)
        ? sensor
        : closest,
    );
    const sensorDistance = Math.round(distanceBetween(place, nearest));
    const level =
      nearest.level === "moderate"
        ? "Moderate"
        : `${nearest.level[0].toUpperCase()}${nearest.level.slice(1)}`;
    return {
      amenity: place.amenity,
      crowd: `${level} crowd`,
      proximity: `${sensorDistance} m from nearest reading`,
      isFresh: nearest.dataStatus === "fresh",
    };
  };
  const quietDataNeedsCaution =
    !crowdData.length || crowdData.some((point) => point.dataStatus !== "fresh");
  useEffect(() => {
    if (!journey) return;
    const controller = new AbortController();
    async function loadCrowdConditions() {
      setCrowdStatus("Loading crowd conditions…");
      try {
        const response = await fetch("/api/crowd-conditions", {
          signal: controller.signal,
          cache: "no-store",
        });
        const payload = (await response.json()) as {
          generated_at?: string;
          detail?: string;
          conditions?: Array<{
            sensor_id: number;
            sensor_name: string;
            latitude: number;
            longitude: number;
            source: "live" | "historical" | "none";
            crowd_ratio: number | null;
            crowd_level: "low" | "medium" | "high" | null;
            observed_at: string | null;
            live_status: "fresh" | "stale" | "no_data";
          }>;
        };
        if (!response.ok)
          throw new Error(payload.detail || "Crowd conditions are unavailable.");
        const points = (payload.conditions ?? [])
          .filter(
            (item) =>
              Number.isFinite(item.latitude) &&
              Number.isFinite(item.longitude) &&
              item.crowd_level !== null &&
              item.crowd_ratio !== null,
          )
          .map((item) => ({
            id: String(item.sensor_id),
            location: item.sensor_name,
            lat: item.latitude,
            lng: item.longitude,
            level: item.crowd_level === "medium" ? "moderate" : item.crowd_level!,
            crowdRatio: item.crowd_ratio!,
            source: item.source,
            dataStatus: item.live_status,
            updatedAt: item.observed_at
              ? new Date(item.observed_at).toLocaleString("en-AU", {
                  dateStyle: "medium",
                  timeStyle: "short",
                })
              : undefined,
          })) satisfies CrowdDensityPoint[];
        setCrowdData(points);
        setCrowdStatus(
          points.length
            ? `${points.length} sensor${points.length === 1 ? "" : "s"} updated`
            : "No crowd observations are currently available.",
        );
      } catch (error) {
        if (controller.signal.aborted) return;
        setCrowdData([]);
        setCrowdStatus(
          error instanceof Error
            ? error.message
            : "Crowd conditions are unavailable.",
        );
      }
    }
    void loadCrowdConditions();
    return () => controller.abort();
  }, [journey]);
  const select = (id: (typeof routes)[number]["id"]) => {
    setRouteId(id);
    setNotice("");
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const origin = from.trim(),
      destination = to.trim();
    if (!origin || !destination) {
      setNotice("Enter both a starting point and destination.");
      return;
    }
    if (origin === destination) {
      setNotice("Choose two different places for your journey.");
      return;
    }
    setCalculatedRoutes(null);
    setRoutingStatus("Starting route request…");
    setRouteRequestId((current) => current + 1);
    setJourney({ origin, destination });
    setNotice(`Live route requested for ${origin} to ${destination}.`);
    window.requestAnimationFrame(() =>
      document.querySelector("#options")?.scrollIntoView({ behavior: "smooth" }),
    );
  };
  const reveal = reduceMotion
    ? {}
    : {
        initial: { opacity: 0, y: 18 },
        whileInView: { opacity: 1, y: 0 },
        viewport: { once: true, amount: 0.12 },
        transition: { duration: 0.5, ease: [0.22, 1, 0.36, 1] as const },
  };
  return (
    <>
      <main>
        <motion.section
          className="hero-shell"
          id="planner"
          initial={false}
          onPointerMove={(event) => {
            if (reduceMotion || event.pointerType !== "mouse") return;
            const box = event.currentTarget.getBoundingClientRect();
            heroPointerX.set(
              ((event.clientX - box.left) / box.width - 0.5) * 28,
            );
            heroPointerY.set(
              ((event.clientY - box.top) / box.height - 0.5) * 22,
            );
          }}
          onPointerLeave={() => {
            heroPointerX.set(0);
            heroPointerY.set(0);
          }}
        >
          <motion.nav
            className="hero-nav"
            aria-label="Main navigation"
            initial={reduceMotion ? false : { opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4 }}
          >
            <a href="#planner">Planner</a>
            <a href="#details">Route details</a>
            <a href="#navigation">Navigation</a>
          </motion.nav>
          <motion.div
            className="hero-parallax"
            style={{ x: heroFloatX, y: heroFloatY }}
            aria-hidden="true"
          >
            <span className="hero-orb hero-orb-one" />
            <span className="hero-orb hero-orb-two" />
            <span className="hero-ribbon" />
          </motion.div>
          <div className="intro-panel">
            <div className="hero-brand-lockup">
              <div className="hero-brand-main">
                <motion.span
                  className="hero-brand-mark"
                  initial={{
                    opacity: 0,
                    scale: reduceMotion ? 1 : 0.82,
                    rotate: reduceMotion ? 0 : -7,
                  }}
                  animate={{ opacity: 1, scale: 1, rotate: 0 }}
                  transition={{
                    duration: reduceMotion ? 0 : 0.65,
                    delay: reduceMotion ? 0 : 0.04,
                    ease: [0.22, 1, 0.36, 1],
                  }}
                >
                  <Mark />
                </motion.span>
                <motion.h2
                  initial={{ opacity: 0, y: reduceMotion ? 0 : 18 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{
                    duration: reduceMotion ? 0 : 0.7,
                    delay: reduceMotion ? 0 : 0.08,
                    ease: [0.22, 1, 0.36, 1],
                  }}
                >
                  City Flow
                </motion.h2>
              </div>
              <motion.p
                className="hero-brand-subtitle"
                initial={{ opacity: 0, y: reduceMotion ? 0 : 9 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{
                  duration: reduceMotion ? 0 : 0.5,
                  delay: reduceMotion ? 0 : 0.38,
                }}
              >
                Sensory-Aware Navigation for Melbourne CBD
              </motion.p>
            </div>
            <h1 aria-label="Navigate Melbourne city your own way.">
              {["Navigate Melbourne", "city your own way."].map(
                (line, index) => (
                  <motion.span
                    key={line}
                    initial={{ opacity: 0, y: reduceMotion ? 0 : 28 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{
                      duration: reduceMotion ? 0 : 0.68,
                      delay: reduceMotion ? 0 : 0.58 + index * 0.19,
                      ease: [0.22, 1, 0.36, 1],
                    }}
                  >
                    {line}
                  </motion.span>
                ),
              )}
            </h1>
            <motion.p
              initial={{ opacity: 0, y: reduceMotion ? 0 : 14 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                duration: reduceMotion ? 0 : 0.58,
                delay: reduceMotion ? 0 : 1.22,
                ease: [0.22, 1, 0.36, 1],
              }}
            >
              Compare route options by sensory load, crowding, construction
              risk, and access to nearby quiet spaces. This prototype uses
              session-only inputs so live data can be connected safely.
            </motion.p>
            <motion.div
              className="session-card"
              initial={{ opacity: 0, y: reduceMotion ? 0 : 14 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                duration: reduceMotion ? 0 : 0.55,
                delay: reduceMotion ? 0 : 1.43,
                ease: [0.22, 1, 0.36, 1],
              }}
            >
              <strong>Temporary trip check:</strong> places entered here are
              used only for this route comparison. No login or profile is
              needed.
            </motion.div>
          </div>
          <motion.form
            className="planner-card"
            onSubmit={submit}
            initial={{
              opacity: 0,
              x: reduceMotion ? 0 : 34,
              scale: reduceMotion ? 1 : 0.97,
            }}
            animate={{ opacity: 1, x: 0, scale: 1 }}
            transition={{
              duration: reduceMotion ? 0 : 0.72,
              delay: reduceMotion ? 0 : 1.58,
              ease: [0.22, 1, 0.36, 1],
            }}
            whileHover={reduceMotion ? undefined : { y: -3 }}
          >
            <Heading over="Start a route check" title="Plan with two places" />
            <label>
              <span>From</span>
              <GooglePlaceInput
                value={from}
                onChange={setFrom}
                placeholder="Type a starting address or place"
              />
            </label>
            <label>
              <span>To</span>
              <GooglePlaceInput
                value={to}
                onChange={setTo}
                placeholder="Type a destination address or place"
              />
            </label>
            <fieldset>
              <legend>Sensory preferences</legend>
              <div className="chip-grid">
                {prefs.map((p) => (
                  <motion.button
                    whileHover={
                      reduceMotion ? undefined : { y: -1, scale: 1.015 }
                    }
                    whileTap={reduceMotion ? undefined : { scale: 0.97 }}
                    type="button"
                    className={`chip ${selectedPrefs.includes(p) ? "is-selected" : ""}`}
                    aria-pressed={selectedPrefs.includes(p)}
                    onClick={() =>
                      setPrefs((x) =>
                        x.includes(p) ? x.filter((v) => v !== p) : [...x, p],
                      )
                    }
                    key={p}
                  >
                    {p}
                  </motion.button>
                ))}
              </div>
            </fieldset>
            <motion.button
              whileHover={reduceMotion ? undefined : { y: -2, scale: 1.008 }}
              whileTap={reduceMotion ? undefined : { scale: 0.985 }}
              className="primary-action"
            >
              Compare route options
            </motion.button>
          </motion.form>
        </motion.section>
        <motion.section {...reveal} className="dashboard-grid">
          <section className="map-panel panel">
            <div className="map-toolbar">
              <Heading
                over="Google Maps · Walking"
                title={
                  !journey
                    ? "Plan a route to begin"
                    : route.id === "a"
                    ? "Recommended low-sensory route"
                    : `Selected ${route.label.toLowerCase()} route`
                }
              />
              {journey && <span className="route-badge">{route.name}</span>}
            </div>
            {journey ? (
              <GoogleRouteMap
                origin={journey.origin}
                destination={journey.destination}
                requestId={routeRequestId}
                selectedRouteId={routeId}
                crowdData={crowdData}
                onRoutingStatus={setRoutingStatus}
                onRoutesCalculated={(metrics) =>
                  setCalculatedRoutes((current) => ({
                    ...(current ?? {}),
                    ...metrics,
                  }))
                }
              />
            ) : (
              <div className="feature-empty-state">
                Select a start and destination in the planner to view the map.
              </div>
            )}
          </section>
          <aside
            ref={routeOptionsPanelRef}
            className="panel stack"
            id="options"
            data-empty={!journey}
            style={
              routeOptionsPanelHeight
                ? { height: `${routeOptionsPanelHeight}px` }
                : undefined
            }
          >
            <Heading over="Route options" title="Choose what feels easiest" />
            <p className="route-options-subtitle">
              3 routes ranked by sensory level
            </p>
            <AnimatePresence mode="wait" initial={false}>
              {expandedRouteId === null ? (
                <motion.div
                  key="route-list"
                  className="route-options-view"
                  initial={reduceMotion ? false : { opacity: 0, scale: 0.97 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={reduceMotion ? undefined : { opacity: 0, scale: 0.97 }}
                  transition={{ duration: reduceMotion ? 0 : 0.24 }}
                >
            <div className="route-list">
              {routes.map((r) => (
                <motion.button
                  layout
                  whileHover={reduceMotion ? undefined : { y: -2 }}
                  whileTap={reduceMotion ? undefined : { scale: 0.985 }}
                  className={`route-card ${r.id === route.id ? "is-selected" : ""}`}
                  data-risk={r.risk}
                  onClick={() => {
                    setRouteOptionsPanelHeight(
                      routeOptionsPanelRef.current?.getBoundingClientRect()
                        .height ?? null,
                    );
                    select(r.id);
                    setExpandedRouteId(r.id);
                  }}
                  key={r.id}
                >
                  <div className="route-card-heading">
                    <span className="route-card-icon" aria-hidden="true">
                      {r.risk === "low" ? "✦" : r.risk === "medium" ? "↗" : "!"}
                    </span>
                    <span className="route-meaning">
                      {r.risk === "low"
                        ? "Recommended"
                        : r.risk === "medium"
                          ? "Alternative"
                          : "High sensory load"}
                    </span>
                  </div>
                  <div className="route-card-summary">
                    <div>
                      <h3>{r.name}</h3>
                      <p>
                        <span className="sensory-dot" aria-hidden="true" />
                        {r.risk === "medium" ? "Moderate sensory" : r.label}
                      </p>
                    </div>
                    <div className="route-card-journey">
                      <strong className="route-meta">
                        <ClockIcon />
                        {metricForRoute(r.id)
                          ? `≈${metricForRoute(r.id)!.estimatedMinutes} min`
                          : r.id === "a" && calculatedRoutes?.lowCrowdPending
                            ? "Calculating…"
                            : calculatedRoutes || !routingIsActive
                              ? "Unavailable"
                              : "Calculating…"}
                      </strong>
                      <span>
                        {metricForRoute(r.id)
                          ? `${(metricForRoute(r.id)!.distanceMeters / 1000).toFixed(1)} km`
                          : "—"}
                      </span>
                    </div>
                  </div>
                  <div className="route-card-footer">
                    <div className="route-crowd">
                      <span className="crowd-icon" aria-hidden="true">●</span>
                      <span>
                        <small>Crowd</small>
                        <strong>
                          {r.id === "b"
                            ? "Moderate"
                            : r.metrics
                                .find((metric) => metric.startsWith("Crowd:"))
                                ?.replace("Crowd:", "")
                                .trim() ?? "Not available"}
                        </strong>
                      </span>
                    </div>
                    <span className="route-action">Choose this route →</span>
                  </div>
                </motion.button>
              ))}
            </div>
            <div className="routing-debug" role="status" aria-live="polite">
              <strong>Routing diagnostic</strong>
              <span>{routingStatus}</span>
            </div>
                </motion.div>
              ) : (
                <motion.div
                  key={`route-detail-${expandedRouteId}`}
                  className="route-option-detail"
                  data-risk={route.risk}
                  initial={reduceMotion ? false : { opacity: 0, scale: 0.97 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={reduceMotion ? undefined : { opacity: 0, scale: 0.97 }}
                  transition={{ duration: reduceMotion ? 0 : 0.24 }}
                >
                  <button
                    type="button"
                    className="route-detail-back"
                    onClick={() => setExpandedRouteId(null)}
                  >
                    ← Back to routes
                  </button>
                  <div className="route-detail-header">
                    <div>
                      <span className="route-meaning">
                        {route.risk === "low"
                          ? "Recommended"
                          : route.risk === "medium"
                            ? "Alternative"
                            : "High sensory load"}
                      </span>
                      <h3>{route.name}</h3>
                      <p>
                        <span className="sensory-dot" aria-hidden="true" />
                        {route.risk === "medium"
                          ? "Moderate sensory"
                          : route.label}
                      </p>
                    </div>
                    <div className="route-card-journey">
                      <strong className="route-meta">
                        <ClockIcon />
                        {route.eta}
                      </strong>
                      <span>{route.distance}</span>
                    </div>
                  </div>
                  <p className="route-detail-summary">{route.summary}</p>
                  <div className="route-detail-features">
                    {route.scores
                      .filter(
                        (score) =>
                          route.id !== "a" ||
                          (score[0] !== "Fewer construction points" &&
                            score[0] !== "More open-space nearby"),
                      )
                      .map((score, index) => (
                      <article key={score[0]}>
                        <span className="score-index">{index + 1}</span>
                        <div>
                          <h4>{score[0]}</h4>
                          <p>{score[1]}</p>
                        </div>
                        <strong>{score[2]}</strong>
                      </article>
                    ))}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </aside>
        </motion.section>
        <motion.section {...reveal} className="details-grid details-grid-compact" id="details">
          <article className="panel" id="navigation" data-empty={!journey}>
            <div className="navigation-panel-header">
              <div>
                <Heading
                  over="Navigation in progress"
                  title={journey ? route.nav[0] : "Route guidance"}
                />
                <span className="navigation-sensory-badge">
                  <span aria-hidden="true">◆</span> {route.label} route
                </span>
              </div>
              <div className="navigation-reassurance">
                <span className="navigation-mini-icon" aria-hidden="true">⌁</span>
                <strong>
                  {routeId === "a"
                    ? "You're on the calmer route"
                    : routeId === "b"
                      ? "You're following the alternative route"
                      : "You're following the faster route"}
                </strong>
              </div>
            </div>
            <div className="navigation-next-card">
              <span className="navigation-direction-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24"><path d="M5 19 19 5M10 5h9v9" /></svg>
              </span>
              <div className="navigation-next-copy">
                <p className="step-label">Next step</p>
                <h3>{route.nav[1]}</h3>
                <p>{route.nav[2]}</p>
              </div>
              <div className="navigation-progress-bar">
                <span className="navigation-progress-item">
                  <ClockIcon />
                  <span><strong>{route.eta}</strong><small>remaining</small></span>
                </span>
                <span className="navigation-progress-item">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/></svg>
                  <span><strong>{route.distance}</strong><small>remaining</small></span>
                </span>
              </div>
            </div>
            <motion.div
              initial={reduceMotion ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: reduceMotion ? 0 : 0.2 }}
              className="navigation-route-status"
              data-route={routeId}
            >
              <span className="navigation-heart-icon" aria-hidden="true">♡</span>
              <div>
                <p className="step-label">
                  {routeId === "a"
                    ? "Calmer route active"
                    : routeId === "b"
                      ? "Alternative route active"
                      : "Fastest route active"}
                </p>
                <h3>
                  {routeId === "a"
                    ? "You are on the calmer route."
                    : routeId === "b"
                      ? "You are following the alternative route."
                      : "You are following the faster but busier route."}
                </h3>
                <p>
                  {routeId === "a"
                    ? "Continue following the recommended low-sensory route."
                    : route.summary}
                </p>
                {busy && routeId !== "a" && (
                  <button
                    className="secondary-action"
                    onClick={() => {
                      select("a");
                      setNotice("Switched to the recommended calmer Route A.");
                      document
                        .querySelector(".map-panel")
                        ?.scrollIntoView({ behavior: "smooth", block: "center" });
                    }}
                  >
                    Switch to calmer route
                  </button>
                )}
              </div>
              <div className="navigation-landscape" aria-hidden="true">
                <svg viewBox="0 0 260 150" role="presentation">
                  <rect width="260" height="150" rx="18" fill="#dfeef7" />
                  <circle cx="213" cy="31" r="14" fill="#f1d69a" />
                  <path d="M0 84c39-29 76-29 113 2 40-25 91-25 147 2v62H0Z" fill="#b8d8c2" />
                  <path d="M0 109c49-20 96-12 132 9 43-23 87-23 128-7v39H0Z" fill="#8fbea2" />
                  <path d="M116 150c3-28 15-55 36-82 8 5 13 12 16 20-16 23-26 43-29 62Z" fill="#f7ecd1" />
                  <g fill="#507f6b">
                    <rect x="40" y="73" width="5" height="42" rx="2" />
                    <circle cx="42" cy="67" r="17" />
                    <rect x="205" y="77" width="5" height="41" rx="2" />
                    <circle cx="207" cy="70" r="16" />
                  </g>
                  <path d="M145 55a13 13 0 0 1 26 0c0 9-13 22-13 22s-13-13-13-22Z" fill="#6d91b8" />
                  <circle cx="158" cy="55" r="4" fill="#fff" />
                  <path d="M22 31c8-9 20-8 26 0 7-5 17-1 18 7H16c0-4 2-6 6-7Z" fill="#fff" opacity=".78" />
                </svg>
              </div>
            </motion.div>
            <div className="navigation-quiet-strip">
              <span className="navigation-quiet-icon" aria-hidden="true">☼</span>
              <div><strong>Need a break later?</strong><span>Quiet break spots are shown below.</span></div>
              <button
                type="button"
                onClick={() =>
                  document
                    .querySelector("#quiet-break-support")
                    ?.scrollIntoView({ behavior: "smooth", block: "center" })
                }
              >
                View quiet break spots →
              </button>
            </div>
          </article>
        </motion.section>
        <motion.section
          {...reveal}
          className="panel quiet-space-card quiet-support-final"
          id="quiet-break-support"
          data-empty={!journey}
        >
          <div className="support-heading-row">
            <span className="support-heading-icon quiet" aria-hidden="true">
              ♧
            </span>
            <div>
              <Heading over="Quiet break support" title="Need a quiet break?" />
            </div>
          </div>
          <div className="quiet-place-grid">
            {quietBreakPlaces.map((place) => (
              <Place
                name={place.name}
                detail={quietPlaceDetail(place)}
                key={place.id}
              />
            ))}
          </div>
          {quietDataNeedsCaution && (
            <p className="quiet-data-note">
              These suggestions are based on general information and may not
              reflect real-time crowd levels.
            </p>
          )}
        </motion.section>
      </main>
    </>
  );
}
function Place({
  name,
  detail,
}: {
  name: string;
  detail: {
    amenity: string;
    crowd: string;
    proximity: string | null;
    isFresh: boolean;
  };
}) {
  const reduce = useReducedMotion();
  return (
    <motion.div
      className="quiet-place"
      whileHover={reduce ? undefined : { x: 3 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
    >
      <span className="quiet-place-icon" aria-hidden="true">♧</span>
      <h3>{name}</h3>
      <div className="quiet-place-tags">
        <span>{detail.amenity}</span>
        <span>{detail.crowd}</span>
      </div>
      {detail.proximity && <p>⌖ {detail.proximity}</p>}
    </motion.div>
  );
}
