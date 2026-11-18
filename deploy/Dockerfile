# BlinkTalk always-on box: hub (Photon iMessage + AI agent) + caregiver dashboard and patient screen,
# talking to SpacetimeDB Maincloud. Runs on any container host.
# Build from the repo root:
#   docker build -f deploy/Dockerfile --build-arg SPACETIME_DB=blinktalk -t blinktalk .
#   docker run -p 8080:8080 --env-file .env -e BOARD_PORT= blinktalk
# Runtime secrets (environment variables on the host, never baked into the image):
#   AI_BASE_URL, AI_API_KEY, AI_MODEL, PHOTON_API_KEY, PHOTON_PROJECT_ID
FROM node:22-slim
WORKDIR /app
COPY . .
RUN npm install --no-audit --no-fund
ARG SPACETIME_URI=wss://maincloud.spacetimedb.com
ARG SPACETIME_DB=blinktalk
RUN VITE_SPACETIME_URI=$SPACETIME_URI VITE_SPACETIME_DB=$SPACETIME_DB npx -w dashboard vite build
ENV SPACETIME_URI=$SPACETIME_URI SPACETIME_DB=$SPACETIME_DB PORT=8080 NODE_ENV=production
EXPOSE 8080
CMD ["node", "deploy/start.mjs"]
