/**
 * Tailwind config for Kai's web UI.
 *
 * Lifted verbatim from the former inline `tailwind.config` in app.html (the
 * Play-CDN setup).  Building CSS ahead of time means the app no longer needs
 * the runtime CDN compiler, so the CSP can stay tight (no 'unsafe-eval') and
 * the UI styles work fully offline.
 *
 * `content` must include app.js — it generates markup with Tailwind classes at
 * runtime, and those classes would be purged if the file weren't scanned.
 *
 * Build:  npm run build:css   (or `npm run watch:css` while editing the UI)
 */
module.exports = {
  darkMode: "class",
  content: [
    "./kai/static/app.html",
    "./kai/static/app.js",
  ],
  theme: {
    extend: {
      colors: {
        "surface-container-lowest": "#0d0d17",
        "surface-container-low": "#1b1b25",
        "inverse-surface": "#e4e1f0",
        "on-background": "#e4e1f0",
        "on-primary-container": "#130094",
        "background": "#12121d",
        "error": "#ffb4ab",
        "surface-tint": "#c1c1ff",
        "surface": "#12121d",
        "on-tertiary": "#482900",
        "on-error-container": "#ffdad6",
        "on-secondary-fixed-variant": "#414273",
        "surface-bright": "#393844",
        "on-secondary-fixed": "#151545",
        "on-tertiary-fixed-variant": "#673d00",
        "secondary-fixed-dim": "#c2c1fc",
        "primary": "#c1c1ff",
        "surface-container-high": "#292934",
        "tertiary-fixed-dim": "#ffb867",
        "tertiary-fixed": "#ffddba",
        "outline": "#918fa0",
        "surface-variant": "#34343f",
        "error-container": "#93000a",
        "surface-container-highest": "#34343f",
        "secondary-container": "#444476",
        "on-surface-variant": "#c7c4d6",
        "on-primary-fixed-variant": "#3430b7",
        "on-tertiary-fixed": "#2b1700",
        "secondary-fixed": "#e2dfff",
        "on-primary": "#1908a2",
        "on-tertiary-container": "#3f2300",
        "inverse-on-surface": "#302f3b",
        "primary-fixed": "#e2dfff",
        "secondary": "#c2c1fc",
        "inverse-primary": "#4d4ccf",
        "primary-container": "#8283ff",
        "surface-dim": "#12121d",
        "on-secondary": "#2b2b5b",
        "on-error": "#690005",
        "on-surface": "#e4e1f0",
        "outline-variant": "#464554",
        "tertiary": "#ffb867",
        "surface-container": "#1f1f29",
        "primary-fixed-dim": "#c1c1ff",
        "on-primary-fixed": "#0b006b",
        "on-secondary-container": "#b4b3ed",
        "tertiary-container": "#cc7f0c",
      },
      fontFamily: {
        "headline": ["Space Grotesk"],
        "body": ["Inter"],
        "label": ["Space Grotesk"],
      },
      borderRadius: { "DEFAULT": "0.125rem", "lg": "0.25rem", "xl": "0.5rem", "full": "0.75rem" },
    },
  },
  plugins: [
    require("@tailwindcss/forms"),
    require("@tailwindcss/container-queries"),
  ],
};
