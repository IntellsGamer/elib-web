/** Tailwind config — mirrors the former inline Play-CDN config in base.html.
 *  Build with:  npx tailwindcss -i ./static/src.css -o ./static/tailwind.min.css --minify
 *  (or `npm run build:css`). Re-run after changing templates or static/app.js. */
module.exports = {
  content: ["./templates/**/*.html", "./static/app.js"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        display: ["Fraunces", "Georgia", "serif"],
      },
      colors: {
        night: "#060913",
        gold: { 300: "#fcd34d", 400: "#fbbf24", 500: "#f59e0b" },
      },
      boxShadow: {
        glow: "0 0 40px -8px rgba(245,158,11,.5)",
        card: "0 24px 60px -24px rgba(0,0,0,.8)",
      },
    },
  },
  plugins: [],
};
