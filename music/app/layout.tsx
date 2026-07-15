import type { Metadata } from "next";
import { Fraunces, Sometype_Mono } from "next/font/google";
import "./globals.css";
import { PlayerProvider } from "@/components/PlayerProvider";
import { Chrome } from "@/components/Chrome";
import { PlayerBar } from "@/components/PlayerBar";

// Same type pairing as the MCC -- Fraunces for display, Sometype Mono for
// telemetry -- because the two apps are rooms in one station. Self-hosted at
// build time by next/font.
const fraunces = Fraunces({
  variable: "--font-display",
  subsets: ["latin"],
  axes: ["SOFT", "WONK", "opsz"],
});

const sometypeMono = Sometype_Mono({
  variable: "--font-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Music",
  description: "The listening room: the NAS library, played. (v1: fixture data.)",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${fraunces.variable} ${sometypeMono.variable} h-full antialiased`}
    >
      <body className="min-h-full">
        <PlayerProvider>
          <Chrome />
          {/* bottom padding reserves the player bar's strip on every route */}
          <main className="mx-auto w-full max-w-6xl px-4 pb-32 pt-6">{children}</main>
          <PlayerBar />
        </PlayerProvider>
      </body>
    </html>
  );
}
