"use client";
import { FormEvent, useEffect, useState } from "react";
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
const prefs = [
  "Avoid crowds",
  "Avoid construction",
  "Simpler crossings",
];
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
    [selectedPrefs, setPrefs] = useState<string[]>(prefs.slice(0, 2)),
    [from, setFrom] = useState("RMIT University, Melbourne"),
    [to, setTo] = useState("State Library Victoria, Melbourne"),
    [journey, setJourney] = useState({
      origin: "RMIT University, Melbourne",
      destination: "State Library Victoria, Melbourne",
    }),
    [notice, setNotice] = useState(""),
    [crowdData, setCrowdData] = useState<CrowdDensityPoint[]>([]),
    [crowdStatus, setCrowdStatus] = useState("Loading crowd conditions…"),
    [dataQualityStatus, setDataQualityStatus] = useState(
      "Checking sensor freshness…",
    ),
    [calculatedRoutes, setCalculatedRoutes] =
      useState<CalculatedRouteMetrics | null>(null),
    [crowdRefresh, setCrowdRefresh] = useState(0);
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
            : calculatedRoutes
              ? "Unavailable"
              : "Calculating…",
        distance: "—",
      };
  const busy = crowdData.some((point) => point.level === "high");
  const quietPlaceDetail = (place: (typeof quietBreakPlaces)[number]) => {
    if (!crowdData.length) return `${place.amenity} · ${crowdStatus}`;
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
    const freshness =
      nearest.dataStatus === "fresh"
        ? "Fresh reading"
        : nearest.dataStatus === "stale"
          ? "Stale reading"
          : "No live reading";
    return `${place.amenity} · ${level} crowd · ${freshness} · nearest sensor ${sensorDistance} m away${nearest.updatedAt ? ` · observed ${nearest.updatedAt}` : ""}`;
  };
  useEffect(() => {
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
        const allConditions = payload.conditions ?? [];
        const fresh = allConditions.filter(
          (item) => item.live_status === "fresh",
        ).length;
        const stale = allConditions.filter(
          (item) => item.live_status === "stale",
        ).length;
        const noData = allConditions.filter(
          (item) => item.live_status === "no_data",
        ).length;
        setDataQualityStatus(
          `${fresh} fresh · ${stale} stale · ${noData} no data`,
        );
      } catch (error) {
        if (controller.signal.aborted) return;
        setCrowdData([]);
        setCrowdStatus(
          error instanceof Error
            ? error.message
            : "Crowd conditions are unavailable.",
        );
        setDataQualityStatus("Sensor freshness is unavailable.");
      }
    }
    void loadCrowdConditions();
    return () => controller.abort();
  }, [crowdRefresh]);
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
    setJourney({ origin, destination });
    setNotice(`Live route requested for ${origin} to ${destination}.`);
    document.querySelector("#options")?.scrollIntoView({ behavior: "smooth" });
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
            <a href="#data">Live data</a>
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
          <aside className="panel stack" id="data">
            <Heading over="Live data status" title="Current conditions" />
            <div className="status-list">
              <Status
                warning={crowdData.some((point) => point.level === "high")}
                title="Pedestrian density"
                text={
                  crowdData.some((point) => point.level === "high")
                    ? `${crowdData.filter((point) => point.level === "high").length} high-crowd sensor area${crowdData.filter((point) => point.level === "high").length === 1 ? "" : "s"}`
                    : crowdData.length
                      ? "No high-crowd sensor areas reported"
                      : crowdStatus
                }
              />
              <Status
                warning={dataQualityStatus.startsWith("0 fresh")}
                title="Sensor freshness"
                text={dataQualityStatus}
              />
              <Status
                calm
                title="Data confidence"
                text={notice || crowdStatus}
              />
            </div>
            <button
              className="quiet-button"
              onClick={() => {
                setNotice("");
                setCrowdRefresh((value) => value + 1);
              }}
            >
              Refresh crowd conditions
            </button>
          </aside>
          <section className="map-panel panel">
            <div className="map-toolbar">
              <Heading
                over="Google Maps · Walking"
                title={
                  route.id === "a"
                    ? "Recommended low-sensory route"
                    : `Selected ${route.label.toLowerCase()} route`
                }
              />
              <span className="route-badge">{route.name}</span>
            </div>
            <GoogleRouteMap
              origin={journey.origin}
              destination={journey.destination}
              selectedRouteId={routeId}
              crowdData={crowdData}
              onRoutesCalculated={setCalculatedRoutes}
            />
          </section>
          <aside className="panel stack" id="options">
            <Heading over="Route options" title="Choose what feels easiest" />
            <div className="route-list">
              {routes.map((r) => (
                <motion.button
                  layout
                  whileHover={reduceMotion ? undefined : { y: -2 }}
                  whileTap={reduceMotion ? undefined : { scale: 0.985 }}
                  className={`route-card ${r.id === route.id ? "is-selected" : ""}`}
                  data-risk={r.risk}
                  onClick={() => select(r.id)}
                  key={r.id}
                >
                  <div>
                    <h3>
                      {r.name}
                      <span className="route-meaning">
                        <span aria-hidden="true">
                          {r.risk === "low"
                            ? "✓"
                            : r.risk === "medium"
                              ? "⚠"
                              : "⛔"}
                        </span>{" "}
                        {r.risk === "low"
                          ? "Recommended"
                          : r.risk === "medium"
                            ? "Alternative"
                            : "High sensory load"}
                      </span>
                    </h3>
                    <p>{r.label}</p>
                  </div>
                  <strong className="route-meta">
                    {metricForRoute(r.id)
                      ? `≈${metricForRoute(r.id)!.estimatedMinutes} min`
                      : r.id === "a" && calculatedRoutes?.lowCrowdPending
                        ? "Calculating…"
                      : calculatedRoutes
                          ? "Unavailable"
                          : "Calculating…"}
                  </strong>
                  <div className="route-metrics">
                    {metricForRoute(r.id) && (
                      <span>
                        Distance: {" "}
                        {(
                          metricForRoute(r.id)!.distanceMeters / 1000
                        ).toFixed(1)}{" "}
                        km
                      </span>
                    )}
                    {r.metrics.map((m) => {
                      const tone =
                        m.includes("high") || m.includes("limited")
                          ? "metric-high"
                          : m.includes("mid")
                            ? "metric-medium"
                            : m.includes("low") || m.includes("best")
                              ? "metric-low"
                              : "";
                      return (
                        <span className={tone} key={m}>
                          {m}
                        </span>
                      );
                    })}
                  </div>
                  <span className="route-action">View this route</span>
                </motion.button>
              ))}
            </div>
          </aside>
        </motion.section>
        <motion.section {...reveal} className="details-grid" id="details">
          <article className="panel route-detail-card">
            <Heading over="Recommended route" title={route.name} />
            <span className="soft-pill">{route.label}</span>
            <p>{route.summary}</p>
            <div className="score-breakdown">
              {route.scores.map((s, i) => (
                <article className={`score-item ${s[3]}`} key={s[0]}>
                  <span className="score-index">{i + 1}</span>
                  <div>
                    <h3>{s[0]}</h3>
                    <p>{s[1]}</p>
                  </div>
                  <strong>{s[2]}</strong>
                </article>
              ))}
            </div>
          </article>
          <article className="panel" id="navigation">
            <Heading over="Navigation in progress" title={route.nav[0]} />
            <div className="navigation-card">
              <p className="step-label">Next step</p>
              <h3>{route.nav[1]}</h3>
              <p>{route.nav[2]}</p>
              <div className="progress-row">
                <span>
                  <strong>{route.eta}</strong> remaining
                </span>
                <span>
                  <strong>{route.distance}</strong> remaining
                </span>
              </div>
            </div>
            <AnimatePresence>
              {busy && (
                <motion.div
                  initial={reduceMotion ? false : { opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  className="reroute-alert"
                >
                  <p className="step-label">Calm reroute available</p>
                  <h3>The next section is busier than usual.</h3>
                  <p>
                    A quieter alternative adds 3 min and passes through an open
                    area.
                  </p>
                  <button
                    className="secondary-action"
                    onClick={() => {
                      select("b");
                      setNotice("Switched to the calmer live-data option.");
                    }}
                  >
                    Switch to calmer route
                  </button>
                </motion.div>
              )}
            </AnimatePresence>
          </article>
          <article className="panel quiet-space-card">
            <Heading over="Quiet break support" title="Need a quiet break?" />
            {quietBreakPlaces.map((place) => (
              <Place
                name={place.name}
                detail={quietPlaceDetail(place)}
                key={place.id}
              />
            ))}
          </article>
        </motion.section>
      </main>
    </>
  );
}
function Status({
  warning = false,
  calm = false,
  title,
  text,
}: {
  warning?: boolean;
  calm?: boolean;
  title: string;
  text: string;
}) {
  const reduce = useReducedMotion();
  return (
    <motion.article
      whileHover={reduce ? undefined : { y: -2 }}
      transition={{ duration: 0.18 }}
    >
      <motion.span
        animate={reduce ? undefined : { scale: [1, 1.12, 1] }}
        transition={{ duration: 0.35 }}
        className={`status-dot ${warning ? "warning" : calm ? "calm" : "good"}`}
      />
      <div>
        <h3>{title}</h3>
        <AnimatePresence mode="wait" initial={false}>
          <motion.p
            key={text}
            initial={reduce ? false : { opacity: 0, y: 3 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduce ? undefined : { opacity: 0 }}
            transition={{ duration: reduce ? 0 : 0.2 }}
          >
            {text}
          </motion.p>
        </AnimatePresence>
      </div>
    </motion.article>
  );
}
function Place({ name, detail }: { name: string; detail: string }) {
  const reduce = useReducedMotion();
  return (
    <motion.div
      className="quiet-place"
      whileHover={reduce ? undefined : { x: 3 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
    >
      <h3>{name}</h3>
      <p>{detail}</p>
    </motion.div>
  );
}
