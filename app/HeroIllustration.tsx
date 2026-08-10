"use client";
import { motion, useReducedMotion } from "framer-motion";

const loop = (duration: number, delay = 0) => ({
  duration,
  delay,
  repeat: Infinity,
  repeatType: "mirror" as const,
  ease: "easeInOut" as const,
});
export default function HeroIllustration() {
  const reduce = useReducedMotion();
  return (
    <div className="hero-illustration" aria-hidden="true">
      <svg viewBox="0 0 680 520">
        <defs>
          <linearGradient id="scene" x2="1" y2="1">
            <stop stopColor="#edf6ff" />
            <stop offset=".52" stopColor="#eee9fb" />
            <stop offset="1" stopColor="#e8f7ef" />
          </linearGradient>
          <linearGradient id="route" x2="1">
            <stop stopColor="#31a878" />
            <stop offset="1" stopColor="#66c693" />
          </linearGradient>
          <linearGradient id="coat" x2="1" y2="1">
            <stop stopColor="#557fc0" />
            <stop offset="1" stopColor="#7659bb" />
          </linearGradient>
          <filter id="blur">
            <feGaussianBlur stdDeviation="6" />
          </filter>
          <filter id="glow">
            <feGaussianBlur stdDeviation="4" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>
        <rect
          x="16"
          y="18"
          width="648"
          height="484"
          rx="48"
          fill="url(#scene)"
          stroke="#fff"
          strokeWidth="3"
        />
        <path
          d="M26 337c106-30 181-25 271 5 104 35 209 27 357-35v195H26Z"
          fill="#dce9ee"
        />
        <path
          d="M25 390c116-34 218-20 310 9 105 34 201 20 319-48"
          fill="none"
          stroke="#fff"
          strokeWidth="65"
        />
        <path
          d="M25 390c116-34 218-20 310 9 105 34 201 20 319-48"
          fill="none"
          stroke="#bfcdd7"
          strokeWidth="2"
          strokeDasharray="8 12"
        />
        <g opacity=".72">
          <path d="M38 142h112v189H38z" fill="#9daac0" />
          <path d="M56 114h76v28H56z" fill="#7f8da6" />
          <path
            d="M61 170h24v30H61zm43 0h24v30h-24zm-43 53h24v30H61zm43 0h24v30h-24z"
            fill="#dce7f3"
          />
          <path d="M150 182h85v154h-85z" fill="#a9b5c9" />
        </g>
        <motion.g
          opacity=".3"
          filter="url(#blur)"
          animate={reduce ? undefined : { x: [0, 8, -3] }}
          transition={loop(14)}
        >
          {[72, 108, 146, 185, 221].map((x, i) => (
            <g key={x} transform={`translate(${x} ${322 + (i % 2) * 14})`}>
              <circle cy="-20" r="12" fill="#626c88" />
              <path d="M-17 30c1-35 7-43 17-43s16 8 17 43Z" fill="#737d98" />
            </g>
          ))}
        </motion.g>
        <g opacity=".5">
          <path
            d="M77 299q23-35 46 0M68 286q33-49 65 0"
            fill="none"
            stroke="#d25d8b"
            strokeWidth="4"
            strokeLinecap="round"
          />
          <circle cx="100" cy="307" r="6" fill="#c14f7a" />
        </g>
        <ellipse cx="552" cy="365" rx="104" ry="37" fill="#b9ddc8" />
        <Tree x={493} y={270} />
        <Tree x={575} y={248} />
        <Tree x={625} y={290} />
        <path
          d="M456 337q102-52 183-5"
          fill="none"
          stroke="#79b995"
          strokeWidth="8"
          strokeLinecap="round"
        />
        <path
          d="M190 403C265 362 313 445 384 397s103-88 174-60"
          fill="none"
          stroke="#fff"
          strokeWidth="15"
          strokeLinecap="round"
        />
        <path
          d="M190 403C265 362 313 445 384 397s103-88 174-60"
          fill="none"
          stroke="url(#route)"
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray="2 13"
          filter="url(#glow)"
        />
        <motion.circle
          r="10"
          fill="#fff"
          stroke="#2f9b70"
          strokeWidth="5"
          animate={
            reduce
              ? undefined
              : {
                  cx: [190, 255, 322, 384, 448, 505, 558],
                  cy: [403, 385, 421, 397, 360, 337, 337],
                }
          }
          transition={{ duration: 12, repeat: Infinity, ease: "linear" }}
        />
        <motion.g
          animate={reduce ? undefined : { y: [0, -8, 0] }}
          transition={loop(6)}
        >
          <Pin x={190} y={366} color="#557fc0" label="A" />
        </motion.g>
        <motion.g
          animate={reduce ? undefined : { y: [0, -10, 0] }}
          transition={loop(6.5, 0.7)}
        >
          <Pin x={558} y={298} color="#36a274" label="✓" />
        </motion.g>
        <motion.circle
          cx="558"
          cy="337"
          r="19"
          fill="none"
          stroke="#42ad7e"
          strokeWidth="3"
          animate={
            reduce ? undefined : { r: [15, 31, 15], opacity: [0.65, 0, 0.65] }
          }
          transition={{ duration: 3.6, repeat: Infinity, ease: "easeOut" }}
        />
        <motion.g
          animate={reduce ? undefined : { y: [0, -4, 0], rotate: [0, 1.2, 0] }}
          transition={loop(7.5)}
          style={{ transformOrigin: "347px 310px" }}
        >
          <ellipse
            cx="349"
            cy="432"
            rx="66"
            ry="13"
            fill="#8390ad"
            opacity=".16"
          />
          <circle cx="345" cy="221" r="36" fill="#d69a7e" />
          <path
            d="M311 215c4-40 67-51 72-2-13-15-28-19-45-14-7 2-16 9-27 16Z"
            fill="#313e5c"
          />
          <path
            d="M307 285c8-34 27-48 42-48 22 0 43 19 49 55l-10 101h-82Z"
            fill="url(#coat)"
          />
          <path
            d="m319 385-7 63m62-63 13 63"
            stroke="#34445f"
            strokeWidth="18"
            strokeLinecap="round"
          />
          <path
            d="m319 449-21 8m90-8 21 8"
            stroke="#26354e"
            strokeWidth="11"
            strokeLinecap="round"
          />
          <path
            d="M375 275c25 8 33 29 21 49"
            fill="none"
            stroke="#d69a7e"
            strokeWidth="14"
            strokeLinecap="round"
          />
          <g transform="translate(376 285) rotate(7)">
            <rect width="45" height="72" rx="10" fill="#17243c" />
            <rect x="5" y="7" width="35" height="55" rx="6" fill="#eef6ff" />
            <path
              d="M12 50c6-17 13-8 19-28"
              fill="none"
              stroke="#38a877"
              strokeWidth="4"
            />
            <circle cx="31" cy="22" r="4" fill="#3ba97a" />
          </g>
        </motion.g>
        <motion.g
          opacity=".75"
          animate={reduce ? undefined : { x: [0, 18, 0] }}
          transition={loop(18)}
        >
          <path
            d="M266 106c9-22 42-20 49 1 19-9 37 4 35 21h-99c-2-12 5-20 15-22Z"
            fill="#fff"
          />
          <path
            d="M470 89c7-18 35-16 40 1 16-8 31 3 29 17h-82c-1-10 4-16 13-18Z"
            fill="#fff"
          />
        </motion.g>
        <motion.g
          transform="translate(438 188)"
          animate={reduce ? undefined : { y: [0, -6, 0] }}
          transition={loop(8, 1)}
        >
          <circle r="28" fill="#fff" opacity=".86" />
          <path
            d="M-10 3c7-8 13-8 20 0M-14-4c10-12 18-12 28 0"
            fill="none"
            stroke="#7659bb"
            strokeWidth="3"
            strokeLinecap="round"
          />
          <circle cy="9" r="3" fill="#7659bb" />
        </motion.g>
      </svg>
    </div>
  );
}
function Pin({
  x,
  y,
  color,
  label,
}: {
  x: number;
  y: number;
  color: string;
  label: string;
}) {
  return (
    <g transform={`translate(${x} ${y})`}>
      <path
        d="M0 7S-27-9-27-27 0-54 0-54s27 9 27 27C27-9 0 7 0 7Z"
        fill={color}
        stroke="#fff"
        strokeWidth="4"
      />
      <circle cy="-28" r="13" fill="#fff" />
      <text
        y="-23"
        textAnchor="middle"
        fill={color}
        fontSize="14"
        fontWeight="800"
      >
        {label}
      </text>
    </g>
  );
}
function Tree({ x, y }: { x: number; y: number }) {
  return (
    <g transform={`translate(${x} ${y})`}>
      <path
        d="M0 44v45"
        stroke="#7e695a"
        strokeWidth="9"
        strokeLinecap="round"
      />
      <circle cy="20" r="35" fill="#65b388" />
      <circle cx="-22" cy="38" r="26" fill="#78c399" />
      <circle cx="25" cy="39" r="27" fill="#54a779" />
    </g>
  );
}
