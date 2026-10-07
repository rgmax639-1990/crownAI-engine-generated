/** @type {import('tailwindcss').Config} */
// Every colour here points at a CSS variable defined once in
// app/globals.css, so a single class (e.g. `text-muted`) is correct in both
// the light and the dark theme. The gold values come from the Crownwright logo.
module.exports = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./lib/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        canvas: "var(--canvas)",
        surface: "var(--surface)",
        subtle: "var(--subtle)",
        "subtle-2": "var(--subtle-2)",
        line: "var(--line)",
        "line-strong": "var(--line-strong)",
        ink: "var(--ink)",
        muted: "var(--muted)",
        faint: "var(--faint)",
        brand: "var(--brand)",
        "brand-strong": "var(--brand-strong)",
        "brand-soft": "var(--brand-soft)",
        "brand-soft-ink": "var(--brand-soft-ink)",
        "on-gold": "var(--on-gold)",
        gold: {
          1: "var(--gold-1)",
          2: "var(--gold-2)",
          3: "var(--gold-3)",
        },
        danger: "var(--danger)",
        success: "var(--success)",
        warning: "var(--warning)",
      },
      // Text utilities use the same restrained type scale as the component
      // classes in globals.css, so `text-sm` and `.body-muted` never disagree.
      fontSize: {
        xs: ["var(--text-xs)", { lineHeight: "1.5" }],
        sm: ["var(--text-sm)", { lineHeight: "1.6" }],
        base: ["var(--text-base)", { lineHeight: "1.65" }],
        lg: ["var(--text-lg)", { lineHeight: "1.55" }],
        xl: ["var(--text-xl)", { lineHeight: "1.4" }],
        "2xl": ["var(--text-2xl)", { lineHeight: "1.3" }],
        "3xl": ["var(--text-3xl)", { lineHeight: "1.2" }],
        "4xl": ["var(--text-4xl)", { lineHeight: "1.15" }],
      },
      borderRadius: {
        sm: "var(--radius-sm)",
        md: "var(--radius-md)",
        lg: "var(--radius-lg)",
        xl: "var(--radius-xl)",
      },
      boxShadow: {
        sm: "var(--shadow-sm)",
        md: "var(--shadow-md)",
        lg: "var(--shadow-lg)",
        gold: "var(--shadow-gold)",
      },
      backgroundImage: {
        "gold-gradient": "var(--gold-gradient)",
      },
    },
  },
  plugins: [],
};
