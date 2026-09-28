/** Number formatting. Centralised so the same value never renders two ways. */

const currency = new Intl.NumberFormat("es-ES", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const compactCurrency = new Intl.NumberFormat("es-ES", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});

export function money(value: number | null | undefined, compact = false): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return compact ? compactCurrency.format(value) : currency.format(value);
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

/** For values stored as fractions (0.0123) rather than percentages. */
export function fraction(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return pct(value * 100, digits);
}

export function ratio(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("es-ES").format(value);
}

export function tokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${seconds.toFixed(1)} s`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${minutes.toFixed(1)} min`;
  return `${(minutes / 60).toFixed(1)} h`;
}

export function usd4(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value === 0) return "$0";
  if (Math.abs(value) < 0.01) return `$${value.toFixed(5)}`;
  return `$${value.toFixed(3)}`;
}

export function time(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("es-ES", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("es-ES", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Human-readable age.
 *
 * Handles future timestamps rather than rendering "hace -4026 s": under a
 * replay clock the recorded instant is a simulated one, and it can sit ahead
 * of the wall clock perfectly legitimately.
 */
export function relative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  const ago = seconds >= 0;
  const delta = Math.abs(seconds);
  const amount =
    delta < 60
      ? `${Math.round(delta)} s`
      : delta < 3600
        ? `${Math.round(delta / 60)} min`
        : delta < 86400
          ? `${Math.round(delta / 3600)} h`
          : `${Math.round(delta / 86400)} d`;
  return ago ? `hace ${amount}` : `en ${amount}`;
}

export function signClass(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return "text-ink-muted";
  return value > 0 ? "text-up" : "text-down";
}
