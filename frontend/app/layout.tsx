import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Calendar Sync",
  description: "All your Microsoft 365 calendars, across every organization, in one calendar.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen font-sans antialiased">{children}</body>
    </html>
  );
}
