import type { VerifiedSentence } from "@/lib/types";

interface Props {
  text: string;
  verification: VerifiedSentence[] | null;
}

const CITATION_RE = /\[([^\]]+)\]/g;

export function StreamedAnswer({ text, verification }: Props) {
  if (!text) return null;

  // If we have verification data, render sentence-by-sentence with status badges
  if (verification && verification.length > 0) {
    return (
      <section className="space-y-2">
        <h2 className="text-xs font-medium uppercase tracking-wider text-slate-500">
          Answer
        </h2>
        <div className="space-y-2 rounded-lg border border-slate-200 bg-white p-4 leading-relaxed">
          {verification.map((s, i) => (
            <VerifiedSentenceLine key={i} v={s} />
          ))}
        </div>
      </section>
    );
  }

  // While streaming (no verification yet), just render text + highlighted citations
  return (
    <section className="space-y-2">
      <h2 className="text-xs font-medium uppercase tracking-wider text-slate-500">
        Answer
      </h2>
      <div className="rounded-lg border border-slate-200 bg-white p-4 leading-relaxed text-slate-800">
        <CitationHighlighted text={text} />
        <span className="ml-1 inline-block h-4 w-2 animate-pulse bg-slate-400 align-middle" />
      </div>
    </section>
  );
}

function VerifiedSentenceLine({ v }: { v: VerifiedSentence }) {
  let badgeBg = "bg-slate-100 text-slate-600";
  let icon = "·";
  if (v.citation_chunk_ids.length === 0) {
    badgeBg = "bg-slate-100 text-slate-500";
    icon = "—";
  } else if (v.supported) {
    badgeBg = "bg-emerald-50 text-emerald-700";
    icon = "✓";
  } else if (v.entailment_score > 0.2) {
    badgeBg = "bg-amber-50 text-amber-700";
    icon = "~";
  } else {
    badgeBg = "bg-rose-50 text-rose-700";
    icon = "✗";
  }

  return (
    <div className="flex items-start gap-2">
      <span
        className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-xs font-medium ${badgeBg}`}
        title={`entailment ${v.entailment_score.toFixed(3)}`}
      >
        {icon}
      </span>
      <p className="text-slate-800">
        <CitationHighlighted text={v.sentence} />
        <span className="ml-2 font-mono text-xs text-slate-400">
          {v.entailment_score.toFixed(2)}
        </span>
      </p>
    </div>
  );
}

function CitationHighlighted({ text }: { text: string }) {
  const parts: React.ReactNode[] = [];
  let lastIdx = 0;
  let m: RegExpExecArray | null;
  CITATION_RE.lastIndex = 0;
  while ((m = CITATION_RE.exec(text)) !== null) {
    if (m.index > lastIdx) {
      parts.push(text.slice(lastIdx, m.index));
    }
    parts.push(
      <span
        key={m.index}
        className="rounded bg-indigo-50 px-1 py-0.5 font-mono text-xs text-indigo-700 ring-1 ring-indigo-200"
      >
        {m[1]}
      </span>
    );
    lastIdx = m.index + m[0].length;
  }
  if (lastIdx < text.length) parts.push(text.slice(lastIdx));
  return <>{parts}</>;
}
