FROM python:3.12-alpine3.19

RUN \
    apk add --no-cache \
        bluez \
        bluez-deprecated \
        bluez-libs \
        libffi-dev \
        gcc \
        musl-dev

RUN pip install bleak==0.21.1 bleak-retry-connector==3.5.0 dbus-fast==2.21.1
RUN pip install aiomqtt==2.0.1
RUN pip install casambi-bt==0.2.1

ENV MQTT_HOST=mosquitto
ENV MQTT_PORT=1883
ENV MQTT_HOMEASSISTANT_DISCOVERY_TOPIC=homeassistant
ENV VERBOSITY=WARNING

WORKDIR /app
COPY main.py /app

CMD python main.py
