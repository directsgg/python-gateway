import asyncio
import json
from logging.handlers import RotatingFileHandler
import time
from datetime import datetime, timezone, timedelta
from ble_man.manager import SensorManager
from ble_man.uploader import SensorDataUploader
from wifi_man.wifi_manager import WiFiManager
import os
from dotenv import load_dotenv
import logging

# LOG_DIR = "/var/log/gateway_app"
# LOG_FILE = os.path.join(LOG_DIR, "main.log")

# # Crear carpeta de logs si no existe
# os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,  # disponible DEBUG, INFO, WARNING, ERROR, CRITICAL
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        # RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5)  # rota a 5MB, mantiene 5 copias
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

        await asyncio.sleep(10)

        gateway_config = await uploader.fetch_gateway_config()
        self.on_config_update(gateway_config)
        await uploader.listen_gateway_config_changes()

        self.sensor_manager.disconnect_all_bluetooth_to_init()

        while True:
            startG = time.perf_counter()
            data = await self.sensor_manager.read_all_sensors(self.sampling_interval > 600) # desconectar los sensores si sobrepasa los 10 min en muestras
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
            alerts_to_send = [] 
            for d in data:

                mac = d["mac"]
                temp = d["temperature"]
                payload_telemetry.append({
                    "publisher": mac,
                    "value": temp,
                })

                if self.active_alarm:
                    if temp > self.max_alert_threshold or temp < self.min_alert_threshold:
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
                                    "duration_minutes": int(duration.total_seconds() / 60)
                                })
                                self.last_alert_sent[mac] = now
                    else:
                        if mac in self.over_threshold_start:
                            del self.over_threshold_start[mac]
                        if mac in self.active_alert_macs:
                            self.active_alert_macs.remove(mac)

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

            delay_between_samples = round(self.sampling_interval - elapsedG, 2)
            delay_between_samples = max(delay_between_samples, 0.2)

            next_sample_time = now + timedelta(seconds=delay_between_samples)

            has_critical_monitoring = False
            adjusted_for_deadline = False

            for mac in active_macs:
                start_time_1 = self.over_threshold_start.get(mac)
                last_sent_1 = self.last_alert_sent.get(mac)

                if start_time_1:
                    deadline = start_time_1 + self.min_duration_after_start
                    
                    if not last_sent_1 and now < deadline <= next_sample_time:
                        delay_until_deadline = (deadline - now).total_seconds()
                        delay_between_samples = max(delay_until_deadline - 1, 0.2)
                        logger.info(f"⏱ Ajuste de muestreo para alcanzar deadline de alerta ({delay_until_deadline:.2f}s)")
                        adjusted_for_deadline = True
                        break

                    if last_sent_1:
                        time_since_alert = now - last_sent_1
                        if time_since_alert < self.resend_alarm_interval:
                            resend_deadline = last_sent_1 + self.resend_alarm_interval
                            if now < resend_deadline <= next_sample_time:
                                delay_until_resend = (resend_deadline - now).total_seconds()
                                delay_between_samples = max(delay_until_resend - 1, 0.2)
                                logger.info(f"Ajuste de muestreo para reenvio ({delay_until_resend:.2f}s)")
                            else:
                                logger.info(f"proxima muestra en {delay_between_samples:.2f}s cumple reenvío sin ajuste")
        
                            has_critical_monitoring = True
                            break
            
            if not adjusted_for_deadline and not has_critical_monitoring:
                logger.info(f"🕒 Esperando {delay_between_samples}s = {self.sampling_interval} - {elapsedG:.2f}")

            await asyncio.sleep(delay_between_samples)


async def main():
    wifi = WiFiManager(host="192.168.4.1", port=8080)
    # wifi = WiFiManager(host="0.0.0.0", port=8080)

    with open("sensors.json", "r") as f:
        SENSORS = json.load(f)
    logger.info(f"Sensores cargados: {SENSORS}")

    monitor = SensorMonitorApp(SENSORS)

    await asyncio.gather(
        wifi.start_web_server(),
        monitor.run()
    )

if __name__ == "__main__":
    asyncio.run(main())