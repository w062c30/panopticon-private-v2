/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        panBg: "#060b13",
        panPanel: "#111827",
        panPanel2: "#1f2937",
        panGrid: "#334155",
        panNeon: "#22d3ee",
        panGood: "#22c55e",
        panWarn: "#f97316",
        panDanger: "#ef4444"
      }
    }
  },
  plugins: [],
};

