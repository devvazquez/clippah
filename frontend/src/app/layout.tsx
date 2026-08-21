import type { Metadata } from "next";
import { Toaster } from "sonner";
import { SocialLinks } from "@/components/SocialLinks";
import "./globals.css";

export const metadata: Metadata = {
  title: "clipper — clips verticales de tus directos",
  description:
    "Encola directos de Twitch o YouTube y descarga los clips verticales ya montados, listos para subir.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es" className="dark">
      <body className="min-h-screen bg-base">
        <div className="mx-auto max-w-[1400px] px-4 py-5 sm:px-6 lg:px-8">
          {children}
          <footer className="mt-12 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
            <SocialLinks />
            <span className="text-[11px] text-ink-faint">
              Estas tres redes van quemadas en cada clip. Música de Kevin MacLeod
              (CC BY 3.0): acredítala al publicar.
            </span>
          </footer>
        </div>
        <Toaster
          theme="dark"
          position="bottom-right"
          toastOptions={{
            style: {
              background: "#18181B",
              border: "1px solid #26262B",
              color: "#EDEDEF",
              fontSize: "13px",
            },
          }}
        />
      </body>
    </html>
  );
}
