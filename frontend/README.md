# Majeure frontend

React 19 + Vite + TypeScript UI for the FastAPI API in `../backend`.

```bash
npm ci          # postinstall copies MapLibre's shared worker module into place (the map is blank without it)
npm run dev     # http://localhost:5173, talks to VITE_API_BASE (default http://localhost:8700)
npm run build   # tsc -b && vite build → dist/
```

**Vercel:** Root Directory `frontend`, Framework Vite, Build `npm run build`, Output `dist`,
env `VITE_API_BASE=https://<render-service>.onrender.com` (no trailing slash). The variable is
read at build time, so redeploy after changing it.
