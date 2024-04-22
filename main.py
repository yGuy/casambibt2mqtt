import asyncio
import logging
import os
import json
import signal
from enum import IntEnum
from typing import List, Union

import aiomqtt
from asyncio import CancelledError

from CasambiBt import Casambi, discover, Unit, UnitControlType, UnitState

class OpCode(IntEnum):
    Response = 0
    SetLevel = 1
    SetVertical = 4
    SetWhite = 5
    SetColor = 7
    SetTemperature = 10
    SetState = 48


async def main() -> None:
    logging.getLogger("CasambiBt")
    logger = logging.getLogger()
    mqttLogger = logging.getLogger("MQTT")

    loop = asyncio.get_event_loop()

    password = os.environ.get("CASAMBI_NETWORK_PASSWORD", "")

    logger.info("Getting devices")

    devices = await discover()
    for i, d in enumerate(devices):
        print(f"[{i}]\t{d.address}")

    bleakdevice = devices[0] if len(devices) > 0 else None

    if bleakdevice is None:
        logger.error("No Device returned")
        return

    device = bleakdevice.name

    broker_address = os.environ.get("MQTT_HOST", "mosquitto")
    broker_port = int(os.environ.get("MQTT_PORT", "1883"))

    homeassistant_discovery_topic = os.environ.get("MQTT_HOMEASSISTANT_DISCOVERY_TOPIC", None)

    logger.info(f"Connecting to MQTT broker {broker_address}:{broker_port} ...")

    async with aiomqtt.Client(hostname=broker_address, port=broker_port,
                              logger=mqttLogger) as client:

        logger.info("Connected MQTT broker.")
        set_actions = {}

        mqtt_prefix = "casambi/"

        stop_signal = asyncio.Event()

        async def listen():
            try:
                await client.subscribe(mqtt_prefix + "#")
                async for message in client.messages:
                    topic_value = message.topic.value
                    logger.debug("Received mqtt message for topic " + topic_value)
                    action = set_actions.get(topic_value)
                    if action is not None:
                        param = message.payload.decode()
                        logger.info("Executing action for " + topic_value + " -> " + param)
                        await action(param)
            except aiomqtt.MqttError:
                stop_signal.set()

        asyncio.ensure_future(listen())

        async def publish_async(topic, payload) -> None:
            logger.debug("Publishing " + str(topic) + " -> " + str(payload))
            await client.publish(mqtt_prefix + str(topic), payload=payload, qos=0, retain=True)

        async def casa_set_temperature(
                casa: Casambi, target: Unit, temperature: int
        ) -> None:
            control = target.unitType.get_control(UnitControlType.TEMPERATURE)

            if control is not None:
                if control.min is not None and control.max is not None and control.max > control.min and control.length == 8:

                    c = 2 ** control.length - 1
                    level = int(c * ((temperature - control.min) / (control.max - control.min)))
                    if level < 0 or level > c:
                        raise ValueError("Temperature out of range " + str(temperature) + " -> " + str(level))

                    payload = level.to_bytes(1, byteorder="big", signed=False)
                    await casa._send(target, payload, OpCode.SetTemperature)

        async def update_unit(unit: Unit) -> None:
            name = str(unit.uuid)

            state = {
                'state': "ON" if unit.is_on else "OFF",
            }

            if unit.state is not None:
                if unit.unitType.get_control(UnitControlType.WHITE) is not None:
                    state["white"] = unit.state.white
                if unit.unitType.get_control(UnitControlType.VERTICAL) is not None:
                    state["vertical"] = unit.state.vertical
                if unit.unitType.get_control(UnitControlType.TEMPERATURE) is not None:
                    state["color_temp"] = int(1000000/unit.state.temperature)
                if unit.unitType.get_control(UnitControlType.DIMMER) is not None:
                    state["brightness"] = unit.state.dimmer

            await publish_async(name + "/state", json.dumps(state))
            await publish_async(name + "/availability", "online" if unit.online else "offline")
            if unit.state is not None:
                await publish_async(name + "/white", unit.state.white) if (unit.
                                                                           unitType.get_control(
                    UnitControlType.WHITE) is not None) else None
                await publish_async(name + "/vertical", unit.state.vertical) if (unit.
                                                                                 unitType.get_control(
                    UnitControlType.VERTICAL) is not None) else None
                await publish_async(name + "/dimmer", unit.state.dimmer) if (unit.
                                                                             unitType.get_control(
                    UnitControlType.DIMMER) is not None) else None
                await publish_async(name + "/temperature", unit.state.temperature) if (unit.
                                                                                       unitType.get_control(
                    UnitControlType.TEMPERATURE) is not None) else None

        async def register_unit(unit: Unit) -> None:
            await update_unit(unit)
            name = unit.name

            topic = mqtt_prefix + str(unit.uuid)

            if homeassistant_discovery_topic is not None:
                dimmer_config = {
                    'brightness_scale': 255,
                    'brightness_state_topic': topic + '/dimmer',
                    'brightness_command_topic': topic + '/dimmer/set',
                }

                temperature_config = {
                    'color_temp_state_topic': topic + '/temperature',
                    'color_temp_command_topic': topic + '/temperature/set',
                }

                white_config = {
                    'white_scale': 255,
                    'white_command_topic': topic + '/white/set',
                }

                on_off_config = {
                    'state_topic': topic + "/state",
                    'command_topic': topic + "/state/set",
                    'payload_on': 'ON',
                    'payload_off': 'OFF',
                    'on_command_type': 'brightness'
                }

                config = {
                    'availability': [
                        {
                            'topic': topic + '/availability',
                        },
                        {
                            'topic': mqtt_prefix + 'availability'
                        },
                    ],
                    'availability_mode': 'all',
                    'payload_available': 'online',
                    'payload_not_available': 'offline',
                    'device': {
                        'identifiers': ['casambibt2mqtt_' + str(unit.uuid)],
                        'manufacturer': 'Casambi',
                        'model': 'unknown',
                        'name': unit.name,
                        'sw_version': unit.firmwareVersion
                    },
                    'schema': 'json',
                    'name': None,
                    'supported_color_modes': [],
                    'unique_id': str(unit.uuid) + "_light_casambibt2mqtt",
                }

                if unit.unitType.get_control(UnitControlType.TEMPERATURE) is not None:
                    temperature_config["min_mireds"] = int(1000000 / unit.unitType.get_control(UnitControlType.TEMPERATURE).max)
                    temperature_config["max_mireds"] = int(1000000 / unit.unitType.get_control(UnitControlType.TEMPERATURE).min)
                    config["supported_color_modes"].append("color_temp")

                config["supported_color_modes"].append("white") if unit.unitType.get_control(UnitControlType.WHITE) is not None else None
                config["supported_color_modes"].append("brightness") if unit.unitType.get_control(UnitControlType.DIMMER) is not None else None

                config.update(temperature_config) if unit.unitType.get_control(UnitControlType.TEMPERATURE) is not None else None
                config.update(dimmer_config) if unit.unitType.get_control(UnitControlType.DIMMER) is not None else None
                config.update(white_config) if unit.unitType.get_control(UnitControlType.WHITE) is not None else None

                config.update(on_off_config)

                await client.publish(homeassistant_discovery_topic + "/light/" + str(unit.uuid) + "/light/config", json.dumps(config), 0, True)

            def check_and_parse_dict(value) -> dict | None:
                try:
                    json_data = json.loads(value)
                except ValueError as e:
                    return None
                if not isinstance(json_data, dict):
                    return None
                return json_data

            async def switch_action(value) -> None:
                logger.debug("Switch action " + name + " " + value)
                try:
                    command = check_and_parse_dict(value)
                    if command is not None:
                        if "brightness" in command:
                            await casa.setLevel(unit, command["brightness"])
                        if "white" in command:
                            await casa.setWhite(unit, command["white"])
                        if "color_temp" in command:
                            temp = int(1000000 / command["color_temp"])
                            temp = max(unit.unitType.get_control(UnitControlType.TEMPERATURE).min, min(temp, unit.unitType.get_control(UnitControlType.TEMPERATURE).max))
                            await casa_set_temperature(casa, unit, temp)
                        if "vertical" in command:
                            await casa.setVertical(unit, command["vertical"])
                        if "state" in command and command["state"] == "OFF":
                            await casa.setLevel(unit, 0)
                        if "state" in command and command["state"] == "ON" and not unit.is_on:
                            await casa.turnOn(unit)
                    else:
                        if value == "ON":
                            await casa.turnOn(unit)
                        elif value == "OFF":
                            await casa.setLevel(unit, 0)
                        else:
                            logger.warning("Invalid value passed to state: " + name + " " + value)
                            return
                except CancelledError:
                    logger.exception("Cancelled action " + name)

            action_channel = topic + "/state/set"
            set_actions[action_channel] = switch_action
            logger.debug("Registering action for " + action_channel)

            async def set_white(value) -> None:
                logger.debug("Set white " + name + " " + value)
                try:
                    await casa.setWhite(unit, int(value))
                    await publish_async(name + "/white", unit.state.white)
                except ValueError:
                    logger.warning("Invalid value passed to white: " + name + " " + value)
                except CancelledError:
                    logger.exception("Cancelled action " + name)

            set_actions[topic + "/white/set"] = set_white

            async def set_dimmer(value) -> None:
                logger.debug("Set dimmer " + name + " " + value)
                try:
                    await casa.setLevel(unit, int(value))
                    await publish_async(name + "/dimmer", unit.state.dimmer)
                except ValueError:
                    logger.warning("Invalid value passed to dimmer: " + name + " " + value)
                except CancelledError:
                    logger.exception("Cancelled action " + name)

            set_actions[topic + "/dimmer/set"] = set_dimmer

            async def set_vertical(value) -> None:
                logger.debug("Set vertical " + name + " " + value)
                try:
                    await casa.setVertical(unit, int(value))
                    await publish_async(name + "/vertical", unit.state.vertical)
                except ValueError:
                    logger.warning("Invalid value passed to vertical: " + name + " " + value)
                except CancelledError:
                    logger.exception("Cancelled action " + name)

            set_actions[topic + "/vertical/set"] = set_vertical

            async def set_temperature(value) -> None:
                logger.debug("Set temperature " + name + " " + value)
                try:
                    await casa_set_temperature(casa, unit, int(value))
                    await publish_async(name + "/temperature", unit.state.vertical)
                except ValueError:
                    logger.warning("Invalid value passed to temperature: " + name + " " + value)
                except CancelledError:
                    logger.exception("Cancelled action " + name)

            set_actions[topic + "/temperature/set"] = set_temperature

        logger.info(f"Connecting to Casambi Network {device} ...")

        def unit_changed(unit) -> None:
            logger.debug("Unit changed " + unit.__repr__())
            loop.create_task(update_unit(unit))

        def bluetooth_disconnected() -> None:
            logger.warning("Bluetooth disconnected")
            stop_signal.set()


        async def update_availability(available: bool) -> None:
            await publish_async("availability", 'online' if available else 'offline')

        def stop_program():
            logger.warning("Stopping...")
            stop_signal.set()

        await update_availability(False)

        casa = Casambi()

        try:
            await casa.connect(bleakdevice, password, forceOffline=True)
            casa.registerUnitChangedHandler(unit_changed)
            casa.registerDisconnectCallback(bluetooth_disconnected)
            logger.info("Connected.")

            for u in casa.units:
                await register_unit(u)

            await update_availability(True)

            logger.info("Waiting for events...")

            for signame in ('SIGINT', 'SIGTERM'):
                loop.add_signal_handler(getattr(signal, signame), stop_program)

            await stop_signal.wait()
            await update_availability(False)
            await asyncio.sleep(1)

        finally:
            casa.unregisterUnitChangedHandler(unit_changed)
            await casa.disconnect()
            await update_availability(False)
            await client.__aexit__(None, None, None)
            logger.warning("Exiting.")
            await asyncio.sleep(2)
            loop.stop()


if __name__ == "__main__":
    # Install logging
    stream_logger = logging.StreamHandler()
    stream_logger.setLevel(logging.DEBUG)
    stream_logger.setFormatter(
        logging.Formatter("%(asctime)s> %(levelname)s %(message)s")
    )
    logging.getLogger().addHandler(stream_logger)
    logging.getLogger().setLevel(os.environ.get('VERBOSITY', logging.WARNING))

    asyncio.run(main())
