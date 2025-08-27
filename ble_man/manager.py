import asyncio
import re
import struct
import subprocess
import time
from bleak import BleakClient
import logging

logger = logging.getLogger(__name__)

def battery_mv_to_percent(mv: int, temp_c: int) -> int:
    """
    Convierte mV -> % para CR2477 considerando el efecto de temperatura.
    Modelo: rango [V_min, V_max(temp)] + compresión no lineal para simular el 'plateau'.
    """
    # ---- Parámetros (ajustables) ----
    V_min = 2400            # tensión crítica: 0 %
    Vmax_20C = 2960         # celda llena a ~20 °C
    Vmax_n25C = 2650        # celda llena a ~-25 °C
    alpha = 0.60            # 0.55–0.70 da buenas curvas (más bajo = más % arriba)
    headroom_full = 6       # si mv está a <6 mV de Vmax(temp), muestra 100 %
     # ---- V_max dependiente de temperatura (interpolación lineal) ----
    if temp_c >= 20:
        V_max = Vmax_20C
    elif temp_c <= -25:
        V_max = Vmax_n25C
    else:
        # interpola entre -25 °C y 20 °C
        V_max = Vmax_n25C + (Vmax_20C - Vmax_n25C) * ((temp_c + 25) / 45.0)

    # ---- Acotaciones rápidas ----
    if mv <= V_min:
        return 0
    if mv >= V_max - headroom_full:
        return 100
    
    # ---- SOC no lineal (plateau) ----
    soc = (mv - V_min) / float(V_max - V_min)  # 0..1 relativo a la temperatura
    percent = 100.0 * (soc ** alpha)

    # Redondeo y límites
    return max(0, min(100, int(round(percent))))
        
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
            # EOFError de dbus-fast cuando BlueZ cerró el socket antes de la respuesta
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

        percent = battery_mv_to_percent(batt_mv, int(batt_temp))
        last_percent = self.last_reported_battery.get(device_id)
        
        data =  {
            "temperature": temp,
            "humidity": humid,
            "battery_mv": batt_mv,
            "battery_temp": batt_temp,
        }

        if last_percent is None or abs(percent - last_percent) >= 5:
            data["battery_percent"] = percent
            self.last_reported_battery[device_id] = percent

        return data
    