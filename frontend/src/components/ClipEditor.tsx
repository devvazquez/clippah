"use client";

import { Loader2, Music, Play, Plus, RotateCcw, Save, Trash2, Type } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { saveClipEdit } from "@/lib/supabase";
import { type Clip, type Cue, MUSIC_TRACKS, SFX_KINDS, type SfxCue } from "@/lib/types";

/** `0:04.2` — con decima, que es la precision con la que se ve encajar una frase. */
function stamp(seconds: number): string {
  const s = Math.max(0, seconds);
  const m = Math.floor(s / 60);
  return `${m}:${(s % 60).toFixed(1).padStart(4, "0")}`;
}

function sameCues(a: Cue[], b: Cue[]): boolean {
  return a.length === b.length && a.every((c, i) => c.text === b[i].text);
}

function sameSfx(a: SfxCue[], b: SfxCue[]): boolean {
  return (
    a.length === b.length &&
    a.every((c, i) => c.name === b[i].name && Math.abs(c.t - b[i].t) < 0.01)
  );
}

/**
 * Retoca lo que lleva puesto un clip y lo vuelve a renderizar.
 *
 * Dos pestanas porque son dos tareas distintas: corregir lo que Whisper oyo mal, y
 * decidir que suena. Se guarda una vez y el clip se rehace una vez, aunque se hayan
 * tocado las dos cosas.
 *
 * De los subtitulos solo se edita el texto: los tiempos salen de las palabras transcritas
 * y moverlos a mano descuadra el ritmo con la voz. Los efectos, al contrario, se colocan
 * por tiempo: se para el video donde tiene que sonar y se anade ahi.
 */
export function ClipEditor({
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
  const baseCues = React.useMemo<Cue[]>(
    () => clip.captions_edited ?? clip.captions ?? [],
    [clip.captions, clip.captions_edited],
  );
  const baseSfx = React.useMemo<SfxCue[]>(
    () => clip.sfx_edited ?? clip.sfx_cues ?? [],
    [clip.sfx_cues, clip.sfx_edited],
  );
  const baseMusic = clip.music_edited ?? (clip.music === "ninguna" ? "" : (clip.music ?? ""));

  const [tab, setTab] = React.useState<"texto" | "sonido">("texto");
  const [cues, setCues] = React.useState<Cue[]>(baseCues);
  const [sfx, setSfx] = React.useState<SfxCue[]>(baseSfx);
  const [music, setMusic] = React.useState<string>(baseMusic);
  const [kind, setKind] = React.useState(SFX_KINDS[0].name);
  const [at, setAt] = React.useState(0);
  const [saving, setSaving] = React.useState(false);
  const [playing, setPlaying] = React.useState(-1);
  const video = React.useRef<HTMLVideoElement>(null);

  // Al abrirlo, lo que se ve es lo que hay guardado ahora mismo.
  React.useEffect(() => {
    if (!open) return;
    setCues(baseCues);
    setSfx(baseSfx);
    setMusic(baseMusic);
  }, [open, baseCues, baseSfx, baseMusic]);

  const cuesDirty = !sameCues(cues, baseCues);
  const sfxDirty = !sameSfx(sfx, baseSfx);
  const musicDirty = music !== baseMusic;
  const dirty = cuesDirty || sfxDirty || musicDirty;

  function seek(t: number, index = -1) {
    const el = video.current;
    if (!el) return;
    el.currentTime = t;
    void el.play();
    setPlaying(index);
  }

  function addSfx() {
    const spec = SFX_KINDS.find((k) => k.name === kind) ?? SFX_KINDS[0];
    const t = video.current ? video.current.currentTime : at;
    setSfx((prev) =>
      [...prev, { name: spec.name, t, gain_db: spec.gain_db }].sort((a, b) => a.t - b.t),
    );
    setAt(t);
  }

  async function save() {
    if (saving || !dirty) return;
    setSaving(true);
    try {
      await saveClipEdit(clip.id, {
        cues: cuesDirty ? cues : undefined,
        sfx: sfxDirty ? sfx : undefined,
        music: musicDirty ? music : undefined,
      });
      toast.success("Guardado: el backend está rehaciendo el clip");
      onClose();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "No se pudo guardar");
    } finally {
      setSaving(false);
    }
  }

  const tabs = [
    { key: "texto" as const, label: "Subtítulos", icon: Type, dirty: cuesDirty },
    { key: "sonido" as const, label: "Sonido", icon: Music, dirty: sfxDirty || musicDirty },
  ];

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={clip.title || clip.moment_id}
      description={`${cues.length} frases · ${sfx.length} efectos · ${Math.round(clip.duration_s)} s · v${clip.version}`}
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
            {tab === "texto"
              ? "Pincha una frase para ver ese momento. Al guardar, el clip se vuelve a renderizar con el texto nuevo."
              : "Para colocar un efecto, para el vídeo justo donde tiene que sonar y dale a añadir."}
          </p>
        </div>

        <div className="flex flex-col gap-3">
          <div className="flex gap-1.5">
            {tabs.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setTab(t.key)}
                aria-pressed={tab === t.key}
                className={`flex h-8 items-center gap-1.5 rounded-md border px-3 text-[12px] transition-colors ${
                  tab === t.key
                    ? "border-accent bg-accent/10 text-ink"
                    : "border-line text-ink-dim hover:bg-surface-2"
                }`}
              >
                <t.icon className="h-3.5 w-3.5" aria-hidden />
                {t.label}
                {t.dirty ? (
                  <span className="h-1.5 w-1.5 rounded-full bg-accent" aria-label="sin guardar" />
                ) : null}
              </button>
            ))}
          </div>

          {tab === "texto" ? (
            /* Altura propia para la lista: dejando que la caja crezca con las frases, el
               pie con el boton de guardar se salia de la pantalla. */
            <ul className="flex max-h-[44vh] flex-col gap-1.5 overflow-y-auto pr-1">
              {cues.length === 0 ? (
                <li className="rounded border border-line px-3 py-6 text-center text-[12px] text-ink-faint">
                  Este clip no tiene subtítulos guardados. Los generados antes de esta
                  versión no los traen; se rellenan al volver a renderizarlos.
                </li>
              ) : null}
              {cues.map((cue, i) => (
                <li key={`${cue.start}-${i}`} className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => seek(cue.start, i)}
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
                    onChange={(e) =>
                      setCues((prev) =>
                        prev.map((c, j) => (j === i ? { ...c, text: e.target.value } : c)),
                      )
                    }
                    spellCheck
                    aria-label={`Frase en ${stamp(cue.start)}`}
                    className={`h-9 w-full rounded-md border bg-surface px-3 text-[13px] text-ink focus-visible:border-accent/60 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 ${
                      cue.text !== (baseCues[i]?.text ?? "") ? "border-accent/50" : "border-line"
                    }`}
                  />
                </li>
              ))}
            </ul>
          ) : (
            <div className="flex max-h-[44vh] flex-col gap-4 overflow-y-auto pr-1">
              <section className="flex flex-col gap-2">
                <h3 className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
                  Música de fondo
                </h3>
                <div className="flex flex-wrap gap-1.5">
                  {MUSIC_TRACKS.map((t) => (
                    <button
                      key={t.key || "ninguna"}
                      type="button"
                      onClick={() => setMusic(t.key)}
                      aria-pressed={music === t.key}
                      className={`h-8 rounded-md border px-3 text-[12px] transition-colors ${
                        music === t.key
                          ? "border-accent bg-accent/10 text-ink"
                          : "border-line text-ink-dim hover:bg-surface-2"
                      }`}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
                <p className="text-[11px] text-ink-faint">
                  Va 30 dB por debajo de la voz: acompaña, no tapa. Kevin MacLeod, CC BY
                  3.0 — acredítala al publicar.
                </p>
              </section>

              <section className="flex flex-col gap-2">
                <h3 className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
                  Efectos de sonido
                </h3>
                <div className="flex flex-wrap items-center gap-1.5">
                  {SFX_KINDS.map((k) => (
                    <button
                      key={k.name}
                      type="button"
                      onClick={() => setKind(k.name)}
                      aria-pressed={kind === k.name}
                      className={`h-8 rounded-md border px-3 text-[12px] transition-colors ${
                        kind === k.name
                          ? "border-accent bg-accent/10 text-ink"
                          : "border-line text-ink-dim hover:bg-surface-2"
                      }`}
                    >
                      {k.label}
                    </button>
                  ))}
                  <Button size="sm" variant="subtle" onClick={addSfx}>
                    <Plus className="h-3.5 w-3.5" aria-hidden />
                    Añadir en {stamp(video.current?.currentTime ?? at)}
                  </Button>
                </div>

                <ul className="flex flex-col gap-1.5">
                  {sfx.length === 0 ? (
                    <li className="rounded border border-line px-3 py-4 text-center text-[12px] text-ink-faint">
                      Sin efectos. El clip se renderizará limpio.
                    </li>
                  ) : null}
                  {sfx.map((cue, i) => (
                    <li key={`${cue.name}-${cue.t}-${i}`} className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => seek(cue.t)}
                        title="Escuchar ese momento"
                        className="tnum flex h-9 shrink-0 items-center gap-1 rounded border border-line px-2 text-[11px] text-ink-faint transition-colors hover:bg-surface-2 hover:text-ink"
                      >
                        <Play className="h-3 w-3" aria-hidden />
                        {stamp(cue.t)}
                      </button>
                      <span className="flex h-9 flex-1 items-center rounded-md border border-line px-3 text-[13px] text-ink">
                        {SFX_KINDS.find((k) => k.name === cue.name)?.label ?? cue.name}
                      </span>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => setSfx((prev) => prev.filter((_, j) => j !== i))}
                        title="Quitar este efecto"
                      >
                        <Trash2 className="h-3.5 w-3.5" aria-hidden />
                      </Button>
                    </li>
                  ))}
                </ul>
              </section>
            </div>
          )}

          <div className="flex items-center justify-between gap-2 border-t border-line pt-3">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setCues(baseCues);
                setSfx(baseSfx);
                setMusic(baseMusic);
              }}
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
