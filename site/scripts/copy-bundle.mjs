// Copy the shared component bundle (repo-root static/t411.js + t411.css, also served by the
// Command Center) into public/vendor/ so the site ships the exact same components.
// Runs automatically before `npm run build` / `npm run dev` (see package.json).
import { copyFileSync, mkdirSync, existsSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const site = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = join(site, "..", "static");
const dest = join(site, "public", "vendor");
mkdirSync(dest, { recursive: true });
for (const f of ["t411.js", "t411.css"]) {
  const from = join(src, f);
  if (!existsSync(from)) {
    console.error(`[copy-bundle] missing ${from} (the site builds from the full repo checkout)`);
    process.exit(1);
  }
  copyFileSync(from, join(dest, f));
}

// The brand faces, inlined into a data card's PNG export at export time. Without them an exported
// card is still correct, just set in Helvetica - so a missing folder is a warning, not a failure.
const fontSrc = join(src, "fonts");
const fontDest = join(dest, "fonts");
if (existsSync(fontSrc)) {
  mkdirSync(fontDest, { recursive: true });
  const faces = readdirSync(fontSrc).filter((f) => f.endsWith(".woff2"));
  for (const f of faces) copyFileSync(join(fontSrc, f), join(fontDest, f));
  console.log(`[copy-bundle] static/t411.js + t411.css + ${faces.length} fonts -> public/vendor/`);
} else {
  console.warn("[copy-bundle] no static/fonts/ — data-card PNG exports will use fallback faces");
  console.log("[copy-bundle] static/t411.js + t411.css -> public/vendor/");
}
