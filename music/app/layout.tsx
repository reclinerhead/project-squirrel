import type { Metadata } from "next";
import { Fraunces, Sometype_Mono } from "next/font/google";
import "./globals.css";
import { getSeedQueue } from "@/lib/api";
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
  description: "The listening room: the NAS library, played.",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // The seed queue is server-fetched so the bar opens populated -- the app
  // resumes where the listening left off (real play_history the moment any
  // exists) with no client fetch and no loading flash.
  const seed = await getSeedQueue();
  return (
    <html
      lang="en"
      className={`${fraunces.variable} ${sometypeMono.variable} h-full antialiased`}
    >
      <body className="min-h-full">
        <PlayerProvider seed={seed}>
          <Chrome />
          {/* bottom padding reserves the player bar's strip on every route */}
          <main className="mx-auto w-full max-w-6xl px-4 pb-32 pt-6">{children}</main>
          <PlayerBar />
        </PlayerProvider>
      </body>
    </html>
  );
}
