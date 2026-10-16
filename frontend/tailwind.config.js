/**
 * Tailwind maps utility names onto the design tokens defined once as CSS variables in
 * app/globals.css, so light/dark themes and senior mode switch by changing variables only.
 * Loaded from globals.css with `@config`.
 * @type {import('tailwindcss').Config}
 */
module.exports = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--color-bg)",
        surface: "var(--color-surface)",
        "surface-2": "var(--color-surface-2)",
        "surface-3": "var(--color-surface-3)",
        border: "var(--color-border)",
        "border-strong": "var(--color-border-strong)",
        fg: "var(--color-text)",
        "fg-muted": "var(--color-text-muted)",
        "fg-subtle": "var(--color-text-subtle)",
        primary: "var(--color-primary)",
        "primary-hover": "var(--color-primary-hover)",
        "primary-fg": "var(--color-primary-fg)",
        "primary-soft": "var(--color-primary-soft)",
        accent: "var(--color-accent)",
        "accent-fg": "var(--color-accent-fg)",
        "accent-soft": "var(--color-accent-soft)",
        success: "var(--color-success)",
        "success-soft": "var(--color-success-soft)",
        warning: "var(--color-warning)",
        "warning-soft": "var(--color-warning-soft)",
        danger: "var(--color-danger)",
        "danger-hover": "var(--color-danger-hover)",
        "danger-fg": "var(--color-danger-fg)",
        "danger-soft": "var(--color-danger-soft)",
        info: "var(--color-info)",
        "info-soft": "var(--color-info-soft)",
        focus: "var(--color-focus)",
        overlay: "var(--color-overlay)",
      },
      borderRadius: {
        sm: "var(--radius-sm)",
        md: "var(--radius-md)",
        lg: "var(--radius-lg)",
        xl: "var(--radius-xl)",
        pill: "9999px",
      },
      boxShadow: {
        sm: "var(--shadow-sm)",
        md: "var(--shadow-md)",
        lg: "var(--shadow-lg)",
      },
      spacing: {
        tap: "var(--tap-min)",
      },
      minHeight: {
        tap: "var(--tap-min)",
      },
      minWidth: {
        tap: "var(--tap-min)",
      },
      fontSize: {
        xs: ["var(--text-xs)", { lineHeight: "1.5" }],
        sm: ["var(--text-sm)", { lineHeight: "1.5" }],
        base: ["var(--text-base)", { lineHeight: "1.6" }],
        lg: ["var(--text-lg)", { lineHeight: "1.5" }],
        xl: ["var(--text-xl)", { lineHeight: "1.4" }],
        "2xl": ["var(--text-2xl)", { lineHeight: "1.3" }],
        "3xl": ["var(--text-3xl)", { lineHeight: "1.2" }],
        "4xl": ["var(--text-4xl)", { lineHeight: "1.1" }],
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
      },
      maxWidth: {
        page: "var(--page-max)",
      },
    },
  },
  plugins: [],
};
