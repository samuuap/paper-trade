import type { Metadata } from "next";
import Nav from "@/components/Nav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Paper Trade Arena",
  description:
    "Agentes LLM compitiendo en bolsa US con dinero simulado, instrumentados de punta a punta.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body className="min-h-screen bg-plane">
        <Nav />
        <main className="mx-auto w-full max-w-[1400px] px-6 pb-16 pt-6">{children}</main>
      </body>
    </html>
  );
}
