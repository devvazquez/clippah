"use client";

import { ImageOff } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";

/**
 * Miniatura con degradado elegante si la imagen no carga: las URLs de las CDN de
 * Twitch/YouTube caducan y no deben dejar un icono de imagen rota en la UI.
 */
export function Thumb({
  src,
  alt = "",
  className,
  iconClassName,
}: {
  src?: string | null;
  alt?: string;
  className?: string;
  iconClassName?: string;
}) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) {
    return (
      <div
        className={cn(
          "flex h-full w-full items-center justify-center bg-surface-2",
          className,
        )}
      >
        <ImageOff className={cn("h-3.5 w-3.5 text-ink-faint", iconClassName)} />
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
      className={cn("h-full w-full object-cover", className)}
    />
  );
}
