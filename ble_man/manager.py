import asyncio
import re
import struct
import subprocess
import time
from bleak import BleakClient
import logging

logger = logging.getLogger(__name__)

def battery_mv_to_percent(mv: int) -> int:
    if mv >= 3000:
        return 100
    elif mv >= 2950:
        return 95
    elif mv >= 2900:
        return 90
    elif mv >= 2850:
        return 80
    elif mv >= 2800:
        return 70
    elif mv >= 2750:
        return 60
    elif mv >= 2700:
        return 50
    elif mv >= 2650:
        return 40
    elif mv >= 2600:
        return 30
    elif mv >= 2550:
        return 20
    elif mv >= 2500:
        return 10
    elif mv >= 2400:
        return 5
    else:
        return 0
        
class SensorManager:
    CHAR_TEMP = "EF090080-11D6-42BA-93B8-9DD7EC090AA9"
    CHAR_HUMID = "EF090081-11D6-42BA-93B8-9DD7EC090AA9"
    CHAR_BATT = "EF090007-11D6-42BA-93B8-9DD7EC090AA9"

    def __init__(self, sensors, max_retries=3, retry_delay=2):
        self.sensors = sensors
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.clients = []
        self.last_reported_battery = {}

    def disconnect_all_bluetooth_to_init(self):
        try:
            self.enable_bluetooth()
            result = subprocess.run(["hcitool", "con"], capture_output=True, text=True)
            output = result.stdout.strip()

            if "Connections:" not in output:
                logger.info("No se pudo leer conexiones Bluetooth.")
                return

            # extraer MACs con regex
            macs = re.findall(r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})", output, re.I)

            if not macs:
                logger.info("no hay dispositivos Bluetooth conectados.")
                return

            logger.info(f"dispositivos conectados encontrados: {macs}")

            for mac in macs:
                logger.info(f"desconectando {mac} ...")
                subprocess.run(
                    ["bluetoothctl"], input=f"disconnect {mac}\n", text=True
                )
                time.sleep(2)

            logger.info("todos los dispositivos fueron desconectados.")

        except Exception as e:
            logger.info(f"error al desconectar dispositivos: {e}")

    def enable_bluetooth(self):
        subprocess.run(["bluetoothctl", "power", "on"], check=False)

    async def connect_all(self):
        self.enable_bluetooth()
        for address, name in self.sensors:
            already_connected = False
            for client, addr, _ in self.clients:
                if addr == address:
                    if client.is_connected:
                        already_connected = True
                    else:
                        logger.warning(f"{name} ({address}) esta en la lista pero desconectado")
                    break
            if already_connected:
                continue
            client = await self._connect_sensor(address, name)
            if (client):
                self.clients.append((client, address, name))

    def _on_disconnect_client(self, bleak_client):
        logger.info(f"desconectado {bleak_client.address}")
        for entry in self.clients:
            if entry[0] == bleak_client:
                self.clients.remove(entry)
                break

    async def disconnect_all(self):
        entries = list(self.clients)

        await asyncio.gather(
            *(self._disconnect_sensor(client, address, name) for client, address, name in entries),
            return_exceptions=True,
        )

        self.clients.clear()

    async def read_all_sensors(self, disconnect_sensors_on_finish = False):
        results = []
        await self.connect_all()

        if not self.clients:
            logger.warning("no se conecto a ningun sensor")
            return results
        
        logger.info("leyendo datos de los sensores...")
        for client, address, name in self.clients:
            try:
                data = await self._read_sensor_data(client, name)
                results.append({
                    "mac": address,
                    "name": name,
                    **data
                })
            except Exception as e:
                logger.error(f"[{name}] Error al leer datos: {e}")
        if disconnect_sensors_on_finish:
            await self.disconnect_all()
        return results
    
    
    async def _connect_sensor(self, address, name):
        for attempt in range(1, self.max_retries + 1):
            client = BleakClient(address, self._on_disconnect_client)
            try:
                logger.info(f"conn {name} intento {attempt} de {self.max_retries}...")
                start = time.perf_counter()
                await client.connect()
                elapsed = time.perf_counter() - start
                if client.is_connected:
                    logger.info(f"✅ {name} conectado en {elapsed:.2f} segundos")
                    return client
            except Exception as e:
                logger.warning(f"{name} error al conectar (intento {attempt}): {e}")
            await asyncio.sleep(self.retry_delay)
        logger.error(f"{name} fallaron los intentos de conexion")
        return None
    
    async def _disconnect_sensor(self, client, address=None, name=None):
        if not client:
            return False

        tag = f"{name or ''} ({address or ''})".strip()
        try:
            if getattr(client, "is_connected", False):
                # Evita quedarte colgado si el bus ya murió
                await asyncio.wait_for(client.disconnect(), timeout=5)
            logger.info(f"Desconectado OK: {tag}")
            return True
        except Exception as e:
            # Muy común: EOFError de dbus-fast cuando BlueZ cerró el socket antes de la respuesta
            logger.warning(f"[{tag}] Falló Bleak disconnect(): {e} — intento fallback bluetoothctl")
            try:
                if address:
                    subprocess.run(
                        ["bluetoothctl"],
                        input=f"disconnect {address}\n",
                        text=True,
                        timeout=5
                    )
                    logger.info(f"[{tag}] Fallback bluetoothctl desconectó")
                    return True
            except Exception as e2:
                logger.error(f"[{tag}] Fallback bluetoothctl también falló: {e2}")
            return False
    
    async def _read_sensor_data(self, client, name):
        trigger = bytes([0x01, 0x00, 0x00, 0x00])
        await client.write_gatt_char(self.CHAR_TEMP, trigger)
        await asyncio.sleep(0.1)

        temp_raw = await client.read_gatt_char(self.CHAR_TEMP)
        humid_raw = await client.read_gatt_char(self.CHAR_HUMID)
        batt_raw = await client.read_gatt_char(self.CHAR_BATT)

        temp = struct.unpack('<i', temp_raw)[0] / 100.0
        humid = struct.unpack('<i', humid_raw)[0] / 100.0
        batt_mv, batt_temp = struct.unpack('<hh', batt_raw)

        device_id = client.address

        percent = battery_mv_to_percent(batt_mv)
        last_percent = self.last_reported_battery.get(device_id)
        
        data =  {
            "temperature": temp,
            # "humidity": humid,
            "battery_mv": batt_mv,
            # "battery_temp": batt_temp,
        }

        if last_percent is None or abs(percent - last_percent) >= 10:
            data["battery_percent"] = percent
            self.last_reported_battery[device_id] = percent

        return data
    