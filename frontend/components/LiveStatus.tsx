"use client";

import { useApi } from "@/lib/useApi";
import { useStream } from "@/lib/useStream";
import { PHASE_COLOR, PHASE_LABEL } from "@/lib/theme";
import type { OrchestratorStatus } from "@/lib/api";
import { time } from "@/lib/format";

export default function LiveStatus() {
  const { data, refresh } = useApi<OrchestratorStatus>("/status", 60_000);
  // A phase change is the one event that invalidates this widget outright.
  const { connected } = useStream(["phase"], () => refresh());

  const phase = data?.phase ?? "—";
  const color = PHASE_COLOR[phase] ?? "#898781";

  return (
    <div className="flex items-center gap-4 text-xs">
      <div className="flex items-center gap-2">
        <span
          className="inline-block size-2 rounded-full"
          style={{ background: color }}
          aria-hidden
        />
        <span className="text-ink-secondary">{PHASE_LABEL[phase] ?? phase}</span>
        {data?.next_transition && (
          <span className="hidden text-ink-muted sm:inline">
            → {PHASE_LABEL[data.next_transition.phase] ?? data.next_transition.phase}{" "}
            {time(data.next_transition.at)}
          </span>
        )}
      </div>
      <div className="flex items-center gap-1.5">
        <span
          className={`inline-block size-1.5 rounded-full ${
            connected ? "bg-up" : "bg-ink-muted"
          }`}
          aria-hidden
        />
        <span className="text-ink-muted">{connected ? "En vivo" : "Sin stream"}</span>
      </div>
      {data?.replay && (
        <span className="rounded border border-warning/40 px-1.5 py-0.5 text-warning">
          replay
        </span>
      )}
    </div>
  );
}
