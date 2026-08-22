import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** `1:24:07` / `4:12` — el formato que la gente lee en un reproductor. */
export function hhmmss(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

/** `1h24m07s`, el formato que acepta el reproductor de Twitch en `?t=`. */
export function twitchTime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h}h${m.toString().padStart(2, "0")}m${sec.toString().padStart(2, "0")}s`;
}

export function durationLabel(seconds: number): string {
  if (seconds >= 3600) {
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    return m ? `${h} h ${m} min` : `${h} h`;
  }
  return `${Math.max(1, Math.round(seconds / 60))} min`;
}

export function relativeDate(epochSeconds: number): string {
  if (!epochSeconds) return "";
  const diff = Date.now() / 1000 - epochSeconds;
  if (diff < 60) return "hace unos segundos";
  if (diff < 3600) return `hace ${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `hace ${Math.floor(diff / 3600)} h`;
  if (diff < 86400 * 7) return `hace ${Math.floor(diff / 86400)} d`;
  return new Date(epochSeconds * 1000).toLocaleDateString("es-ES", {
    day: "2-digit",
    month: "short",
  });
}

/** `20260822` → `22 ago 2026`. Con todos los directos titulados igual, es la etiqueta. */
export function streamDate(raw: string | null | undefined): string {
  const m = /^(\d{4})(\d{2})(\d{2})$/.exec((raw ?? "").trim());
  if (!m) return "";
  const [, y, mo, d] = m;
  return new Date(Number(y), Number(mo) - 1, Number(d)).toLocaleDateString("es-ES", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** Nombre con el que se guarda el mp4: la fecha del directo delante, para que ordenen. */
export function clipFilename(clip: {
  title: string;
  moment_id: string;
  video_date?: string | null;
}): string {
  const slug =
    clip.title
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 50) || clip.moment_id;
  const d = /^(\d{4})(\d{2})(\d{2})$/.exec((clip.video_date ?? "").trim());
  return d ? `${d[1]}-${d[2]}-${d[3]}-${slug}.mp4` : `${slug}.mp4`;
}

export function scoreTone(score: number): { text: string; ring: string; dot: string } {
  if (score >= 0.75) return { text: "text-ok", ring: "ring-ok/30", dot: "bg-ok" };
  if (score >= 0.5) return { text: "text-accent-soft", ring: "ring-accent/30", dot: "bg-accent" };
  if (score >= 0.3) return { text: "text-warn", ring: "ring-warn/30", dot: "bg-warn" };
  return { text: "text-ink-faint", ring: "ring-line", dot: "bg-ink-faint" };
}

const VOD_PATTERNS = [
  /^https?:\/\/(?:www\.|m\.)?twitch\.tv\/videos\/\d+/i,
  /^https?:\/\/(?:www\.|m\.)?twitch\.tv\/[^/]+\/v(?:ideo)?\/\d+/i,
  /^https?:\/\/(?:www\.|m\.)?youtube\.com\/watch\?(?:.*&)?v=[\w-]{6,}/i,
  /^https?:\/\/youtu\.be\/[\w-]{6,}/i,
  /^https?:\/\/(?:www\.)?youtube\.com\/live\/[\w-]{6,}/i,
];

export function isSupportedUrl(raw: string): boolean {
  const url = raw.trim().startsWith("http") ? raw.trim() : `https://${raw.trim()}`;
  return VOD_PATTERNS.some((p) => p.test(url));
}
