ARG BUILD_FROM=python:3.12-alpine
FROM $BUILD_FROM

RUN apk add --no-cache bash jpeg-dev zlib-dev \
    && apk add --no-cache --virtual .build-deps gcc musl-dev \
    && pip install --no-cache-dir --root-user-action=ignore \
        flask==3.0.* \
        pillow==10.* \
        requests==2.* \
    && apk del .build-deps

COPY run.sh /run.sh
COPY app /app
RUN chmod a+x /run.sh

WORKDIR /app
CMD [ "/run.sh" ]
