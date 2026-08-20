"use client";

import { useEffect, useState } from "react";
import { api, type Health } from "@/lib/api";
import { cn } from "@/lib/utils";

export function ProviderStatus() {
  const [health, setHealth] = useState<Health | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .health()
      .then((h) => alive && setHealth(h))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, []);

  if (failed) {
    return (
      <span className="flex items-center gap-1.5 text-xs text-danger">
        <span className="h-1.5 w-1.5 rounded-full bg-danger" />
        Backend no disponible
      </span>
    );
  }
  if (!health) return <span className="h-4 w-24" />;

  const active = health.providers.filter((p) => p.configured && p.name !== "faster-whisper");
  const isLocal = health.mode === "local";
  const label = isLocal
    ? "Modo local (sin API keys) — más lento"
    : active.map((p) => (p.name === "groq" ? "Groq" : "Gemini")).join(" + ");

  const tone = isLocal ? "text-warn" : "text-ok";
  const dot = isLocal ? "bg-warn" : "bg-ok";
  const detail = health.providers
    .map((p) => {
      if (!p.configured) return `${p.name}: no configurado`;
      const left =
        p.requests_limit !== null
          ? `${p.requests_today}/${p.requests_limit} peticiones hoy`
          : p.note;
      return `${p.name}: ${left}`;
    })
    .join("\n");

  return (
    <span className={cn("flex items-center gap-1.5 text-xs", tone)} title={detail}>
      <span className={cn("h-1.5 w-1.5 rounded-full", dot)} />
      {label}
      {!health.ffmpeg && <span className="ml-2 text-danger">falta ffmpeg</span>}
    </span>
  );
}
