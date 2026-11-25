FROM alpine:3.22
RUN apk add --no-cache git git-daemon busybox-extras \
    && mkdir -p /srv/www/cgi-bin \
    && ln -s "$(git --exec-path)/git-http-backend" /srv/www/cgi-bin/git \
    && test -x /srv/www/cgi-bin/git
ENV HOME=/tmp
WORKDIR /srv/www
COPY publish-chart /usr/local/bin/publish-chart
USER 65532:65532
CMD ["httpd", "-f", "-p", "8080", "-h", "/srv/www"]
