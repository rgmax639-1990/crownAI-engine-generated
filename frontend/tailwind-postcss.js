// Minimal PostCSS plugin that runs Tailwind CSS v4 through the `tailwindcss`
// package's own compile() API. It stands in for `@tailwindcss/postcss` so the
// stylesheet builds with only the dependencies already in package-lock.json.
//
// The compiled CSS is handed back to PostCSS as a string (root.append parses
// it with the *host's* PostCSS), so this file never mixes nodes from a second
// PostCSS copy into the bundler's tree.
const fs = require("fs");
const path = require("path");
const { compile } = require("tailwindcss");

const ROOT = __dirname;
const TAILWIND_DIR = path.dirname(require.resolve("tailwindcss/package.json", { paths: [ROOT] }));
const SCAN_DIRS = ["app", "components", "lib"].map((d) => path.join(ROOT, d));
const SCAN_EXT = /\.(jsx?|tsx?|mdx|html)$/;
const NEEDS_TAILWIND = /@(import\s+["']tailwindcss|tailwind|apply|config|theme|plugin|utility|variant|source)\b/;
// Class candidates are split on whitespace, quotes and JSX/JS punctuation --
// but not on commas, which occur inside arbitrary values such as
// shadow-[0_-4px_16px_rgba(0,0,0,0.08)].
const CANDIDATE_SPLIT = /[\s"'`<>{};=\\]+/;

function listFiles(dir, out = []) {
  let entries = [];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) listFiles(full, out);
    else if (SCAN_EXT.test(entry.name)) out.push(full);
  }
  return out;
}

function scanCandidates(files) {
  const candidates = new Set();
  for (const file of files) {
    const text = fs.readFileSync(file, "utf8");
    for (const token of text.split(CANDIDATE_SPLIT)) {
      if (token) candidates.add(token);
    }
  }
  return [...candidates];
}

function resolveStylesheet(id, base) {
  if (id === "tailwindcss") return path.join(TAILWIND_DIR, "index.css");
  if (id.startsWith("tailwindcss/")) {
    const rest = id.slice("tailwindcss/".length);
    return path.join(TAILWIND_DIR, rest.endsWith(".css") ? rest : `${rest}.css`);
  }
  if (id.startsWith(".") || path.isAbsolute(id)) return path.resolve(base, id);
  return require.resolve(id, { paths: [base] });
}

function resolveModule(id, base) {
  if (id.startsWith(".") || path.isAbsolute(id)) return path.resolve(base, id);
  return require.resolve(id, { paths: [base] });
}

module.exports = () => ({
  postcssPlugin: "crownai-tailwindcss",
  async Once(root, { result }) {
    const source = root.toString();
    if (!NEEDS_TAILWIND.test(source)) return;

    const from = result.opts.from || path.join(ROOT, "app", "globals.css");
    const base = path.dirname(from);
    const deps = new Set();

    let css;
    const files = SCAN_DIRS.flatMap((dir) => listFiles(dir));
    try {
      const compiler = await compile(source, {
        base,
        from,
        async loadStylesheet(id, sheetBase) {
          const file = resolveStylesheet(id, sheetBase);
          deps.add(file);
          return { path: file, base: path.dirname(file), content: fs.readFileSync(file, "utf8") };
        },
        async loadModule(id, moduleBase) {
          const file = resolveModule(id, moduleBase);
          deps.add(file);
          delete require.cache[file];
          const mod = require(file);
          return { path: file, base: path.dirname(file), module: mod && mod.__esModule ? mod.default : mod };
        },
      });
      css = compiler.build(scanCandidates(files));
    } catch (err) {
      throw new Error(`Tailwind CSS failed to compile ${path.relative(ROOT, from)}: ${err && err.message ? err.message : err}`);
    }

    for (const file of [...deps, ...files]) {
      result.messages.push({ type: "dependency", plugin: "crownai-tailwindcss", file, parent: from });
    }
    for (const dir of SCAN_DIRS) {
      result.messages.push({ type: "dir-dependency", plugin: "crownai-tailwindcss", dir, glob: "**/*", parent: from });
    }

    root.removeAll();
    root.append(css);
  },
});
module.exports.postcss = true;
