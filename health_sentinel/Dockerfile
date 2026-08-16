ARG BUILD_FROM
FROM ${BUILD_FROM}

# Supplied by the Supervisor and by the HA builder action. Used to version the
# dashboard's asset URLs so a rebuilt add-on can never be shadowed by a stale
# copy of the JavaScript in someone's browser.
ARG BUILD_VERSION="dev"

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SENTINEL_VERSION=${BUILD_VERSION}

# python3 comes from Alpine main, aiohttp from community as a prebuilt wheel.
# Installing aiohttp via pip would compile C extensions on the user's device,
# which turns a local-build install into a multi-minute ordeal. Don't.
RUN apk add --no-cache \
        python3 \
        py3-aiohttp

COPY rootfs /
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
