"use client";

import { use } from "react";
import Link from "next/link";
import {
  Badge,
  Card,
  CardHeader,
  Chip,
  Empty,
  ErrorNote,
  Loading,
  PageTitle,
} from "@/components/ui";
import StatTile from "@/components/StatTile";
import type { AgentDetail, DecisionRow } from "@/lib/api";
import { count, dateTime, fraction, money, pct, ratio, usd4 } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useStream } from "@/lib/useStream";

export default function AgentPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = use(params);
  const detail = useApi<AgentDetail>(`/agents/${slug}`);
  const decisions = useApi<DecisionRow[]>(`/decisions?agent=${slug}&limit=20`);

  useStream(["fill", "decision"], () => {
    detail.refresh();
    decisions.refresh();
  });

  if (detail.loading && !detail.data) return <Loading />;
  if (detail.error)
    return (
      <Card>
        <ErrorNote message={detail.error} />
      </Card>
    );
  if (!detail.data) return null;

  const { agent, portfolio, positions, history } = detail.data;
  const totalReturn = (portfolio.equity / portfolio.initial_capital - 1) * 100;
  const latest = history[history.length - 1];

  return (
    <>
      <PageTitle
        title={agent.name}
        subtitle={agent.description}
        action={
          <div className="flex items-center gap-2">
            <Chip color={agent.color} />
            <Badge>{agent.kind === "LLM" ? "LLM" : "baseline"}</Badge>
            {!agent.is_active && <Badge tone="warn">inactivo</Badge>}
            <Link href="/" className="text-xs text-ink-muted hover:text-ink-secondary">
              ← volver
            </Link>
          </div>
        }
      />

      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Equity"
          value={money(portfolio.equity)}
          hint={`Capital inicial ${money(portfolio.initial_capital, true)}`}
          accent={agent.color}
        />
        <StatTile
          label="Retorno total"
          value={pct(totalReturn)}
          tone={totalReturn >= 0 ? "up" : "down"}
          hint={`${money(portfolio.equity - portfolio.initial_capital)} de P&L`}
        />
        <StatTile
          label="Efectivo"
          value={money(portfolio.cash)}
          hint={`${positions.length} posiciones · ${money(portfolio.positions_value, true)} invertido`}
        />
        <StatTile
          label="Comisiones pagadas"
          value={money(portfolio.fees_paid)}
          hint={`P&L realizado ${money(portfolio.realized_pnl)}`}
        />
      </div>

      {latest && (
        <Card className="mb-6">
          <CardHeader
            title="Métricas"
            subtitle={`Desde el inicio, calculadas al cierre de ${latest.day}.`}
          />
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 px-5 py-4 text-sm sm:grid-cols-3 lg:grid-cols-6">
            <Metric label="Sharpe" value={ratio(latest.sharpe)} />
            <Metric label="Sortino" value={ratio(latest.sortino)} />
            <Metric label="Max drawdown" value={fraction(latest.max_drawdown)} />
            <Metric label="Drawdown actual" value={fraction(latest.current_drawdown)} />
            <Metric label="Win rate" value={fraction(latest.win_rate, 0)} />
            <Metric label="Profit factor" value={ratio(latest.profit_factor)} />
            <Metric label="Alpha" value={fraction(latest.alpha)} />
            <Metric label="Beta" value={ratio(latest.beta)} />
            <Metric label="Retorno benchmark" value={fraction(latest.benchmark_return)} />
            <Metric label="Exposición bruta" value={fraction(latest.gross_exposure_pct, 0)} />
            <Metric label="Concentración (HHI)" value={ratio(latest.concentration_hhi)} />
            <Metric label="Turnover del día" value={fraction(latest.turnover, 1)} />
          </dl>
        </Card>
      )}

      <Card className="mb-6">
        <CardHeader title="Posiciones abiertas" />
        {!positions.length ? (
          <Empty>Sin posiciones abiertas.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b border-hairline text-left text-xs text-ink-muted">
                  <th className="py-2.5 pl-5 pr-3 font-medium">Símbolo</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Cantidad</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Entrada media</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Precio</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Valor</th>
                  <th className="py-2.5 pr-3 text-right font-medium">P&L no real.</th>
                  <th className="py-2.5 pr-5 text-right font-medium">Peso</th>
                </tr>
              </thead>
              <tbody className="tabular">
                {positions.map((p) => (
                  <tr key={p.symbol} className="border-b border-hairline last:border-0">
                    <td className="py-2.5 pl-5 pr-3 font-medium text-ink">{p.symbol}</td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {p.quantity.toFixed(4)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {money(p.avg_entry_price)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {money(p.price)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {money(p.market_value)}
                    </td>
                    <td
                      className={`py-2.5 pr-3 text-right ${
                        p.unrealized_pnl > 0
                          ? "text-up"
                          : p.unrealized_pnl < 0
                            ? "text-down"
                            : "text-ink-muted"
                      }`}
                    >
                      {money(p.unrealized_pnl)}{" "}
                      <span className="text-xs text-ink-muted">
                        ({pct(p.unrealized_pnl_pct)})
                      </span>
                    </td>
                    <td className="py-2.5 pr-5 text-right text-ink-muted">
                      {p.weight_pct.toFixed(1)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card className="mb-6">
        <CardHeader
          title="Decisiones recientes"
          subtitle="Lo que pensó, lo que costó pensarlo."
        />
        {!decisions.data?.length ? (
          <Empty>Este agente no ha tomado decisiones todavía.</Empty>
        ) : (
          <ul className="divide-y divide-hairline">
            {decisions.data.map((d) => (
              <li key={d.id} className="px-5 py-3">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <Badge>{d.kind}</Badge>
                  <Badge tone={d.status === "OK" ? "good" : d.status === "ERROR" ? "bad" : "warn"}>
                    {d.status}
                  </Badge>
                  <span className="text-ink-muted">{dateTime(d.created_at)}</span>
                  <span className="tabular ml-auto text-ink-muted">
                    {count(d.total_tokens)} tok · {(d.inference_ms / 1000).toFixed(1)} s ·{" "}
                    {usd4(d.shadow_cost_usd)}
                  </span>
                </div>
                {d.market_view && (
                  <p className="mt-1.5 text-sm text-ink-secondary">{d.market_view}</p>
                )}
                {d.reasoning && (
                  <p className="mt-1 whitespace-pre-line text-sm leading-relaxed text-ink-muted">
                    {d.reasoning}
                  </p>
                )}
                {d.error && <p className="mt-1 text-xs text-down">{d.error}</p>}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {agent.strategy_prompt && (
        <Card>
          <CardHeader
            title="Estrategia"
            subtitle="La única variable independiente del experimento: todos los agentes LLM comparten modelo, herramientas, límites de riesgo y cadencia."
          />
          <pre className="overflow-x-auto whitespace-pre-wrap px-5 py-4 text-xs leading-relaxed text-ink-muted">
            {agent.strategy_prompt}
          </pre>
        </Card>
      )}
    </>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-ink-muted">{label}</dt>
      <dd className="tabular mt-0.5 text-ink">{value}</dd>
    </div>
  );
}
