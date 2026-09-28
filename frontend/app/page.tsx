"use client";

import EquityChart from "@/components/EquityChart";
import Leaderboard from "@/components/Leaderboard";
import StatTile from "@/components/StatTile";
import { Card, CardHeader, ErrorNote, Loading, PageTitle } from "@/components/ui";
import type { EquityResponse, LeaderboardRow } from "@/lib/api";
import { count, duration, money, pct, usd4 } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useStream } from "@/lib/useStream";

export default function OverviewPage() {
  const board = useApi<LeaderboardRow[]>("/leaderboard");
  const equity = useApi<EquityResponse>("/equity?max_points=900");

  // A fill or a finished cycle is the only thing that moves these numbers.
  useStream(["fill", "cycle"], () => {
    board.refresh();
    equity.refresh();
  });

  const rows = board.data ?? [];
  const benchmark = rows.find((r) => r.slug === "buy_and_hold_spy");
  const best = rows[0];
  const llm = rows.filter((r) => r.kind === "LLM");
  const bestLlm = llm[0];

  const totalDecisions = rows.reduce((sum, r) => sum + r.decisions, 0);
  const totalShadow = rows.reduce((sum, r) => sum + r.shadow_cost_usd, 0);
  const totalSeconds = rows.reduce((sum, r) => sum + r.inference_seconds, 0);
  const excess =
    bestLlm && benchmark ? bestLlm.total_return_pct - benchmark.total_return_pct : null;

  return (
    <>
      <PageTitle
        title="Resumen"
        subtitle="Seis carteras compitiendo con dinero simulado. La pregunta no es cuánto ganan, sino si alguna bate al índice y a qué coste."
      />

      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Líder"
          value={best ? pct(best.total_return_pct) : "—"}
          hint={best?.name}
          accent={best?.color}
          tone={best && best.total_return_pct >= 0 ? "up" : "down"}
        />
        <StatTile
          label="Mejor LLM vs Buy & Hold"
          value={excess === null ? "—" : pct(excess)}
          hint={
            bestLlm && benchmark
              ? `${bestLlm.name} frente al benchmark`
              : "Esperando al primer ciclo"
          }
          tone={excess === null ? "neutral" : excess >= 0 ? "up" : "down"}
        />
        <StatTile
          label="Decisiones registradas"
          value={count(totalDecisions)}
          hint={`${duration(totalSeconds)} de inferencia acumulada`}
        />
        <StatTile
          label="Coste sombra acumulado"
          value={usd4(totalShadow)}
          hint="Lo que habrían costado estas decisiones en una API frontera"
        />
      </div>

      <Card className="mb-6">
        <CardHeader
          title="Curvas de equity"
          subtitle="Todos los agentes sobre el mismo eje, con el benchmark rebasado al mismo capital inicial."
        />
        {equity.loading && !equity.data ? (
          <Loading />
        ) : equity.error ? (
          <ErrorNote message={equity.error} />
        ) : equity.data ? (
          <EquityChart payload={equity.data} />
        ) : null}
      </Card>

      <Card>
        <CardHeader
          title="Leaderboard"
          subtitle="Rentabilidad junto al coste que la produjo."
          action={
            board.data && benchmark ? (
              <span className="text-xs text-ink-muted">
                Capital inicial {money(benchmark.initial_capital, true)} por agente
              </span>
            ) : undefined
          }
        />
        {board.loading && !board.data ? (
          <Loading />
        ) : board.error ? (
          <ErrorNote message={board.error} />
        ) : (
          <Leaderboard rows={rows} />
        )}
      </Card>
    </>
  );
}
