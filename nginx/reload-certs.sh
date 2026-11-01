#!/bin/sh
# Recarga Nginx cada 6 horas para que use el certificado renovado. Recargar no corta las
# conexiones abiertas (tampoco los streaming ni el WebSocket del panel).
# Solo en producción: en desarrollo no hay certificado
[ -f /etc/nginx/certs/fullchain.pem ] || exit 0
(while sleep 21600; do nginx -s reload; done) &
