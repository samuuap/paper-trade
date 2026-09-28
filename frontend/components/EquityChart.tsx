"use client";

import { useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { EquityResponse } from "@/lib/api";
import { BENCHMARK_COLOR, chrome, ink, surface } from "@/lib/theme";
import { money, pct } from "@/lib/format";
import { Chip } from "./ui";

type Mode = "value" | "return";

interface Row {
  ts: number;
  [series: string]: number | null;
}

const BENCHMARK_KEY = "__benchmark";

/** Line styles for the grey reference curves, in roster order. */
const BASELINE_DASHES = ["7 3", "2 3"];

/**
 * Merge the per-series point lists onto one shared time axis.
 *
 * The API thins each series independently, so their timestamps do not line up.
 * Forward-filling on a union of the timestamps keeps every line continuous
 * without inventing values: a gap carries the last observed equity, which is
 * exactly what a portfolio does when nothing trades.
 */
function mergeSeries(payload: EquityResponse): { rows: Row[]; keys: string[] } {
  const sources: { key: string; points: { ts: string; equity: number }[] }[] = [
    ...payload.series.map((s) => ({ key: s.slug, points: s.points })),
    { key: BENCHMARK_KEY, points: payload.benchmark.points },
  ].filter((s) => s.points.length > 0);

  const stamps = new Set<number>();
  for (const source of sources) {
    for (const point of source.points) stamps.add(new Date(point.ts).getTime());
  }
  const axis = [...stamps].sort((a, b) => a - b);

  const cursors = new Map<string, number>();
  const carried = new Map<string, number | null>();
  for (const source of sources) {
    cursors.set(source.key, 0);
    carried.set(source.key, null);
  }

  const rows: Row[] = axis.map((ts) => {
    const row: Row = { ts };
    for (const source of sources) {
      let index = cursors.get(source.key)!;
      while (index < source.points.length && new Date(source.points[index].ts).getTime() <= ts) {
        carried.set(source.key, source.points[index].equity);
        index += 1;
      }
      cursors.set(source.key, index);
      row[source.key] = carried.get(source.key) ?? null;
    }
    return row;
  });

  return { rows, keys: sources.map((s) => s.key) };
}

function toReturns(rows: Row[], keys: string[]): Row[] {
  const bases = new Map<string, number>();
  for (const key of keys) {
    const first = rows.find((row) => typeof row[key] === "number" && row[key] !== 0);
    if (first) bases.set(key, first[key] as number);
  }
  return rows.map((row) => {
    const next: Row = { ts: row.ts };
    for (const key of keys) {
      const base = bases.get(key);
      const value = row[key];
      next[key] =
        base && typeof value === "number" ? ((value / base - 1) * 100) : null;
    }
    return next;
  });
}

export default function EquityChart({
  payload,
  height = 340,
}: {
  payload: EquityResponse;
  height?: number;
}) {
  const [mode, setMode] = useState<Mode>("value");
  const [table, setTable] = useState(false);

  const { rows, keys } = useMemo(() => mergeSeries(payload), [payload]);
  const data = useMemo(
    () => (mode === "return" ? toReturns(rows, keys) : rows),
    [rows, keys, mode],
  );

  const labels = useMemo(() => {
    const map = new Map<string, { name: string; color: string; dash?: string }>();
    // The baselines are greys, so grey alone cannot tell them apart. Line
    // style is the second channel: the palette stays validated and the two
    // reference curves stop reading as one smudge.
    let baselineIndex = 0;
    for (const series of payload.series) {
      const dash =
        series.kind === "BASELINE"
          ? BASELINE_DASHES[baselineIndex++ % BASELINE_DASHES.length]
          : undefined;
      map.set(series.slug, { name: series.name, color: series.color, dash });
    }
    map.set(BENCHMARK_KEY, {
      name: `${payload.benchmark.symbol} (benchmark)`,
      color: BENCHMARK_COLOR,
      dash: "4 4",
    });
    return map;
  }, [payload]);

  if (data.length === 0) {
    return (
      <div className="px-5 py-12 text-center text-sm text-ink-muted">
        Todavía no hay snapshots de equity. Aparecerán en cuanto corra el primer ciclo.
      </div>
    );
  }

  const format = (value: number) =>
    mode === "return" ? pct(value, 1) : money(value, true);

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 pb-3 pt-4">
        <div className="flex gap-1 rounded-lg border border-hairline p-0.5">
          {(
            [
              ["value", "Valor"],
              ["return", "Retorno"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setMode(key)}
              className={`rounded-md px-2.5 py-1 text-xs transition-colors ${
                mode === key ? "bg-raised text-ink" : "text-ink-muted hover:text-ink-secondary"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => setTable((v) => !v)}
          className="rounded-md border border-hairline px-2.5 py-1 text-xs text-ink-muted transition-colors hover:text-ink-secondary"
        >
          {table ? "Ver gráfica" : "Ver tabla"}
        </button>
      </div>

      {table ? (
        <EquityTable data={data} keys={keys} labels={labels} mode={mode} />
      ) : (
        <div style={{ height }} className="px-2">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 8, right: 84, bottom: 8, left: 8 }}>
              <CartesianGrid stroke={chrome.grid} strokeWidth={1} vertical={false} />
              <XAxis
                dataKey="ts"
                type="number"
                scale="time"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(ts: number) =>
                  new Date(ts).toLocaleDateString("es-ES", {
                    day: "2-digit",
                    month: "short",
                  })
                }
                tick={{ fill: ink.muted, fontSize: 11 }}
                axisLine={{ stroke: chrome.axis }}
                tickLine={false}
                minTickGap={48}
              />
              <YAxis
                tickFormatter={format}
                tick={{ fill: ink.muted, fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                width={66}
                domain={["auto", "auto"]}
              />
              <Tooltip
                cursor={{ stroke: chrome.axis, strokeWidth: 1 }}
                content={({ active, payload: points, label }) => {
                  if (!active || !points?.length) return null;
                  const ordered = [...points].sort(
                    (a, b) => (Number(b.value) || 0) - (Number(a.value) || 0),
                  );
                  return (
                    <div className="rounded-lg border border-hairline bg-card px-3 py-2 text-xs shadow-lg">
                      <p className="mb-1.5 text-ink-muted">
                        {new Date(Number(label)).toLocaleString("es-ES", {
                          day: "2-digit",
                          month: "short",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </p>
                      <div className="space-y-1">
                        {ordered.map((point) => {
                          const meta = labels.get(String(point.dataKey));
                          return (
                            <div
                              key={String(point.dataKey)}
                              className="flex items-center gap-2"
                            >
                              <Chip color={meta?.color ?? ink.muted} />
                              <span className="text-ink-secondary">{meta?.name}</span>
                              <span className="tabular ml-auto pl-4 text-ink">
                                {format(Number(point.value))}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                }}
              />
              {keys.map((key) => {
                const meta = labels.get(key);
                return (
                  <Line
                    key={key}
                    type="monotone"
                    dataKey={key}
                    name={meta?.name}
                    stroke={meta?.color ?? ink.muted}
                    strokeWidth={2}
                    strokeDasharray={meta?.dash}
                    dot={false}
                    // The endpoint marker doubles as the anchor for the direct
                    // label, so identity does not rest on colour alone.
                    activeDot={{
                      r: 4,
                      strokeWidth: 2,
                      stroke: surface.card,
                    }}
                    isAnimationActive={false}
                    connectNulls
                  />
                );
              })}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      <Legend data={data} keys={keys} labels={labels} mode={mode} />
    </div>
  );
}

/** Legend mark that mirrors the plot: same colour, same dash. */
function LineSwatch({ color, dash }: { color: string; dash?: string }) {
  return (
    <svg width="16" height="8" aria-hidden className="shrink-0">
      <line
        x1="0"
        y1="4"
        x2="16"
        y2="4"
        stroke={color}
        strokeWidth="2"
        strokeDasharray={dash}
      />
    </svg>
  );
}

function Legend({
  data,
  keys,
  labels,
  mode,
}: {
  data: Row[];
  keys: string[];
  labels: Map<string, { name: string; color: string; dash?: string }>;
  mode: Mode;
}) {
  const last = data[data.length - 1];
  const entries = keys
    .map((key) => ({
      key,
      meta: labels.get(key),
      value: typeof last?.[key] === "number" ? (last[key] as number) : null,
    }))
    .sort((a, b) => (b.value ?? -Infinity) - (a.value ?? -Infinity));

  return (
    <ul className="flex flex-wrap gap-x-5 gap-y-2 border-t border-hairline px-5 py-3 text-xs">
      {entries.map(({ key, meta, value }) => (
        <li key={key} className="flex items-center gap-2">
          <LineSwatch color={meta?.color ?? ink.muted} dash={meta?.dash} />
          <span className="text-ink-secondary">{meta?.name}</span>
          <span className="tabular text-ink-muted">
            {value === null ? "—" : mode === "return" ? pct(value, 2) : money(value)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function EquityTable({
  data,
  keys,
  labels,
  mode,
}: {
  data: Row[];
  keys: string[];
  labels: Map<string, { name: string; color: string; dash?: string }>;
  mode: Mode;
}) {
  // Last 40 rows, newest first: the table is an accessible equivalent of the
  // chart's right-hand edge, not a data dump.
  const rows = data.slice(-40).reverse();
  return (
    <div className="max-h-[340px] overflow-auto px-5 pb-4">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-card text-left text-ink-muted">
          <tr>
            <th className="py-2 pr-4 font-medium">Momento</th>
            {keys.map((key) => (
              <th key={key} className="py-2 pr-4 text-right font-medium">
                {labels.get(key)?.name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="tabular">
          {rows.map((row) => (
            <tr key={row.ts} className="border-t border-hairline">
              <td className="whitespace-nowrap py-1.5 pr-4 text-ink-muted">
                {new Date(row.ts).toLocaleString("es-ES", {
                  day: "2-digit",
                  month: "short",
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </td>
              {keys.map((key) => (
                <td key={key} className="py-1.5 pr-4 text-right text-ink-secondary">
                  {typeof row[key] === "number"
                    ? mode === "return"
                      ? pct(row[key] as number, 2)
                      : money(row[key] as number)
                    : "—"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
