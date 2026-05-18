"use client";

import { useState } from "react";
import { useChat } from "@/lib/use-chat";
import { RetrievedChunks } from "@/components/retrieved-chunks";
import { StreamedAnswer } from "@/components/streamed-answer";
import { VerificationPanel } from "@/components/verification-panel";

// Configure where the API lives. NEXT_PUBLIC_* is exposed to the browser.
// Default to localhost for dev; override with NEXT_PUBLIC_API_URL in production.
const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const EXAMPLE_QUERIES = [
  "What is bottlenecked SFT?",
  "How does policy iteration work in this paper?",
  "What is the maximum number of abstract tokens?",
];

export default function HomePage() {
  const [query, setQuery] = useState("");
  const { state, send } = useChat(API_URL);

  const onSubmit = () => {
    if (!query.trim() || state.status === "retrieving" || state.status === "streaming") {
      return;
    }
    send(query.trim());
  };

  const busy =
    state.status === "retrieving" ||
    state.status === "streaming" ||
    state.status === "verifying";

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <Header />

      <div className="mt-8 space-y-3">
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onSubmit();
          }}
          placeholder="Ask a question about ML papers..."
          rows={2}
          className="w-full resize-none rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
        <div className="flex items-center justify-between">
          <div className="flex flex-wrap gap-1.5">
            {EXAMPLE_QUERIES.map((q) => (
              <button
                key={q}
                onClick={() => setQuery(q)}
                disabled={busy}
                className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-50"
              >
                {q}
              </button>
            ))}
          </div>
          <button
            onClick={onSubmit}
            disabled={busy || !query.trim()}
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? <Spinner /> : "Ask"}
          </button>
        </div>
      </div>

      {state.error && (
        <div className="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
          {state.error}
        </div>
      )}

      <div className="mt-8 space-y-6">
        <RetrievedChunks chunks={state.chunks} />
        <StreamedAnswer
          text={state.answer}
          verification={state.verification?.sentences ?? null}
        />
        <VerificationPanel
          verification={state.verification}
          latencyMs={state.latencyMs}
        />
      </div>
    </main>
  );
}

function Header() {
  return (
    <header className="border-b border-slate-200 pb-6">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
        ArXivLens
      </h1>
      <p className="mt-1 text-sm text-slate-600">
        Multi-modal RAG over ArXiv ML literature. Hybrid retrieval (dense +
        BM25 fused with RRF), NLI-based per-sentence citation verification.
      </p>
      <div className="mt-3 flex flex-wrap gap-2 text-xs text-slate-500">
        <Pill>GCP · Cloud Run · Cloud SQL</Pill>
        <Pill>pgvector · HNSW</Pill>
        <Pill>Gemini 2.5 Flash</Pill>
        <Pill>DeBERTa-MNLI</Pill>
      </div>
    </header>
  );
}

function Pill({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-full bg-slate-100 px-2 py-0.5 ring-1 ring-slate-200">
      {children}
    </span>
  );
}

function Spinner() {
  return (
    <span className="inline-flex items-center gap-2">
      <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-white border-t-transparent" />
      Thinking…
    </span>
  );
}
