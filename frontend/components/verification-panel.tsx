import type { VerificationPayload } from "@/lib/types";

export function VerificationPanel({
  verification,
  latencyMs,
}: {
  verification: VerificationPayload | null;
  latencyMs: number | null;
}) {
  if (!verification) return null;

  const pct = (verification.faithfulness * 100).toFixed(0);
  const supported = verification.sentences.filter((s) => s.supported).length;
  const cited = verification.sentences.filter(
    (s) => s.citation_chunk_ids.length > 0
  ).length;

  let bandColor = "bg-rose-500";
  if (verification.faithfulness >= 0.7) bandColor = "bg-emerald-500";
  else if (verification.faithfulness >= 0.4) bandColor = "bg-amber-500";

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-xs font-medium uppercase tracking-wider text-slate-500">
          Faithfulness
        </h2>
        {latencyMs !== null && (
          <span className="font-mono text-xs text-slate-400">
            {(latencyMs / 1000).toFixed(2)}s
          </span>
        )}
      </div>

      <div className="mt-2 flex items-baseline gap-2">
        <span className="text-3xl font-semibold tabular-nums text-slate-900">
          {pct}%
        </span>
        <span className="text-sm text-slate-500">
          {supported} of {cited} cited sentences supported
        </span>
      </div>

      <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full ${bandColor} transition-all`}
          style={{ width: `${pct}%` }}
        />
      </div>

      <p className="mt-2 text-xs text-slate-500">
        Per-sentence NLI verification via DeBERTa-MNLI cross-encoder.
      </p>
    </section>
  );
}
