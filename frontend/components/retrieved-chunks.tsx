import type { RetrievedChunk } from "@/lib/types";

const MODALITY_STYLES: Record<string, string> = {
  text: "bg-slate-100 text-slate-700 ring-slate-200",
  figure: "bg-indigo-50 text-indigo-700 ring-indigo-200",
  table: "bg-emerald-50 text-emerald-700 ring-emerald-200",
};

export function RetrievedChunks({ chunks }: { chunks: RetrievedChunk[] }) {
  if (chunks.length === 0) return null;
  return (
    <section className="space-y-2">
      <h2 className="text-xs font-medium uppercase tracking-wider text-slate-500">
        Retrieved context ({chunks.length})
      </h2>
      <div className="grid gap-2">
        {chunks.map((c) => (
          <div
            key={c.chunk_id}
            className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm"
          >
            <div className="flex items-center gap-2 text-xs">
              <span
                className={`rounded px-1.5 py-0.5 font-medium ring-1 ${
                  MODALITY_STYLES[c.modality] ?? MODALITY_STYLES.text
                }`}
              >
                {c.modality}
              </span>
              <span className="font-mono text-slate-500">{c.chunk_id}</span>
              {c.section && (
                <span className="text-slate-400">· {c.section}</span>
              )}
            </div>
            <p className="mt-2 line-clamp-3 text-sm text-slate-700">
              {c.content_preview}
            </p>
            {c.image_uri && (
              <p className="mt-1 text-xs text-indigo-600">
                📷 {c.image_uri.split("/").pop()}
              </p>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
