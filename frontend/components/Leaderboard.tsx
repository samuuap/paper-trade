"use client";

import Link from "next/link";
import type { LeaderboardRow } from "@/lib/api";
import { count, fraction, money, pct, ratio, usd4 } from "@/lib/format";
import { Badge, Chip } from "./ui";

/**
 * The ranking, with what it cost to get there in the same row.
 *
 * Putting return and token spend side by side is the whole point of the
 * project: a strategy that wins by burning ten times the inference of the
 * baseline has not obviously won.
 */
export default function Leaderboard({ rows }: { rows: LeaderboardRow[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[980px] text-sm">
        <thead>
          <tr className="border-b border-hairline text-left text-xs text-ink-muted">
            <th className="py-2.5 pl-5 pr-3 font-medium">#</th>
            <th className="py-2.5 pr-3 font-medium">Agente</th>
            <th className="py-2.5 pr-3 text-right font-medium">Equity</th>
            <th className="py-2.5 pr-3 text-right font-medium">Retorno</th>
            <th className="py-2.5 pr-3 text-right font-medium">vs SPY</th>
            <th className="py-2.5 pr-3 text-right font-medium">Sharpe</th>
            <th className="py-2.5 pr-3 text-right font-medium">Max DD</th>
            <th className="py-2.5 pr-3 text-right font-medium">Pos.</th>
            <th className="py-2.5 pr-3 text-right font-medium">Decisiones</th>
            <th className="py-2.5 pr-3 text-right font-medium">Tokens</th>
            <th className="py-2.5 pr-5 text-right font-medium">Coste sombra</th>
          </tr>
        </thead>
        <tbody className="tabular">
          {rows.map((row) => {
            const excess =
              row.benchmark_return === null
                ? null
                : row.total_return_pct - row.benchmark_return * 100;
            return (
              <tr
                key={row.slug}
                className="border-b border-hairline last:border-0 hover:bg-raised/60"
              >
                <td className="py-2.5 pl-5 pr-3 text-ink-muted">{row.rank}</td>
                <td className="py-2.5 pr-3">
                  <Link
                    href={`/agents/${row.slug}`}
                    className="flex items-center gap-2 hover:underline"
                  >
                    <Chip color={row.color} />
                    <span className="text-ink">{row.name}</span>
                    {row.kind === "BASELINE" && <Badge>baseline</Badge>}
                  </Link>
                </td>
                <td className="py-2.5 pr-3 text-right text-ink">{money(row.equity)}</td>
                <td
                  className={`py-2.5 pr-3 text-right ${
                    row.total_return_pct > 0
                      ? "text-up"
                      : row.total_return_pct < 0
                        ? "text-down"
                        : "text-ink-muted"
                  }`}
                >
                  {pct(row.total_return_pct)}
                </td>
                <td
                  className={`py-2.5 pr-3 text-right ${
                    excess === null
                      ? "text-ink-muted"
                      : excess > 0
                        ? "text-up"
                        : excess < 0
                          ? "text-down"
                          : "text-ink-muted"
                  }`}
                >
                  {excess === null ? "—" : pct(excess)}
                </td>
                <td className="py-2.5 pr-3 text-right text-ink-secondary">
                  {ratio(row.sharpe)}
                </td>
                <td className="py-2.5 pr-3 text-right text-ink-secondary">
                  {fraction(row.max_drawdown)}
                </td>
                <td className="py-2.5 pr-3 text-right text-ink-secondary">
                  {row.position_count}
                </td>
                <td className="py-2.5 pr-3 text-right text-ink-secondary">
                  {count(row.decisions)}
                </td>
                <td className="py-2.5 pr-3 text-right text-ink-secondary">
                  {count(row.total_tokens)}
                </td>
                <td className="py-2.5 pr-5 text-right text-ink-secondary">
                  {usd4(row.shadow_cost_usd)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="border-t border-hairline px-5 py-3 text-xs leading-relaxed text-ink-muted">
        Con seis carteras y pocos meses de datos, las diferencias de rentabilidad son
        en su mayoría <strong className="font-medium text-ink-secondary">ruido</strong>,
        no habilidad. Los dos baselines son la referencia que permite distinguir señal
        de azar.
      </p>
    </div>
  );
}
