# SnapIT backend

Node 18+ server, no dependencies. Endpoints: `POST /api/grab`, `GET /api/download/<id>`.

## Deploy on Render
- Build command: `npm install`  |  Start command: `npm start`
- Environment: `RAPIDAPI_KEY`, `ALLOWED_ORIGIN` (your frontend origin, no trailing slash)
- Check it: open `/api/grab` in a browser; "Use POST." means it is running.

## Run locally
Copy `.env.example` to `.env`, fill it in, then `npm start`. Set `ALLOWED_ORIGIN` to
wherever you serve the frontend from while testing.
