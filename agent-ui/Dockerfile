FROM nginx:1.27-alpine

COPY agent-ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY agent-ui/index.html \
     agent-ui/monitor.html \
     agent-ui/app.js \
     agent-ui/api.js \
     agent-ui/monitor.js \
     agent-ui/styles.css \
     /usr/share/nginx/html/ui/

EXPOSE 8000
