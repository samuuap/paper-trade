"use client";

import { Badge, Card, CardHeader, Empty, ErrorNote, Loading, PageTitle } from "@/components/ui";
import StatTile from "@/components/StatTile";
import type { Health, JobRow, SystemEventRow } from "@/lib/api";
import { PHASE_LABEL } from "@/lib/theme";
import { count, dateTime, duration, relative } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useStream } from "@/lib/useStream";

export default function SystemPage() {
  const health = useApi<Health>("/health", 15_000);
  const jobs = useApi<JobRow[]>("/jobs?limit=25", 30_000);
  const events = useApi<SystemEventRow[]>("/events?limit=25", 30_000);

  useStream(["job", "system", "phase"], () => {
    health.refresh();
    jobs.refresh();
    events.refresh();
  });

  const h = health.data;

  return (
    <>
      <PageTitle
        title="Salud del sistema"
        subtitle="Esto corre desatendido en un portátil, así que «¿está vivo?» no es la pregunta útil. Cada sección falla por su cuenta."
      />

      {health.loading && !h ? (
        <Loading />
      ) : health.error ? (
        <Card>
          <ErrorNote message={health.error} />
        </Card>
      ) : h ? (
        <>
          <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label="Fase actual"
              value={PHASE_LABEL[h.orchestrator.phase] ?? h.orchestrator.phase}
              hint={
                h.orchestrator.next_transition
                  ? `Siguiente: ${
                      PHASE_LABEL[h.orchestrator.next_transition.phase] ??
                      h.orchestrator.next_transition.phase
                    } · ${dateTime(h.orchestrator.next_transition.at)}`
                  : undefined
              }
            />
            <StatTile
              label="Uptime del orquestador"
              value={h.orchestrator.running ? duration(h.orchestrator.uptime_seconds) : "parado"}
              hint={`${count(h.orchestrator.ticks)} ticks`}
              tone={h.orchestrator.running ? "neutral" : "down"}
            />
            <StatTile
              label="Lag de datos"
              value={
                h.data.quote_lag_seconds === null
                  ? "sin quotes"
                  : duration(h.data.quote_lag_seconds)
              }
              hint={`${count(h.data.screenable_symbols)} de ${count(
                h.data.universe_size,
              )} símbolos con señales`}
              tone={
                h.data.quote_lag_seconds !== null && h.data.quote_lag_seconds > 900
                  ? "down"
                  : "neutral"
              }
            />
            <StatTile
              label="Éxito de jobs (24 h)"
              value={
                h.jobs.success_rate === null
                  ? "—"
                  : `${(h.jobs.success_rate * 100).toFixed(1)}%`
              }
              hint={`${count(h.decisions.decisions)} decisiones · ${
                h.decisions.failed
              } fallidas`}
              tone={
                h.jobs.success_rate !== null && h.jobs.success_rate < 0.9 ? "down" : "neutral"
              }
            />
          </div>

          <div className="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader
                title="Inferencia local"
                subtitle="Memoria, residencia del modelo y profundidad de la cola de decisiones."
              />
              <dl className="divide-y divide-hairline text-sm">
                <Row label="Modelo para esta fase" value={h.inference.model_for_phase ?? "—"} />
                <Row
                  label="Modelo residente"
                  value={
                    h.inference.model_resident ? (
                      <Badge tone="good">
                        sí · {h.inference.inference_process?.rss_gb.toFixed(1)} GB
                      </Badge>
                    ) : (
                      <Badge>descargado</Badge>
                    )
                  }
                />
                <Row
                  label="Memoria libre"
                  value={`${h.inference.memory.available_gb.toFixed(1)} GB de ${h.inference.memory.total_gb.toFixed(
                    0,
                  )} GB`}
                />
                <Row
                  label="Suelo de memoria"
                  value={`${h.inference.min_free_memory_gb.toFixed(1)} GB`}
                />
                <Row
                  label="Cola"
                  value={`${h.inference.queue.depth} en cola · ${count(
                    h.inference.queue.completed,
                  )} completadas · ${h.inference.queue.failed} fallidas`}
                />
                <Row
                  label="Espera media"
                  value={`${(h.inference.queue.avg_wait_ms / 1000).toFixed(2)} s`}
                />
              </dl>
            </Card>

            <Card>
              <CardHeader
                title="Datos de mercado"
                subtitle="Consumo del presupuesto de peticiones del proveedor y cobertura de barras."
              />
              <dl className="divide-y divide-hairline text-sm">
                <Row
                  label="Proveedor"
                  value={
                    <span className="flex items-center gap-2">
                      {h.provider.provider}
                      {h.provider.offline && <Badge tone="warn">offline</Badge>}
                    </span>
                  }
                />
                {h.provider.rate_limit && (
                  <>
                    <Row
                      label="Rate limit"
                      value={`${h.provider.rate_limit.used_last_minute} / ${h.provider.rate_limit.limit_per_minute} por minuto`}
                    />
                    <Row
                      label="Peticiones totales"
                      value={`${count(h.provider.rate_limit.total_requests)} · ${count(
                        h.provider.rate_limit.total_throttled,
                      )} throttled`}
                    />
                  </>
                )}
                <Row label="Barras almacenadas" value={count(h.data.total_bars)} />
                <Row
                  label="Barra más reciente"
                  value={h.data.latest_bar_ts ? relative(h.data.latest_bar_ts) : "—"}
                />
                <Row label="Suscriptores del stream" value={String(h.stream.subscribers)} />
              </dl>
            </Card>
          </div>

          <Card className="mb-6">
            <CardHeader
              title="Tareas programadas"
              subtitle="Qué corre en cada fase y cuándo toca la próxima."
            />
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-sm">
                <thead>
                  <tr className="border-b border-hairline text-left text-xs text-ink-muted">
                    <th className="py-2.5 pl-5 pr-3 font-medium">Tarea</th>
                    <th className="py-2.5 pr-3 font-medium">Fases</th>
                    <th className="py-2.5 pr-3 text-right font-medium">Intervalo</th>
                    <th className="py-2.5 pr-3 text-right font-medium">Ejecuciones</th>
                    <th className="py-2.5 pr-3 text-right font-medium">Fallos</th>
                    <th className="py-2.5 pr-5 font-medium">Próxima</th>
                  </tr>
                </thead>
                <tbody className="tabular">
                  {h.orchestrator.tasks.map((task) => (
                    <tr key={task.name} className="border-b border-hairline last:border-0">
                      <td className="py-2.5 pl-5 pr-3 text-ink">{task.name}</td>
                      <td className="py-2.5 pr-3 text-xs text-ink-muted">
                        {task.phases.map((p) => PHASE_LABEL[p] ?? p).join(", ")}
                      </td>
                      <td className="py-2.5 pr-3 text-right text-ink-secondary">
                        {duration(task.interval_seconds)}
                      </td>
                      <td className="py-2.5 pr-3 text-right text-ink-secondary">
                        {count(task.runs)}
                      </td>
                      <td
                        className={`py-2.5 pr-3 text-right ${
                          task.failures > 0 ? "text-down" : "text-ink-muted"
                        }`}
                      >
                        {task.failures}
                      </td>
                      <td className="py-2.5 pr-5 text-xs text-ink-muted">
                        {task.next_due ? dateTime(task.next_due) : "inactiva en esta fase"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      ) : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Últimos jobs" />
          {!jobs.data?.length ? (
            <Empty>Sin jobs registrados.</Empty>
          ) : (
            <ul className="divide-y divide-hairline text-sm">
              {jobs.data.map((job) => (
                <li key={job.id} className="flex items-center gap-3 px-5 py-2.5">
                  <Badge tone={job.success === null ? "warn" : job.success ? "good" : "bad"}>
                    {job.success === null ? "en curso" : job.success ? "ok" : "fallo"}
                  </Badge>
                  <span className="text-ink-secondary">{job.job}</span>
                  <span className="tabular ml-auto text-xs text-ink-muted">
                    {job.duration_ms !== null ? `${Math.round(job.duration_ms)} ms` : "—"}
                  </span>
                  <span className="text-xs text-ink-muted">{relative(job.started_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card>
          <CardHeader title="Eventos del sistema" />
          {!events.data?.length ? (
            <Empty>Sin eventos registrados.</Empty>
          ) : (
            <ul className="divide-y divide-hairline text-sm">
              {events.data.map((event) => (
                <li key={event.id} className="px-5 py-2.5">
                  <div className="flex items-center gap-2">
                    <Badge tone={event.level === "error" ? "bad" : "neutral"}>
                      {event.category}
                    </Badge>
                    <span className="text-ink-secondary">{event.event}</span>
                    <span className="ml-auto text-xs text-ink-muted">
                      {relative(event.ts)}
                    </span>
                  </div>
                  {event.message && (
                    <p className="mt-1 text-xs text-ink-muted">{event.message}</p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 px-5 py-2.5">
      <dt className="text-ink-muted">{label}</dt>
      <dd className="tabular text-right text-ink-secondary">{value}</dd>
    </div>
  );
}
