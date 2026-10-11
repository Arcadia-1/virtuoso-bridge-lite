import { build } from "esbuild";
import { cp, mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";

// Only public website files are packaged; repository documentation stays on GitHub.
const destination = new URL("./dist/", import.meta.url);
await mkdir(destination, { recursive: true });
for (const name of [
  "index.html",
  "site.css",
  "site.js",
  "favicon.svg",
  "favicon.png",
  "apple-touch-icon.png",
  "robots.txt",
  "sitemap.xml",
  "guide",
  "assets",
  "analytics",
  "privacy",
]) {
  await cp(
    new URL(`../docs/${name}`, import.meta.url),
    new URL(name, destination),
    { recursive: true },
  );
}
await build({
  entryPoints: [
    fileURLToPath(new URL("./analytics/main.tsx", import.meta.url)),
  ],
  outdir: fileURLToPath(new URL("./analytics/", destination)),
  entryNames: "dashboard",
  bundle: true,
  minify: true,
  format: "esm",
  jsx: "automatic",
  target: ["es2022"],
  define: { "process.env.NODE_ENV": '"production"' },
});
console.log("Built static website and Analog Canvas analytics dashboard.");
await build({
  entryPoints: [
    fileURLToPath(new URL("./analytics/track.ts", import.meta.url)),
  ],
  outfile: fileURLToPath(new URL("./analytics/track.js", destination)),
  bundle: true,
  minify: true,
  format: "esm",
  target: ["es2022"],
});
