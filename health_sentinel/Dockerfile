ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

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
