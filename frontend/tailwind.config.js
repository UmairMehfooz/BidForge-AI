/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          bg: '#F4F6FA',
          surface: '#FFFFFF',
          elevated: '#EEF2F7',
          border: '#E4E9F1',
          primary: '#2563EB',
          primaryGlow: '#2563EB1A',
          success: '#059669',
          danger: '#E11D48',
          warning: '#D97706',
          muted: '#64748B',
          body: '#334155',
          heading: '#0F172A',
        }
      },
      fontFamily: {
        sans: ['Inter', 'sans-serif'],
        mono: ['"JetBrains Mono"', '"Fira Code"', 'monospace'],
      },
      boxShadow: {
        'glow-top': '0 -1px 12px var(--tw-shadow-color)',
      }
    },
  },
  plugins: [],
}
