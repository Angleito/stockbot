import { networkInterfaces } from "node:os";
import type { NextConfig } from "next";

const allowedDevOrigins = Object.values(networkInterfaces())
  .flat()
  .filter((n) => n?.family === "IPv4" && !n.internal)
  .map((n) => n!.address);

const nextConfig: NextConfig = {
  allowedDevOrigins,
  turbopack: {
    root: __dirname,
  },
};

export default nextConfig;
