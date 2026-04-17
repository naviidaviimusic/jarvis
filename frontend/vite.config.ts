import { defineConfig } from "vite";
import fs from "fs";
import path from "path";

export default defineConfig({
  server: {
    port: 5173,
    https: {
      key: fs.readFileSync(path.resolve(__dirname, "../key.pem")),
      cert: fs.readFileSync(path.resolve(__dirname, "../cert.pem")),
    },
    proxy: {
      "/ws": {
        target: "https://localhost:8340",
        ws: true,
        secure: false,
      },
      "/api": {
        target: "https://localhost:8340",
        secure: false,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
