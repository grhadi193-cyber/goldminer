import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mishkah · Mission Control",
  description:
    "Research + paper-trading monitor for Iranian physical gold. Synthetic data unless the banner says otherwise. Paper only.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-base font-sans text-ink">{children}</body>
    </html>
  );
}
