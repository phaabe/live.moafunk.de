# NodeMediaServer pinned to the prod version (2.4.9), with ffmpeg for HLS.
FROM node:16-alpine
RUN apk add --no-cache ffmpeg
WORKDIR /app
RUN npm init -y >/dev/null 2>&1 && npm install node-media-server@2.4.9
COPY app.js .
EXPOSE 1935 8000
CMD ["node", "app.js"]
