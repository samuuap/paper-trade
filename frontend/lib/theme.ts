/**
 * Chart tokens.
 *
 * Dark-only, deliberately. The agent colours live in the database as a single
 * hex each, so supporting both modes would mean shipping a light palette that
 * nobody validated against a light surface. One validated mode beats two modes
 * where one is guesswork.
 *
 * The four agent hues are not a taste call: they are the only four-hue subset
 * of the reference ramp that clears the colour-vision-deficiency checks with
 * every pair on screen at once (worst pair ΔE 6.9, which is legal *with*
 * secondary encoding — hence the legend, the end-of-line labels and the name
 * beside every colour chip). The two baselines are neutral greys on purpose:
 * they are reference lines, not competitors, and the grey says so.
 *
 * Changing any of them means re-running the palette validator against the
 * dark surface below. See backend `app/agents/strategies.py`.
 */

export const surface = {
  /** The page behind the cards. */
  plane: "#0d0d0d",
  /** The card and chart surface the palette was validated against. */
  card: "#1a1a19",
  raised: "#232321",
} as const;

export const ink = {
  primary: "#ffffff",
  secondary: "#c3c2b7",
  muted: "#898781",
} as const;

export const chrome = {
  grid: "#2c2c2a",
  axis: "#383835",
  border: "rgba(255,255,255,0.10)",
} as const;

/** Reserved for state. Never reused as a series colour. */
export const status = {
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
} as const;

/** Gains and losses. Paired with a sign or an arrow, never colour alone. */
export const delta = {
  up: "#0ca30c",
  down: "#d03b3b",
  flat: ink.muted,
} as const;

/** Fallback for an agent the backend did not colour. */
export const FALLBACK_SERIES = "#3987e5";

/** The benchmark overlay: neutral and dashed, so it reads as a reference. */
export const BENCHMARK_COLOR = "#c3c2b7";

export const PHASE_LABEL: Record<string, string> = {
  PRE_MARKET: "Pre-market",
  OPEN: "Mercado abierto",
  AFTER_HOURS: "After-hours",
  CLOSED: "Cerrado",
};

export const PHASE_COLOR: Record<string, string> = {
  PRE_MARKET: status.warning,
  OPEN: status.good,
  AFTER_HOURS: status.serious,
  CLOSED: ink.muted,
};
