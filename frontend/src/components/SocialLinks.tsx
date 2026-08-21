const LINKS = [
  {
    name: "TikTok",
    label: "@rexxyconh",
    url: "https://www.tiktok.com/@rexxyconh",
    // Nota musical del logo, simplificada.
    path: "M12.5 2h2.4c.2 1.6 1.1 2.9 2.6 3.3v2.4c-1-.05-1.9-.35-2.7-.9v4.9a4.3 4.3 0 1 1-4.3-4.3c.2 0 .4 0 .6.05v2.5a1.85 1.85 0 1 0 1.4 1.8V2Z",
    color: "#25F4EE",
  },
  {
    name: "YouTube",
    label: "REXXYCONH",
    url: "https://www.youtube.com/channel/UCNI69ziiM4dUYL7N6Mi4I1g",
    path: "M17.6 6.2c-.2-.8-.8-1.4-1.6-1.6C14.6 4.2 10 4.2 10 4.2s-4.6 0-6 .4c-.8.2-1.4.8-1.6 1.6C2 7.6 2 10 2 10s0 2.4.4 3.8c.2.8.8 1.4 1.6 1.6 1.4.4 6 .4 6 .4s4.6 0 6-.4c.8-.2 1.4-.8 1.6-1.6.4-1.4.4-3.8.4-3.8s0-2.4-.4-3.8ZM8.2 12.7V7.3L13 10l-4.8 2.7Z",
    color: "#FF0000",
  },
  {
    name: "Twitch",
    label: "/rexxyconh",
    url: "https://www.twitch.tv/rexxyconh",
    path: "M3.4 2 2 5.4v11.2h3.9V19h2.2l2.3-2.4h3.4L18.6 12V2H3.4Zm13.8 9.3-2.7 2.8h-3.9l-2.3 2.3v-2.3H5.1V3.5h12.1v7.8ZM13 6h1.6v4.6H13V6Zm-4.3 0h1.6v4.6H8.7V6Z",
    color: "#9146FF",
  },
];

/** Las tres redes del streamer. Los mismos logos que se queman en el clip. */
export function SocialLinks({ className = "" }: { className?: string }) {
  return (
    <nav className={`flex flex-wrap items-center gap-x-4 gap-y-1.5 ${className}`} aria-label="Redes del streamer">
      {LINKS.map((l) => (
        <a
          key={l.name}
          href={l.url}
          target="_blank"
          rel="noreferrer"
          className="group flex items-center gap-1.5 text-[11px] text-ink-faint transition-colors hover:text-ink"
          title={`${l.name}: ${l.label}`}
        >
          <svg viewBox="0 0 20 20" className="h-3.5 w-3.5 shrink-0" aria-hidden>
            <path d={l.path} fill="currentColor" style={{ color: l.color }} />
          </svg>
          <span className="tnum">{l.label}</span>
        </a>
      ))}
    </nav>
  );
}
