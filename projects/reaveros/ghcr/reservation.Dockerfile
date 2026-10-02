FROM scratch
COPY reservation.txt /reaveros-bootstrap-reservation.txt
LABEL org.opencontainers.image.source="https://github.com/reaver-project/reaveros"
LABEL org.opencontainers.image.description="Reserved package name; not a ReaverOS build environment"
