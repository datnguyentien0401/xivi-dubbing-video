# Container running the Vite dev server (hot reload) — used for local
# docker-compose dev, NOT for a production build. The real frontend deploys to
# Firebase Hosting as static files (`npm run build` -> the dist/ directory),
# not via Cloud Run/Docker (see AGENTS.md section 2 — infra/deploy.sh only
# deploys the 2 backend services).
FROM node:20-alpine
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
