from typing import Optional, Callable, Any
from postgrest import ReturnMethod
from supabase import acreate_client, AsyncClient
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
import logging

logger = logging.getLogger(__name__)

class SensorDataUploader:
    def __init__(self, supabase_client, mail_sender, mail_password, gateway_id):
        self.supabase: AsyncClient = supabase_client
        self.gateway_id = gateway_id
        self.config_callback = None
        self.table_telemetry = "telemetry"
        self.table_alarm = "alarm"
        self.table_panel = "messages"
        self.table_device = "device"
        self.mail_sender = mail_sender
        self.mail_password = mail_password
        self.config_callback: Optional[Callable[[Any], None]] = None
    
    @classmethod
    async def create(cls, url, key, mail_sender, mail_passoword, gateway_id):
        client = await acreate_client(url, key)
        return cls(client, mail_sender, mail_passoword, gateway_id)

    async def fetch_gateway_config(self):
        try:
            result = await self.supabase.table("gateway").select("*").eq("id", self.gateway_id).single().execute()
            if result.data:
                return result.data
            else:
                logger.warning(f"⚠️ No se encontró configuración para el gateway {self.gateway_id}")
                return None
        except Exception as e:
            logger.error(f"❌ Error al obtener configuración del gateway: {e}")
            return None

    async def listen_gateway_config_changes(self):
        def handle_config_change(payload):
            if self.config_callback:
                self.config_callback(payload["data"]["record"])

        await self.supabase.channel("gateway-config")\
            .on_postgres_changes(
                event="UPDATE",
                schema="public",
                table="gateway",
                filter=f"id=eq.{self.gateway_id}",
                callback=lambda payload: handle_config_change(payload)
            ).subscribe()
        
    async def upload_telemetry(self, data: list[dict]):
        if not data:
            logger.warning("no hay datos para subir")
            return
        try:
            await self.supabase.table(self.table_telemetry).insert(data,returning=ReturnMethod.minimal).execute()
        except Exception as e:
            logger.error(f"error al subir telemetry bd: {e}")

    async def upload_alarm(self, description: str):
        if not description:
            logger.warning("no hay datos para subir")
            return
        try:
            await self.supabase.table(self.table_alarm).insert({"description": description},returning=ReturnMethod.minimal).execute()
        except Exception as e:
            logger.error(f"error al subir alarm bd: {e}")

    async def upload_status_panel(self, description: str):
        if not description:
            logger.warning("no hay datos para subir")
            return
        try:
            await self.supabase.table(self.table_panel).update({"message": description},returning=ReturnMethod.minimal).eq("id", 1).execute()
        except Exception as e:
            logger.error(f"error al subir panel bd: {e}")
    
    async def update_batt_device(self, device_id: str,  battery_value: int):
        try:
            await self.supabase.rpc("update_battery_device", {"_id": device_id, "_battery": battery_value}).execute()
        except Exception as e:
            logger.error(f"error al subir device bd: {e}")

    async def update_status_device(self, device_id: str,  status: bool):
        try:
            await self.supabase.rpc("update_status_device", {"_id": device_id, "_status": status}).execute()
        except Exception as e:
            logger.error(f"error al subir device bd: {e}")

    def send_alarm_email(self, receivers: list[str], subject: str, body: str):
        if not self.mail_sender or not self.mail_password:
            logger.error("Faltan las credenciales del correo (ALERT_EMAIL_SENDER o ALERT_EMAIL_PASSWORD).")
            return
        
        if not receivers or not subject or not body:
            logger.error("No hay destinatarios o asunto o cuerpo del correo")
            return
        try:
            msg = MIMEText(body, 'plain')
            msg['Subject'] = subject
            msg['From'] = formataddr(('Sensor monitor', self.mail_sender))
            msg['To'] = ', '.join(receivers)

            with smtplib.SMTP("live.smtp.mailtrap.io", 587) as server:
                server.starttls()
                server.login("api",  self.mail_password)
                server.sendmail(self.mail_sender, receivers, msg.as_string())
            
            logger.info(f"correo enviado a: {receivers}")

        except Exception as e:
            logger.error(f"Error al enviar correo: {e}")
