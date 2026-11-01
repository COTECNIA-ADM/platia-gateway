# Nginx con el panel de administración (G13b) ya compilado. Se construye desde la raíz del
# repositorio: docker compose build nginx
FROM node:22-alpine AS panel
WORKDIR /panel
COPY panel/package.json panel/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY panel/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=panel /panel/dist /usr/share/nginx/html/panel
