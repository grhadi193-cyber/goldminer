import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "#0B0E14",
        panel: "#111621",
        panel2: "#0E131C",
        line: "#1E2635",
        ink: "#E6EDF7",
        dim: "#8FA0B8",
        cyan: "#22D3EE",
        green: "#34D399",
        amber: "#F59E0B",
        red: "#F87171",
        violet: "#A78BFA",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        fa: ["Vazirmatn", "Tahoma", "ui-sans-serif", "sans-serif"],
      },
      fontSize: { "2xs": ["10px", "14px"] },
    },
  },
  plugins: [],
};
export default config;
