"use client";

import { useState } from "react";
import Link from "next/link";
import { Badge, Card, CardHeader, Chip, Empty, ErrorNote, Loading, PageTitle } from "@/components/ui";
import type { DecisionRow } from "@/lib/api";
import { count, dateTime, ratio, usd4 } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useStream } from "@/lib/useStream";

const KINDS = ["", "TRADE", "PLAN", "REVIEW", "RESEARCH"] as const;
const KIND_LABEL: Record<string, string> = {
  "": "Todas",
  TRADE: "Trading",
  PLAN: "Plan",
  REVIEW: "Revisión",
  RESEARCH: "Research",
};

function statusTone(status: string): "good" | "bad" | "warn" | "neutral" {
  if (status === "OK") return "good";
  if (status === "ERROR") return "bad";
  if (status === "INVALID_OUTPUT") return "warn";
  return "neutral";
}

export default function DecisionsPage() {
  const [kind, setKind] = useState<string>("");
  const decisions = useApi<DecisionRow[]>(
    `/decisions?limit=60${kind ? `&kind=${kind}` : ""}`,
  );
  useStream(["decision"], () => decisions.refresh());

  return (
    <>
      <PageTitle
        title="Decisiones"
        subtitle="Cada turno de cada agente, con su razonamiento completo y lo que costó producirlo. Esta tabla es tanto el producto como las operaciones."
      />

      <Card>
        <CardHeader
          title="Historial"
          action={
            <div className="flex gap-1 rounded-lg border border-hairline p-0.5">
              {KINDS.map((option) => (
                <button
                  key={option}
                  type="button"
                  onClick={() => setKind(option)}
                  className={`rounded-md px-2.5 py-1 text-xs transition-colors ${
                    kind === option
                      ? "bg-raised text-ink"
                      : "text-ink-muted hover:text-ink-secondary"
                  }`}
                >
                  {KIND_LABEL[option]}
                </button>
              ))}
            </div>
          }
        />
        {decisions.loading && !decisions.data ? (
          <Loading />
        ) : decisions.error ? (
          <ErrorNote message={decisions.error} />
        ) : !decisions.data?.length ? (
          <Empty>Todavía no hay decisiones registradas.</Empty>
        ) : (
          <ul className="divide-y divide-hairline">
            {decisions.data.map((d) => (
              <li key={d.id} className="px-5 py-4">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <Link
                    href={`/agents/${d.agent}`}
                    className="flex items-center gap-2 hover:underline"
                  >
                    <Chip color={d.color} />
                    <span className="font-medium text-ink">{d.agent_name}</span>
                  </Link>
                  <Badge>{KIND_LABEL[d.kind] ?? d.kind}</Badge>
                  <Badge tone={statusTone(d.status)}>{d.status}</Badge>
                  <span className="text-xs text-ink-muted">{dateTime(d.created_at)}</span>
                  {d.model && (
                    <span className="text-xs text-ink-muted">· {d.model}</span>
                  )}
                </div>

                {d.market_view && (
                  <p className="mt-2 text-sm text-ink-secondary">{d.market_view}</p>
                )}
                {d.reasoning && (
                  <p className="mt-1 whitespace-pre-line text-sm leading-relaxed text-ink-muted">
                    {d.reasoning}
                  </p>
                )}
                {d.error && <p className="mt-1 text-xs text-down">{d.error}</p>}

                <dl className="tabular mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-ink-muted">
                  <Stat label="tokens" value={count(d.total_tokens)} />
                  <Stat label="inferencia" value={`${(d.inference_ms / 1000).toFixed(1)} s`} />
                  <Stat label="tok/s" value={ratio(d.tokens_per_second, 1)} />
                  <Stat label="cola" value={`${(d.queue_wait_ms / 1000).toFixed(1)} s`} />
                  <Stat label="tools" value={String(d.tool_call_count)} />
                  <Stat label="reintentos" value={String(d.retries)} />
                  <Stat label="coste sombra" value={usd4(d.shadow_cost_usd)} />
                </dl>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1">
      <dt>{label}</dt>
      <dd className="text-ink-secondary">{value}</dd>
    </div>
  );
}
