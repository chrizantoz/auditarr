FROM node:20-alpine
LABEL org.opencontainers.image.source="https://github.com/chrizantoz/auditarr" org.opencontainers.image.description="Auditarr UI test bot" org.opencontainers.image.licenses="GPL-3.0"
WORKDIR /bot
RUN npm install --silent jsdom@24.1.3 && apk add --no-cache curl
COPY ui_bot.js run.sh ./
RUN chmod +x run.sh
ENTRYPOINT ["/bot/run.sh"]
