/** Attribution for fetched artist prose (issue #170).
 *
 * Wikipedia's lead extracts are CC BY-SA: crediting the source and linking
 * back is part of using the text properly, not decoration. Extracted rather
 * than inlined because there are two real callers -- the artist hero and the
 * album page's About panel -- which is the bar for a shared component here.
 *
 * Renders NOTHING for an owner-written bio (Todd crediting Todd reads as a
 * bug), for a bio with no recorded source (a pre-#170 catalog), and for no
 * bio at all. That absence is deliberate: the surfaces above it must look
 * exactly as they did before the fetcher ran.
 */

const LABELS: Record<string, string> = {
  wikipedia: "Wikipedia",
  lastfm: "Last.fm",
};

export default function BioSource({
  bio,
  src,
  url,
}: {
  bio: string;
  src?: string | null;
  url?: string | null;
}) {
  const label = src ? LABELS[src] : undefined;
  if (!bio || !label) return null;
  return (
    <p className="stamp mt-2 text-[10px] text-inkfaint">
      Source ·{" "}
      {url ? (
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          className="underline decoration-line underline-offset-4 transition-colors hover:decoration-linebright"
        >
          {label}
        </a>
      ) : (
        label
      )}
    </p>
  );
}
