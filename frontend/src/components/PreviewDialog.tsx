"use client";

import { ExternalLink } from "lucide-react";
import { Dialog } from "@/components/ui/dialog";
import { type Moment, type Video, embedUrl, watchUrl } from "@/lib/api";
import { hhmmss } from "@/lib/utils";

/**
 * Previsualizacion con el reproductor embebido de la plataforma arrancando en `t_start`.
 * Da verificacion real del momento sin implementar el render del clip.
 */
export function PreviewDialog({
  moment,
  video,
  onClose,
}: {
  moment: Moment | null;
  video: Video | null;
  onClose: () => void;
}) {
  const open = Boolean(moment && video);
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={moment?.title}
      description={
        moment
          ? `${hhmmss(moment.t_start)} → ${hhmmss(moment.t_end)} · ${Math.round(
              moment.duration,
            )} s`
          : undefined
      }
      className="max-w-4xl"
    >
      {moment && video ? (
        <div>
          <div className="aspect-video w-full bg-black">
            <iframe
              key={`${moment.id}-${moment.t_start}`}
              src={embedUrl(video, moment.t_start)}
              title={moment.title}
              className="h-full w-full"
              allowFullScreen
              allow="autoplay; fullscreen; encrypted-media; picture-in-picture"
            />
          </div>
          <div className="flex items-start justify-between gap-4 px-4 py-3">
            <p className="max-w-2xl text-xs leading-relaxed text-ink-dim">
              {moment.transcript || moment.description}
            </p>
            <a
              href={watchUrl(video, moment.t_start)}
              target="_blank"
              rel="noreferrer"
              className="flex shrink-0 items-center gap-1 text-xs text-accent-soft hover:underline"
            >
              Ver en {video.platform === "twitch" ? "Twitch" : "YouTube"}
              <ExternalLink className="h-3 w-3" />
            </a>
          </div>
        </div>
      ) : null}
    </Dialog>
  );
}
