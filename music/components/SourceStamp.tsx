/** Provenance line for fetched or lifted prose (issues #170, #171).
 *
 * Wikipedia's lead extracts are CC BY-SA, so crediting the source and linking
 * back is part of using the text properly rather than decoration. Album
 * descriptions carry a stamp for a plainer reason: the reader should know the
 * paragraph came out of the file's own tags and not from us.
 *
 * Renders NOTHING for owner-written text (Todd crediting Todd reads as a
 * bug), for text with no recorded source (a pre-#170/#171 catalog), and for
 * no text at all. That absence is deliberate -- the surfaces above it must
 * look exactly as they did before either pass ran.
 */

const LABELS: Record<string, string> = {
  wikipedia: "Wikipedia",
  lastfm: "Last.fm",
  // Store copy that rode in on the files. Named for where it actually came
  // from -- claiming a publisher we cannot verify would be worse than vague.
  "comment-tag": "the album's tags",
};

export default function SourceStamp({
  text,
  src,
  url,
}: {
  text: string;
  src?: string | null;
  url?: string | null;
}) {
  const label = src ? LABELS[src] : undefined;
  if (!text || !label) return null;
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
