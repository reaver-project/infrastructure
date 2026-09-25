FROM scratch
COPY reservation.txt /reaveros-bootstrap-reservation.txt
LABEL org.opencontainers.image.source="https://github.com/reaver-project/infrastructure"
LABEL org.opencontainers.image.description="Reserved package name; not a ReaverOS build environment"
