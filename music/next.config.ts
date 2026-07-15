import type { NextConfig } from "next";

// The music app (issue #116) is a deliberate sibling of mcc/, not a route
// inside it -- epic #115's D1. In v1 it is pure frontend over fixture data:
// no proxy routes, no daemon, no bus. When Phase 2's playback daemon lands,
// its traffic gets a proxying route handler like mcc's /daemon/* (which owns
// the daemon-down failure path), not a rewrite.
const nextConfig: NextConfig = {
  reactCompiler: true,
  // Let LAN devices (e.g. a phone) reach the dev server; Next blocks
  // cross-origin dev requests by default. Hostnames only, no protocol/port.
  // Whole subnet so it survives DHCP reshuffles of the dev machine's IP.
  allowedDevOrigins: ["192.168.1.*"],
};

export default nextConfig;
