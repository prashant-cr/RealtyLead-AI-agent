import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RealtyLead — Pipeline",
  description: "Lead qualification and follow-up, handled by an AI assistant.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
