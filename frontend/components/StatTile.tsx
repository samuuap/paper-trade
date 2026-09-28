import type { ReactNode } from "react";

/**
 * A single number with its label. Used instead of a one-bar chart: when the
 * story is one value, the value *is* the chart.
 */
export default function StatTile({
  label,
  value,
  hint,
  tone = "neutral",
  accent,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "neutral" | "up" | "down";
  accent?: string;
}) {
  const toneClass =
    tone === "up" ? "text-up" : tone === "down" ? "text-down" : "text-ink";
  return (
    <div className="rounded-xl border border-hairline bg-card px-5 py-4">
      <div className="flex items-center gap-2">
        {accent && (
          <span
            className="inline-block size-2.5 rounded-[2px]"
            style={{ background: accent }}
            aria-hidden
          />
        )}
        <p className="text-xs font-medium text-ink-muted">{label}</p>
      </div>
      <p className={`tabular mt-2 text-2xl font-semibold tracking-tight ${toneClass}`}>
        {value}
      </p>
      {hint && <p className="mt-1 text-xs text-ink-muted">{hint}</p>}
    </div>
  );
}
