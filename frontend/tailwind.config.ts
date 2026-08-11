import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        hot: { bg: "#fef2f2", fg: "#b91c1c", ring: "#fecaca" },
        warm: { bg: "#fffbeb", fg: "#b45309", ring: "#fde68a" },
        cold: { bg: "#f8fafc", fg: "#475569", ring: "#e2e8f0" },
      },
    },
  },
  plugins: [],
};
export default config;
