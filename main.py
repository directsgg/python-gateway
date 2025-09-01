import asyncio
import json
from logging.handlers import RotatingFileHandler
import signal
import time
from datetime import datetime, timezone, timedelta
from ble_man.manager import SensorManager
from ble_man.uploader import SensorDataUploader
from wifi_man.wifi_manager import WiFiManager
import os
from dotenv import load_dotenv
import logging

LOG_DIR = "/var/log/gateway_app"
LOG_FILE = os.path.join(LOG_DIR, "main.log")

# Crear carpeta de logs si no existe
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,  # disponible DEBUG, INFO, WARNING, ERROR, CRITICAL
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5)  # rota a 5MB, mantiene 5 copias
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

class SensorMonitorApp:
    def __init__(self, sensors: list[tuple[str, str]]):
        self.sensors = sensors
        self.sensor_manager = SensorManager(self.sensors)
        self.last_device_status = {}
        self.min_duration_after_start = timedelta(minutes=5)
        self.resend_alarm_interval = timedelta(minutes=60)
        self.active_alarm = False
        self.max_alert_threshold = 60
        self.min_alert_threshold = -40
        self.sampling_interval = 60
        self.email_recipients = []
        self.over_threshold_start = {} # mac: datetime when threshold exceeded started
        self.last_alert_sent = {} # mac: datetime of last alert sent
        self.active_alert_macs = set()

    def on_config_update(self, gateway_config):
        logger.info(gateway_config)
        if (gateway_config):
            self.sampling_interval = gateway_config.get("sampling_interval", 60)
            self.email_recipients = gateway_config.get("email_recipients", [])
            self.active_alarm = gateway_config.get("active_alarm", False)
            self.max_alert_threshold = gateway_config.get("max_alert_threshold", 60)
            self.min_alert_threshold = gateway_config.get("min_alert_threshold", -40)

    async def run(self):
        uploader = await SensorDataUploader.create(
            url=os.environ["SUPABASE_URL"], 
            key=os.environ["SUPABASE_KEY"],
            mail_sender=os.environ["ALERT_EMAIL_SENDER"],
            mail_passoword=os.environ["ALERT_EMAIL_PASSWORD"],
            gateway_id=os.environ["GATEWAY_ID"]
        )
        uploader.config_callback = self.on_config_update

        await asyncio.sleep(20)

        while True:
            gateway_config = await uploader.fetch_gateway_config()
            if not gateway_config:
                logger.warning("No se recibio configuración reintentando en 2 min")
                await asyncio.sleep(120)
            else:
                break

        self.on_config_update(gateway_config)
        await uploader.listen_gateway_config_changes()

        self.sensor_manager.disconnect_all_bluetooth_to_init()

        while True:
            startG = time.perf_counter()
            data = await self.sensor_manager.read_all_sensors(self.sampling_interval >= 300) # desconectar los sensores si sobrepasa los 5 min en muestras
            logger.info(f"\n resultados finales:")
            payload_telemetry = []
            
            active_macs = {d["mac"] for d in data}
            for mac, _ in self.sensors:
                is_active = mac in active_macs
                last_status = self.last_device_status.get(mac)

                if last_status is None or last_status != is_active:
                    await uploader.update_status_device(mac, is_active)
                    self.last_device_status[mac] = is_active
                    estado_str = "activo" if is_active else "inactivo"
                    logger.info(f"se publico cambio de estado de {mac} a {estado_str}")
            
            now = datetime.now(timezone.utc) 
            alerts_to_send: list[dict] = [] 
            for d in data:
                logger.info(data)
                mac = d["mac"]
                temp = d["temperature"]
                payload_telemetry.append({
                    "publisher": mac,
                    "value": temp,
                })

                if self.active_alarm:
                    out_of_range = (temp > self.max_alert_threshold) or (temp < self.min_alert_threshold)

                    if out_of_range:
                        if mac not in self.over_threshold_start:
                            self.over_threshold_start[mac] = now

                        duration = now - self.over_threshold_start[mac]
                        logger.info(f"duracion de {mac} es {duration}")
                        if duration >= self.min_duration_after_start:
                            self.active_alert_macs.add(mac)

                            last_sent = self.last_alert_sent.get(mac)
                            if not last_sent or ((now - last_sent) >= self.resend_alarm_interval):
                                alerts_to_send.append({
                                    "mac": mac,
                                    "temperature": temp,
                                    "duration_minutes": int(duration.total_seconds() // 60)
                                })
                                self.last_alert_sent[mac] = now
                    else:
                        if mac in self.over_threshold_start:
                            del self.over_threshold_start[mac]
                        if mac in self.active_alert_macs:
                            self.active_alert_macs.remove(mac)
                        if mac in self.last_alert_sent:
                            del self.last_alert_sent[mac]

                if d.get("battery_percent") is not None:
                    await uploader.update_batt_device(d["mac"], d["battery_percent"])
                    logger.info(f"se publico nivel de batteria {d['battery_percent']}% de {d['mac']}")
            
            await uploader.upload_telemetry(payload_telemetry)
            if alerts_to_send:
                logger.info(alerts_to_send)
                lines = [f"Alerta de nuevos sensor(es) con fuera del rango de temperatura:\n"]
                lines.append(f"Umbrales configurados: Máx {self.max_alert_threshold} °C, Mín {self.min_alert_threshold} °C\n")

                for a in alerts_to_send:
                    lines.append(
                        f"- MAC: {a['mac']}, Temperatura: {a['temperature']} °C, durante al menos {a['duration_minutes']} minutos\n"
                    )
                
                message = "\n".join(lines)
                uploader.send_alarm_email(self.email_recipients, "¡Alerta de temperatura!", message)
                message_alarm = f"¡Alerta de temperatura! \n {message}"
                await uploader.upload_alarm(message_alarm)

                message_panel = [
                    "1" if mac in self.active_alert_macs else "0"
                    for mac, _ in self.sensors
                ]
                message_panel_str= "[" + ",".join(message_panel) + "]"
                logger.info(f"message_panel {message_panel_str}")
                await uploader.upload_status_panel(message_panel_str)
            
            # verificacion de tiempo de espera

            elapsedG = time.perf_counter() - startG
            now = datetime.now(timezone.utc) 

            base_delay = max(self.sampling_interval - elapsedG, 0.2)
            next_sample_time = now + timedelta(seconds=base_delay)

            guard_seconds = 1.0

            all_macs = [mac for mac, _ in self.sensors]

            for mac in all_macs:
                start_time = self.over_threshold_start.get(mac)
                last_sent = self.last_alert_sent.get(mac)

                if start_time and (not last_sent):
                    first_deadline = start_time + self.min_duration_after_start
                    if first_deadline > now:
                        next_sample_time = min(next_sample_time, first_deadline)

                if (mac in self.active_alert_macs) and last_sent:
                    resend_deadline = last_sent + self.resend_alarm_interval
                    if resend_deadline > now:
                        next_sample_time = min (next_sample_time, resend_deadline)

            delay_between_samples = max((next_sample_time - now).total_seconds() - guard_seconds, 0.2)
            logger.info(
                f"🕒 Próxima muestra en {delay_between_samples:.2f}s "
                f"(base {base_delay:.2f}s, next={next_sample_time.isoformat()})"
            )

            await asyncio.sleep(delay_between_samples)

async def main():
    wifi = WiFiManager(host="192.168.4.1", port=8080)
    # wifi = WiFiManager(host="0.0.0.0", port=8080)

    with open("sensors.json", "r") as f:
        SENSORS = json.load(f)
    logger.info(f"Sensores cargados: {SENSORS}")

    monitor = SensorMonitorApp(SENSORS)
    
    def handle_sigusr1():
        current_level = logger.getEffectiveLevel()
        if current_level == logging.WARNING:
            logger.setLevel(logging.INFO)
            logger.info("Logger cambio a nivel INFO")
        else:
            logger.setLevel(logging.WARNING)
            logger.warning("Logger cambio a nivel WARNING")
        logger.warning(f"Sampling interval: {monitor.sampling_interval}")

    signal.signal(signal.SIGUSR1, lambda *args: handle_sigusr1())

    await asyncio.gather(
        wifi.start_web_server(),
        monitor.run()
    )

if __name__ == "__main__":
    asyncio.run(main())
