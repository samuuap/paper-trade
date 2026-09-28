"use client";

import { useState } from "react";
import Link from "next/link";
import { Card, CardHeader, Chip, Empty, ErrorNote, Loading, PageTitle, Badge } from "@/components/ui";
import type { RejectedOrder, TradeRow } from "@/lib/api";
import { dateTime, money, ratio } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useStream } from "@/lib/useStream";

export default function TradesPage() {
  const [symbol, setSymbol] = useState("");
  const query = symbol.trim() ? `?symbol=${encodeURIComponent(symbol.trim())}&limit=150` : "?limit=150";
  const trades = useApi<TradeRow[]>(`/trades${query}`);
  const rejected = useApi<RejectedOrder[]>("/orders/rejected?limit=25");

  useStream(["fill", "order"], () => {
    trades.refresh();
    rejected.refresh();
  });

  return (
    <>
      <PageTitle
        title="Operaciones"
        subtitle="Cada fill con el razonamiento que lo motivó y lo que costó ejecutarlo."
      />

      <Card className="mb-6">
        <CardHeader
          title="Registro de fills"
          action={
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              placeholder="Filtrar por símbolo"
              className="w-44 rounded-md border border-hairline bg-plane px-2.5 py-1 text-xs text-ink placeholder:text-ink-muted focus:border-ink-muted focus:outline-none"
            />
          }
        />
        {trades.loading && !trades.data ? (
          <Loading />
        ) : trades.error ? (
          <ErrorNote message={trades.error} />
        ) : !trades.data?.length ? (
          <Empty>Todavía no se ha ejecutado ninguna orden.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1020px] text-sm">
              <thead>
                <tr className="border-b border-hairline text-left text-xs text-ink-muted">
                  <th className="py-2.5 pl-5 pr-3 font-medium">Momento</th>
                  <th className="py-2.5 pr-3 font-medium">Agente</th>
                  <th className="py-2.5 pr-3 font-medium">Lado</th>
                  <th className="py-2.5 pr-3 font-medium">Símbolo</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Cantidad</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Precio</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Slippage</th>
                  <th className="py-2.5 pr-3 text-right font-medium">Comisión</th>
                  <th className="py-2.5 pr-3 text-right font-medium">P&L real.</th>
                  <th className="py-2.5 pr-5 font-medium">Motivo</th>
                </tr>
              </thead>
              <tbody className="tabular">
                {trades.data.map((t) => (
                  <tr key={t.id} className="border-b border-hairline last:border-0 align-top">
                    <td className="whitespace-nowrap py-2.5 pl-5 pr-3 text-ink-muted">
                      {dateTime(t.ts)}
                    </td>
                    <td className="py-2.5 pr-3">
                      <Link
                        href={`/agents/${t.agent}`}
                        className="flex items-center gap-2 whitespace-nowrap hover:underline"
                      >
                        <Chip color={t.color} />
                        <span className="text-ink-secondary">{t.agent_name}</span>
                      </Link>
                    </td>
                    <td className="py-2.5 pr-3">
                      <Badge tone={t.side === "BUY" ? "good" : "bad"}>
                        {t.side === "BUY" ? "Compra" : "Venta"}
                      </Badge>
                    </td>
                    <td className="py-2.5 pr-3 font-medium text-ink">{t.symbol}</td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {t.quantity.toFixed(4)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-secondary">
                      {money(t.price)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-muted">
                      {money(t.slippage_cost)}
                    </td>
                    <td className="py-2.5 pr-3 text-right text-ink-muted">
                      {money(t.commission)}
                    </td>
                    <td
                      className={`py-2.5 pr-3 text-right ${
                        t.realized_pnl > 0
                          ? "text-up"
                          : t.realized_pnl < 0
                            ? "text-down"
                            : "text-ink-muted"
                      }`}
                    >
                      {t.realized_pnl === 0 ? "—" : money(t.realized_pnl)}
                    </td>
                    <td className="max-w-[320px] py-2.5 pr-5 text-xs leading-relaxed text-ink-muted">
                      {t.rationale || "—"}
                      {t.conviction !== null && (
                        <span className="ml-1 text-ink-secondary">
                          (convicción {ratio(t.conviction)})
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card>
        <CardHeader
          title="Órdenes rechazadas"
          subtitle="El rastro de auditoría de «el modelo propone, Python dispone»: lo que un agente quiso hacer y por qué no se le dejó."
        />
        {!rejected.data?.length ? (
          <Empty>Ninguna orden ha sido rechazada todavía.</Empty>
        ) : (
          <ul className="divide-y divide-hairline">
            {rejected.data.map((order) => (
              <li key={order.id} className="px-5 py-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-ink-muted">{dateTime(order.ts)}</span>
                  <span className="text-ink-secondary">{order.agent}</span>
                  <Badge tone="bad">{order.side}</Badge>
                  <span className="font-medium text-ink">{order.symbol}</span>
                  <span className="tabular text-ink-muted">{order.quantity.toFixed(4)}</span>
                </div>
                <p className="mt-1 text-xs text-down">{order.reject_reason}</p>
                {order.rationale && (
                  <p className="mt-1 text-xs italic text-ink-muted">“{order.rationale}”</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
