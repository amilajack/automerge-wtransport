import { defineConfig } from "vite";
export default defineConfig({
  root: "web",
  build: { outDir: "../dist", emptyOutDir: true },
  optimizeDeps: { exclude: ["@automerge/automerge"] },
});
