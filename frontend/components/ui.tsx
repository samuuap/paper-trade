import type { ReactNode } from "react";

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-xl border border-hairline bg-card ${className}`}
    >
      {children}
    </section>
  );
}

export function CardHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <header className="flex items-start justify-between gap-4 border-b border-hairline px-5 py-4">
      <div>
        <h2 className="text-sm font-semibold tracking-tight text-ink">{title}</h2>
        {subtitle && (
          <p className="mt-0.5 text-xs leading-relaxed text-ink-muted">{subtitle}</p>
        )}
      </div>
      {action}
    </header>
  );
}

export function PageTitle({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {subtitle && <p className="mt-1 max-w-2xl text-sm text-ink-muted">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="px-5 py-10 text-center text-sm text-ink-muted">{children}</div>
  );
}

export function Loading({ label = "Cargando…" }: { label?: string }) {
  return <div className="px-5 py-10 text-center text-sm text-ink-muted">{label}</div>;
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div className="px-5 py-8 text-center text-sm text-down">
      No se pudo cargar: {message}
      <p className="mt-2 text-xs text-ink-muted">
        ¿Está el backend levantado en {process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api"}?
      </p>
    </div>
  );
}

/** Colour chip. Always sits beside a name — never carries identity alone. */
export function Chip({ color }: { color: string }) {
  return (
    <span
      className="inline-block size-2.5 shrink-0 rounded-[2px]"
      style={{ background: color }}
      aria-hidden
    />
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "bad" | "warn";
}) {
  const tones = {
    neutral: "border-hairline text-ink-muted",
    good: "border-up/40 text-up",
    bad: "border-down/40 text-down",
    warn: "border-warning/40 text-warning",
  } as const;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}
