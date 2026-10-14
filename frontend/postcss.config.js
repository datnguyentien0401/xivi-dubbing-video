// Without this file, Vite doesn't know to run the tailwindcss/autoprefixer
// plugins — index.css's "@tailwind base/components/utilities" gets served
// verbatim (not valid CSS, the browser ignores it) instead of being compiled
// into real CSS. Reproduced this bug by hand (curl /src/index.css showed the
// raw @tailwind directives).
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
