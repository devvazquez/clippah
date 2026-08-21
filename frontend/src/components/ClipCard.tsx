"use client";

import { Copy, Download, ExternalLink } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { downloadUrl } from "@/lib/supabase";
import { type Clip, MUSIC_LABELS, SFX_LABELS } from "@/lib/types";
import { hhmmss, scoreTone, twitchTime } from "@/lib/utils";

/** Nombre con el que se guarda el mp4: reconocible en la carpeta de descargas. */
function filename(clip: Clip): string {
  const slug =
    clip.title
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 50) || clip.moment_id;
  return `${slug}.mp4`;
}

function vodLink(clip: Clip): string | null {
  if (!clip.video_url) return null;
  const t = clip.t_start ?? 0;
  return clip.video_url.includes("twitch.tv")
    ? `${clip.video_url}?t=${twitchTime(t)}`
    : `${clip.video_url}&t=${Math.floor(t)}`;
}

export function ClipCard({ clip, src }: { clip: Clip; src?: string }) {
  const tone = scoreTone(clip.score ?? 0);
  const music = MUSIC_LABELS[clip.music ?? ""] ?? "";
  const sfx = SFX_LABELS[clip.sfx ?? ""] ?? "";
  const link = vodLink(clip);

  async function copyTitle() {
    try {
      await navigator.clipboard.writeText(clip.title);
      toast.success("Título copiado");
    } catch {
      toast.error("El navegador no dejó copiar");
    }
  }

  return (
    <article className="card animate-fade-in flex flex-col overflow-hidden">
      <div className="relative bg-black">
        {src ? (
          // eslint-disable-next-line jsx-a11y/media-has-caption -- los subtitulos van quemados
          <video
            // El fragmento #t hace que el navegador busque ese segundo y pinte el
            // fotograma; con preload="metadata" a secas se queda en negro.
            src={`${src}#t=0.5`}
            controls
            preload="metadata"
            playsInline
            className="aspect-[9/16] w-full"
          />
        ) : (
          <div className="aspect-[9/16] w-full animate-pulse bg-surface-2" />
        )}
      </div>

      <div className="flex flex-1 flex-col gap-2.5 p-3">
        <h3 className="text-[13.5px] font-medium leading-snug text-ink">{clip.title}</h3>

        <p className="tnum flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-ink-faint">
          <span className={tone.text}>{(clip.score ?? 0).toFixed(2)}</span>
          <span>·</span>
          <span>{Math.round(clip.duration_s)} s</span>
          <span>·</span>
          <span>{(clip.size_bytes / 1048576).toFixed(1)} MB</span>
          {clip.source === "vision" ? (
            <>
              <span>·</span>
              <span className="text-accent-soft">lo vio Gemini</span>
            </>
          ) : null}
        </p>

        {music || sfx ? (
          <p className="flex flex-wrap gap-1.5">
            {sfx ? (
              <span className="rounded border border-line px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-ink-dim">
                {sfx}
              </span>
            ) : null}
            {music ? (
              <span className="rounded border border-line px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-ink-dim">
                {music}
              </span>
            ) : null}
          </p>
        ) : null}

        {clip.t_start != null ? (
          // El directo lo dice la cabecera del grupo: aqui solo hace falta el minuto.
          <p className="tnum text-[11px] text-ink-faint">minuto {hhmmss(clip.t_start)}</p>
        ) : null}

        <div className="mt-auto flex items-center gap-1.5 pt-1">
          <Button
            size="sm"
            className="flex-1"
            disabled={!src}
            onClick={() => {
              if (!src) return;
              // Un <a> normal: Storage manda el mp4 con Content-Disposition y el
              // navegador lo guarda sin pasar por otra pestaña.
              const a = document.createElement("a");
              a.href = downloadUrl(src, filename(clip));
              a.rel = "noreferrer";
              a.click();
            }}
          >
            <Download className="h-3.5 w-3.5" aria-hidden />
            Descargar
          </Button>
          <Button variant="outline" size="icon" onClick={copyTitle} title="Copiar el título">
            <Copy className="h-3.5 w-3.5" aria-hidden />
          </Button>
          {link ? (
            <a
              href={link}
              target="_blank"
              rel="noreferrer"
              title="Ver el momento en el directo"
              className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-line text-ink transition-colors hover:bg-surface-2"
            >
              <ExternalLink className="h-3.5 w-3.5" aria-hidden />
            </a>
          ) : null}
        </div>
      </div>
    </article>
  );
}
