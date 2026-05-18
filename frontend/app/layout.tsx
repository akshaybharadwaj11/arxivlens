import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ArXivLens — Multi-Modal RAG",
  description:
    "Multi-modal RAG over ArXiv ML literature with NLI-verified citations",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 font-sans text-slate-900 antialiased">
        {children}
      </body>
    </html>
  );
}
