# Public resume demo deployment

This setup serves the static dashboard from Vercel and runs the Python simulation API as a persistent web service. It is intentionally unauthenticated and uses simulated vehicles only.

## Before you deploy

Push this project to a Git repository. The root `.gitignore` excludes `.env`, local vehicle data, databases, and Python virtual environments; keep those files out of the repository.

## Deploy the dashboard on Vercel

1. Import the same repository in Vercel.
2. Set **Root Directory** to `FRONTEND`, choose **Other** as the Framework Preset, leave the Build Command empty, and set the Output Directory to `.`.
3. Deploy once and copy the Vercel production origin, for example `https://your-project.vercel.app`. The dashboard will show the API as offline until you finish the backend setup.

## Deploy the API on Render

1. In Render, create a Blueprint from the repository. Render reads the root `render.yaml` and creates `v2v-fleet-demo-api`.
2. When Render asks for `CORS_ORIGINS`, enter the Vercel production origin (no trailing slash).
3. Wait for the API deploy to finish. Its health URL is `https://<render-service>.onrender.com/api/health`.
4. Set `apiBaseUrl` in `FRONTEND/config.js` to the Render service origin, then redeploy Vercel.

The Blueprint points the API at `BACKEND/data/vehicles.demo.json`. These sample vehicles have no ThingsBoard tokens. Do not add ThingsBoard credentials or live vehicle tokens to the public demo.

## Demo behavior

- Authentication is off as requested. The dashboard can issue commands to the demo simulation.
- Hardware is simulated in this project. Keep this deployment disconnected from real hardware and ThingsBoard accounts.
- Vehicle registry administration remains restricted to the backend host.
- The free Render service can sleep when idle and may restart. The demo may take time to wake and its SQLite history can reset after a restart or redeploy.
