import type { Metadata } from "next";
import { Toaster } from "sonner";
import { SocialLinks } from "@/components/SocialLinks";
import "./globals.css";

export const metadata: Metadata = {
  title: "clipper — momentos destacados de VODs",
  description:
    "Pega un VOD de Twitch o YouTube y obtén una lista de momentos con descripción, fotograma y timestamp.",
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
              Los clips salen con estas tres redes quemadas al pie.
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
