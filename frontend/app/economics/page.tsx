"use client";

import { Card, CardHeader, Chip, Empty, ErrorNote, Loading, PageTitle } from "@/components/ui";
import StatTile from "@/components/StatTile";
import type { Economics } from "@/lib/api";
import { count, duration, money, ratio, usd4 } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function EconomicsPage() {
  const { data, loading, error } = useApi<Economics[]>("/economics", 60_000);
  const rows = data ?? [];

  const totalTokens = rows.reduce((s, r) => s + r.total_tokens, 0);
  const totalShadow = rows.reduce((s, r) => s + r.shadow_cost_usd, 0);
  const totalEnergy = rows.reduce((s, r) => s + r.energy_cost_usd, 0);
  const totalSeconds = rows.reduce((s, r) => s + r.inference_seconds, 0);
  const shadowModel = rows[0]?.shadow_model ?? "API frontera";

  return (
    <>
      <PageTitle
        title="Economía de los agentes"
        subtitle="Correr en local no hace la inferencia gratis: cambia la moneda de dólares a tiempo y vatios. Estas son las dos caras."
      />

      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile label="Tokens acumulados" value={count(totalTokens)} />
        <StatTile label="Tiempo de inferencia" value={duration(totalSeconds)} />
        <StatTile
          label="Coste sombra"
          value={usd4(totalShadow)}
          hint={`Lo que habría costado en ${shadowModel}`}
        />
        <StatTile
          label="Coste energético"
          value={usd4(totalEnergy)}
          hint="El coste local equivalente, en electricidad"
        />
      </div>

      <Card>
        <CardHeader
          title="Por agente"
          subtitle="Los baselines aparecen con ceros a propósito: eso es lo que hace comparable el coste por dólar de P&L en todo el roster."
        />
        {loading && !data ? (
          <Loading />
        ) : error ? (
          <ErrorNote message={error} />
        ) : !rows.length ? (
          <Empty>Ningún agente ha tomado decisiones todavía.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1040px] text-sm">
              <thead>
                <tr className="border-b border-hairline text-left text-xs text-ink-muted">
                  <th className="py-2.5 pl-5 pr-3 font-medium">Agente</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Decisiones</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Tokens</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Tok/decisión</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Tok/s</th>
                  <th className="py-2.5 pr-3 text-right font-medium">p50</th>
                  <th className="py-2.5 pr-3 text-right font-medium">p95</th>
                  <th className="py-2.5 pr-3 text-right font-medium">p99</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Reintentos</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Coste sombra</th>
                  <th className="py-2.5 pr-5 text-right font-medium">s / $ P&L</th>
                </tr>
              </thead>
              <tbody className="tabular">
                {rows.map((row) => (
                  <tr key={row.slug} className="border-b border-hairline last:border-0">
                    <td className="py-2.5 pl-5 pr-3">
                      <span className="flex items-center gap-2 whitespace-nowrap">
                        <Chip color={row.color} />
                        <span className="text-ink">{row.name}</span>
                      </span>
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {count(row.decisions)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {count(row.total_tokens)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {ratio(row.tokens_per_decision, 0)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {ratio(row.avg_tokens_per_second, 1)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-muted">
                      {latency(row.latency_ms.p50)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-muted">
                      {latency(row.latency_ms.p95)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-muted">
                      {latency(row.latency_ms.p99)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-muted">
                      {count(row.retries)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {usd4(row.shadow_cost_usd)}
                    </td>
                    <td
                      className={`py-2.5 pr-5 text-right ${
                        row.pnl > 0 ? "text-ink-secondary" : "text-ink-muted"
                      }`}
                      title={`P&L ${money(row.pnl)}`}
                    >
                      {row.seconds_per_pnl_dollar === null
                        ? "—"
                        : ratio(row.seconds_per_pnl_dollar, 2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="border-t border-hairline px-5 py-3 text-xs leading-relaxed text-ink-muted">
              <strong className="font-medium text-ink-secondary">s / $ P&L</strong> es
              el equivalente local a si el agente se paga a sí mismo: segundos de
              cómputo por cada dólar de beneficio. Un guion significa que el agente aún
              no está en positivo.
            </p>
          </div>
        )}
      </Card>
    </>
  );
}

function latency(ms: number | null): string {
  if (ms === null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}
