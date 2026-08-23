/** Las dos tablas que comparten la interfaz y el backend (ver `supabase/schema.sql`). */

export type RequestStatus =
  | "queued"
  | "claimed"
  | "running"
  | "done"
  | "error"
  | "canceled";

export interface ClipRequest {
  id: string;
  url: string;
  clips: number;
  /** Lo que se pidio en una frase, si se escribio algo. */
  prompt: string | null;
  status: RequestStatus;
  stage: string;
  progress: number;
  message: string;
  error: string | null;
  video_title: string | null;
  video_url: string | null;
  duration_s: number | null;
  clips_done: number;
  job_id: string | null;
  worker: string | null;
  created_at: string;
  claimed_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

export type RenderStatus = "ready" | "rerender_queued" | "rendering" | "error";

/** Una frase de subtitulo, en segundos desde el inicio del clip. */
export interface Cue {
  text: string;
  start: number;
  end: number;
}

/** Un efecto colocado en el clip: `name` es el fichero de `backend/assets/sfx`. */
export interface SfxCue {
  name: string;
  t: number;
  gain_db?: number;
}

export interface Clip {
  id: string;
  request_id: string | null;
  moment_id: string;
  rank: number;
  title: string;
  storage_path: string;
  size_bytes: number;
  duration_s: number;
  width: number;
  height: number;
  video_url: string | null;
  video_title: string | null;
  /** Fecha del directo (AAAAMMDD) y portada del clip en el bucket, para las tarjetas. */
  video_date: string | null;
  poster_path: string | null;
  t_start: number | null;
  t_end: number | null;
  score: number | null;
  clip_score: number | null;
  source: string | null;
  category: string | null;
  sfx: string | null;
  music: string | null;
  transcript: string | null;
  reason: string | null;
  created_at: string;
  // Subtitulos: `captions` es lo que hay quemado; `captions_edited` lo que espera render.
  /** `title` es el que lleva quemado el mp4; `title_edited`, el que espera render. */
  title_edited: string | null;
  captions: Cue[];
  captions_edited: Cue[] | null;
  sfx_cues: SfxCue[];
  sfx_edited: SfxCue[] | null;
  music_edited: string | null;
  render_status: RenderStatus;
  render_error: string | null;
  version: number;
}

/** Las pistas que hay en `backend/assets/music`, en el orden en que se ofrecen. */
export const MUSIC_TRACKS: { key: string; label: string }[] = [
  { key: "", label: "Ninguna" },
  { key: "fluffing_a_duck", label: "Fluffing a Duck" },
  { key: "sneaky_snitch", label: "Sneaky Snitch" },
  { key: "sneaky_adventure", label: "Sneaky Adventure" },
];

/** Los efectos que hay en `backend/assets/sfx`. `gain_db` es el nivel al colocarlos. */
export const SFX_KINDS: { name: string; label: string; gain_db: number }[] = [
  { name: "vineboom.mp3", label: "Golpe", gain_db: -9 },
  { name: "riser-short.mp3", label: "Riser corto", gain_db: -7 },
  { name: "riser-long.mp3", label: "Riser largo", gain_db: -7 },
];

export const MUSIC_LABELS: Record<string, string> = {
  fluffing_a_duck: "Fluffing a Duck",
  sneaky_snitch: "Sneaky Snitch",
  sneaky_adventure: "Sneaky Adventure",
  ninguna: "",
};

export const SFX_LABELS: Record<string, string> = {
  golpe: "golpe seco",
  riser_golpe: "riser + golpe",
  ninguno: "",
};

/** Lo que esta pasando ahora mismo, en palabras, para cada etapa del pipeline. */
export const STAGE_LABELS: Record<string, string> = {
  queued: "En cola",
  ingest: "Leyendo el VOD",
  audio: "Extrayendo audio",
  chat: "Descargando chat",
  signals: "Midiendo señales",
  vision: "Mirando la pantalla",
  candidates: "Eligiendo candidatos",
  transcribe: "Transcribiendo",
  score: "Puntuando",
  frames: "Sacando fotogramas",
  render: "Renderizando",
  done: "Listo",
  error: "Error",
};
