"use client";

import { Loader2, Plus } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { queueRequest } from "@/lib/supabase";
import { isSupportedUrl } from "@/lib/utils";

const MAX_CLIPS = 10;
const MAX_PROMPT = 300;

/**
 * Atajos para no escribir en el móvil. El primero es lo que hace el análisis por su
 * cuenta, así que sirve de ejemplo de qué esperar; los otros son los dos encargos que
 * de verdad se piden: un tipo de momento, o una cosa concreta que hay que ir a buscar.
 */
const EJEMPLOS = [
  "el más gracioso",
  "un fail o una muerte tonta",
  "donde grita o se asusta",
  "busca donde dicen ",
];

/** Encolar un directo: enlace, qué buscar y cuántos clips. */
export function QueueForm({ onQueued }: { onQueued: () => void }) {
  const [url, setUrl] = React.useState("");
  const [prompt, setPrompt] = React.useState("");
  const [clips, setClips] = React.useState(3);
  const [saving, setSaving] = React.useState(false);
  const promptRef = React.useRef<HTMLTextAreaElement>(null);

  const trimmed = url.trim();
  const looksOk = trimmed.length > 12 && isSupportedUrl(trimmed);

  function usarEjemplo(texto: string) {
    setPrompt(texto);
    // Los ejemplos que acaban en espacio están a medias a propósito ("busca donde dicen
    // ..."): el foco al final deja seguir escribiendo sin tocar nada más.
    if (texto.endsWith(" ")) {
      requestAnimationFrame(() => {
        const el = promptRef.current;
        if (!el) return;
        el.focus();
        el.setSelectionRange(texto.length, texto.length);
      });
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (saving || !looksOk) return;
    setSaving(true);
    try {
      await queueRequest(trimmed, clips, prompt);
      setUrl("");
      setPrompt("");
      toast.success(`Guardado en la cola: ${clips} clip${clips > 1 ? "s" : ""}`);
      onQueued();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "No se pudo guardar");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={submit} className="card flex flex-col gap-3 p-4">
      <div className="flex flex-col gap-1.5">
        <label htmlFor="vod" className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
          Enlace del directo
        </label>
        <Input
          id="vod"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://www.twitch.tv/videos/2851488297"
          spellCheck={false}
          autoComplete="off"
          inputMode="url"
          aria-invalid={trimmed.length > 12 && !looksOk}
        />
        {trimmed.length > 12 && !looksOk ? (
          <p className="text-[11px] text-warn">
            Tiene que ser un VOD de Twitch o un vídeo de YouTube.
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor="pide" className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
          Qué buscar <span className="normal-case tracking-normal">(opcional)</span>
        </label>
        <textarea
          id="pide"
          ref={promptRef}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value.slice(0, MAX_PROMPT))}
          rows={2}
          placeholder="el más gracioso · busca donde le llaman jopa"
          className="w-full resize-y rounded-md border border-line bg-surface px-3.5 py-2.5 text-sm text-ink placeholder:text-ink-faint focus-visible:border-accent/60 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
        />
        <div className="flex flex-wrap gap-1.5">
          {EJEMPLOS.map((texto) => (
            <button
              key={texto}
              type="button"
              onClick={() => usarEjemplo(texto)}
              className="rounded-full border border-line px-2.5 py-1 text-[11px] text-ink-dim transition-colors hover:bg-surface-2"
            >
              {texto.trim()}
              {texto.endsWith(" ") ? "…" : ""}
            </button>
          ))}
        </div>
        <p className="text-[11px] text-ink-faint">
          Si lo dejas vacío saca los momentos con más reacción. Si escribes{" "}
          <em className="not-italic text-ink-dim">busca…</em> o{" "}
          <em className="not-italic text-ink-dim">donde…</em>, además va a buscar esa
          palabra en el chat del directo y en las transcripciones guardadas.
        </p>
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
          Cuántos clips
        </label>
        <div className="flex flex-wrap gap-1.5">
          {Array.from({ length: MAX_CLIPS }, (_, i) => i + 1).map((n) => (
            <button
              key={n}
              type="button"
              onClick={() => setClips(n)}
              aria-pressed={clips === n}
              className={`tnum h-9 w-9 rounded-md border text-[13px] transition-colors ${
                clips === n
                  ? "border-accent bg-accent/15 text-ink"
                  : "border-line text-ink-dim hover:bg-surface-2"
              }`}
            >
              {n}
            </button>
          ))}
        </div>
        <p className="text-[11px] text-ink-faint">
          {prompt.trim()
            ? `Los ${clips} momentos que mejor encajen, ya renderizados en vertical.`
            : `Los mejores ${clips} momentos del directo, ya renderizados en vertical.`}
        </p>
      </div>

      <Button type="submit" size="lg" disabled={!looksOk || saving}>
        {saving ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        ) : (
          <Plus className="h-4 w-4" aria-hidden />
        )}
        Guardar en la cola
      </Button>
    </form>
  );
}
