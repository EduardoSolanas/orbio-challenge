# Build from the repository root: docker build -f web/Dockerfile .
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY web/index.html /usr/share/nginx/html/index.html
COPY web/css /usr/share/nginx/html/css
COPY web/js /usr/share/nginx/html/js
EXPOSE 8080
