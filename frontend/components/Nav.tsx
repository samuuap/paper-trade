"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import LiveStatus from "./LiveStatus";

const LINKS = [
  { href: "/", label: "Resumen" },
  { href: "/trades", label: "Operaciones" },
  { href: "/decisions", label: "Decisiones" },
  { href: "/economics", label: "Economía" },
  { href: "/system", label: "Sistema" },
];

export default function Nav() {
  const pathname = usePathname();
  return (
    <nav className="sticky top-0 z-20 border-b border-hairline bg-plane/90 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1400px] items-center gap-6 px-6 py-3">
        <Link href="/" className="text-sm font-semibold tracking-tight text-ink">
          Paper Trade <span className="text-ink-muted">Arena</span>
        </Link>
        <div className="flex items-center gap-1">
          {LINKS.map((link) => {
            const active =
              link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={`rounded px-2.5 py-1 text-xs transition-colors ${
                  active
                    ? "bg-raised text-ink"
                    : "text-ink-muted hover:text-ink-secondary"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </div>
        <div className="ml-auto">
          <LiveStatus />
        </div>
      </div>
    </nav>
  );
}
