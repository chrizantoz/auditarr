FROM node:20-alpine
WORKDIR /bot
RUN npm install --silent jsdom@24.1.3 && apk add --no-cache curl
COPY ui_bot.js run.sh ./
RUN chmod +x run.sh
ENTRYPOINT ["/bot/run.sh"]
