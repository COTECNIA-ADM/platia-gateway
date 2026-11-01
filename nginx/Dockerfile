# Nginx con el panel y el portal (G13b, G14) ya compilados. Se construye desde la raíz del
# repositorio: docker compose build nginx
FROM node:22-alpine AS panel
WORKDIR /panel
COPY panel/package.json panel/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY panel/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx/snippets /etc/nginx/snippets
COPY nginx/nginx.conf /etc/nginx/conf.d/default.conf
# Producción: la imagen oficial lo convierte en conf.d/default.conf al arrancar (envsubst)
COPY nginx/prod.conf.template /etc/nginx/prod/default.conf.template
COPY --chmod=755 nginx/reload-certs.sh /docker-entrypoint.d/99-reload-certs.sh
COPY --from=panel /panel/dist /usr/share/nginx/html/panel
