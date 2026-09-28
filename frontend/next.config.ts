import type { NextConfig } from "next";

const config: NextConfig = {
  // Emits a self-contained server bundle so the Docker image does not need
  // node_modules copied into it.
  output: "standalone",
  reactStrictMode: true,
};

export default config;
