"use client";

import { Loader2, Play, RotateCcw, Save } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { saveCaptions } from "@/lib/supabase";
import type { Clip, Cue } from "@/lib/types";

/** `0:04.2` — con decima, que es la precision con la que se ve encajar una frase. */
function stamp(seconds: number): string {
  const s = Math.max(0, seconds);
  const m = Math.floor(s / 60);
  const rest = (s % 60).toFixed(1).padStart(4, "0");
  return `${m}:${rest}`;
}

/**
 * Corrige lo que Whisper oyo mal y vuelve a quemar el clip.
 *
 * Solo se edita el texto: los tiempos salen de las palabras transcritas y moverlos a
 * mano descuadra el ritmo con la voz, que es lo unico que aqui no se puede comprobar de
 * oido. Para saber si una frase es la que toca, se pincha y el video salta a su segundo.
 */
export function CaptionEditor({
  clip,
  src,
  open,
  onClose,
}: {
  clip: Clip;
  src?: string;
  open: boolean;
  onClose: () => void;
}) {
  const original = React.useMemo<Cue[]>(
    () => clip.captions_edited ?? clip.captions ?? [],
    [clip.captions, clip.captions_edited],
  );
  const [cues, setCues] = React.useState<Cue[]>(original);
  const [saving, setSaving] = React.useState(false);
  const [playing, setPlaying] = React.useState(-1);
  const video = React.useRef<HTMLVideoElement>(null);

  // Al abrirlo, lo que se ve es lo que hay guardado ahora mismo.
  React.useEffect(() => {
    if (open) setCues(original);
  }, [open, original]);

  const dirty = React.useMemo(
    () => cues.some((c, i) => c.text !== (original[i]?.text ?? "")),
    [cues, original],
  );

  function edit(index: number, text: string) {
    setCues((prev) => prev.map((c, i) => (i === index ? { ...c, text } : c)));
  }

  function preview(index: number) {
    const el = video.current;
    if (!el) return;
    el.currentTime = cues[index].start;
    void el.play();
    setPlaying(index);
  }

  async function save() {
    if (saving || !dirty) return;
    setSaving(true);
    try {
      await saveCaptions(clip.id, cues);
      toast.success("Guardado: el backend está rehaciendo el clip");
      onClose();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "No se pudo guardar");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={clip.title || clip.moment_id}
      description={`${cues.length} frases · ${Math.round(clip.duration_s)} s · v${clip.version}`}
      className="max-w-4xl"
    >
      <div className="grid gap-4 p-4 sm:grid-cols-[220px_minmax(0,1fr)]">
        <div className="flex flex-col gap-2">
          {src ? (
            // eslint-disable-next-line jsx-a11y/media-has-caption -- van quemados en el mp4
            <video
              ref={video}
              src={src}
              controls
              playsInline
              preload="metadata"
              className="aspect-[9/16] w-full rounded bg-black"
            />
          ) : (
            <div className="aspect-[9/16] w-full animate-pulse rounded bg-surface-2" />
          )}
          <p className="text-[11px] leading-relaxed text-ink-faint">
            Pincha una frase para ver ese momento. Al guardar, el clip se vuelve a
            renderizar con el texto nuevo y aparece aquí ya cambiado.
          </p>
        </div>

        <div className="flex flex-col gap-3">
          {/* Altura propia para la lista: dejando que la caja crezca con las frases, el
              pie con el boton de guardar se salia de la pantalla. */}
          <ul className="flex max-h-[48vh] flex-col gap-1.5 overflow-y-auto pr-1">
            {cues.length === 0 ? (
              <li className="rounded border border-line px-3 py-6 text-center text-[12px] text-ink-faint">
                Este clip no tiene subtítulos guardados. Los clips generados antes de esta
                versión no los traen; se rellenan al volver a renderizarlos.
              </li>
            ) : null}
            {cues.map((cue, i) => (
              <li key={`${cue.start}-${i}`} className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => preview(i)}
                  title="Ver este momento"
                  className={`tnum flex h-9 shrink-0 items-center gap-1 rounded border px-2 text-[11px] transition-colors ${
                    playing === i
                      ? "border-accent text-accent"
                      : "border-line text-ink-faint hover:bg-surface-2 hover:text-ink"
                  }`}
                >
                  <Play className="h-3 w-3" aria-hidden />
                  {stamp(cue.start)}
                </button>
                <input
                  value={cue.text}
                  onChange={(e) => edit(i, e.target.value)}
                  spellCheck
                  aria-label={`Frase en ${stamp(cue.start)}`}
                  className={`h-9 w-full rounded-md border bg-surface px-3 text-[13px] text-ink focus-visible:border-accent/60 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 ${
                    cue.text !== (original[i]?.text ?? "") ? "border-accent/50" : "border-line"
                  }`}
                />
              </li>
            ))}
          </ul>

          <div className="flex items-center justify-between gap-2 border-t border-line pt-3">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setCues(original)}
              disabled={!dirty || saving}
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden />
              Deshacer
            </Button>
            <Button size="sm" onClick={save} disabled={!dirty || saving}>
              {saving ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <Save className="h-3.5 w-3.5" aria-hidden />
              )}
              Guardar y rehacer el clip
            </Button>
          </div>
        </div>
      </div>
    </Dialog>
  );
}
