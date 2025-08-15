import asyncio
import re
import socket
import subprocess
import threading
from flask import Flask, jsonify, render_template, request
import logging

logger = logging.getLogger(__name__)

WIFI_DEVICE_INT = "wlan0"

class WiFiManager:
    def __init__(self, host="0.0.0.0", port=8081) -> None:
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        self._register_routes()

    def _register_routes(self):
        @self.app.route("/", methods=["GET"])
        def index():
            # wifi_networks = self._scan_wifi_networks()
            # return render_template('index.html',  wifi_networks=wifi_networks)
            return render_template('index.html')
        
        @self.app.route("/rescan_wifi")
        def rescan_wifi():
            # active_bssid = self._get_active_wifi_bssid()
            wifi_list = self._scan_wifi_details()

            # for network in wifi_list:
            #     if active_bssid and network["mac"] == active_bssid:
            #         network["is_connected"] = True
            #     else:
            #         network["is_connected"] = False
            return jsonify(wifi_list)

        @self.app.route("/connect", methods=["POST"])
        def connect():
            ssid = request.form.get("ssid")
            password = request.form.get("password")
            if not ssid:
                return jsonify({'success': False, 'message': 'El nombre de la red (SSID) no puede estar vacío.'}), 400
            
            try:
                if self._check_connection_exists(ssid):
                    subprocess.run(["nmcli", "connection", "delete", ssid], check=True, capture_output=True)

                connection_command = ["nmcli", "device", "wifi", "connect", ssid, "ifname", WIFI_DEVICE_INT]
                if password:
                    connection_command.extend(["password", password])
                subprocess.run(connection_command, capture_output=True, text=True, check=True)

                modify_command = ["nmcli", "connection", "modify", ssid, 
                                "connection.autoconnect", "yes", 
                                "connection.autoconnect-priority", "100"
                                ]
                subprocess.run(modify_command, check=True)
                return jsonify({"success": True, "message": f"Conectado a la red {ssid}..."}), 200
            except subprocess.CalledProcessError as e:
                error_message = e.stderr.lower() if e.stderr else ""

                if 'secrets were required' in error_message or 'invalid key' in error_message:
                    message = 'Error de autenticación. La contraseña podría ser incorrecta.'
                elif 'no network with ssid' in error_message:
                    message = f'No se encontró ninguna red con el nombre "{ssid}".'
                else:
                    message = f'No se pudo conectar a la red. Verifique credenciales. {error_message}'
                
                return jsonify({'success': False, 'message': message}), 400
            except Exception as eg:
                return jsonify({'success': False, 'message': f'Ocurrió un error en el servidor'}), 500

        @self.app.route("/disconnect", methods=["POST"])
        def disconnect():
            try:
                subprocess.run(["nmcli", "device", "disconnect", WIFI_DEVICE_INT], check=True)
                return jsonify({"success": True, "message": "Desconexion realizada"})
            except subprocess.CalledProcessError as e:
                return jsonify({"success": False, "message": f"Ocurrio un error interno"}), 500

        @self.app.route("/connection_status", methods=["GET"])
        def connection_status():
            return jsonify(self._get_connection_status()) 

    def _get_connection_status(self):
        try:
            result = subprocess.check_output(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"]).decode("utf-8").strip()
            split_pattern = r'(?<!\\):'

            for line in result.split("\n"):
                if not line:
                    continue
                parts = re.split(split_pattern, line)
                if parts[0] == "yes":
                    active_ssid = parts[1].replace("\\:", ":")
                else:
                    continue

                if self._check_internet_connection():
                    return {
                        "status": "online",
                        "message": "Conectado a internet",
                        "ssid": active_ssid
                    }
                else:
                    return {
                        "status": "local", 
                        "message": "Conectado a la red (sin Internet)", 
                        "ssid": active_ssid
                    }
        except Exception as e:
            logger.info(f"No se pudo obener estado de conexion: {e}")
        
        return {"status": "offline", "message": "Desconectado", "ssid": None}

    def _check_internet_connection(self):
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            return True
        except OSError:
            return False
        
    # def _get_active_wifi_bssid(self):
    #     try:
    #         result = subprocess.check_output(["nmcli", "-t", "-f", "ACTIVE,BSSID", "device", "wifi"]).decode("utf-8").strip()
            
    #         split_pattern = r'(?<!\\):'

    #         for line in result.split("\n"):
    #             if not line:
    #                 continue
    #             parts = re.split(split_pattern, line)
    #             if parts[0] == "yes":
    #                 return parts[1].replace("\\:", ":")
    #     except Exception as e:
    #         logger.info(f"No se pudo obener la conexion activa: {e}")
    #     return None
        
    def _scan_wifi_details(self):
        networks = []
        try:
            result = subprocess.check_output(["nmcli", "-t", "-f", "BSSID,SSID,CHAN,SIGNAL,SECURITY", "device", "wifi", "list"]).decode("utf-8").strip()
            
            split_pattern = r'(?<!\\):'

            for line in result.split("\n"):
                if not line:
                    continue

                parts = re.split(split_pattern, line)
                if len(parts) >= 5:
                    ssid = parts[1].replace("\\:", ":") 
                    if len(parts[1]) > 0: 
                        networks.append({
                            "mac": parts[0].replace("\\:", ":"),
                            "ssid": ssid,
                            "channel": parts[2],
                            "signal": parts[3],
                            "security": parts[4] if len(parts[4]) > 0 else "Abierta"
                        })
        except Exception as e:
            logger.error(f"Error al escaner Wi-Fi con detalles: {e}")
        return networks
    
    def _scan_wifi_networks(self):
        try:
            result = subprocess.check_output(
                ["nmcli", "--colors", "no", "-m", "multiline", "--get-value", "SSID", "dev", "wifi", "list", "ifname", WIFI_DEVICE_INT])
            ssids_list = result.decode().split('\n')
            return [ssid.removeprefix("SSID:") for ssid in ssids_list if ssid.startswith("SSID:")]
        except subprocess.CalledProcessError as e:
            logger.info(f"Error scannin Wi-Fi networks: {e}")
            return []
        
    def _check_connection_exists(self, ssid):
        try:
            cmd = ['nmcli', '-t', '-f', 'NAME', 'connection', 'show']
            result = subprocess.check_output(cmd).decode('utf-8')
            existing_connections = result.strip().split('\n')

            return ssid in existing_connections
        except Exception as e:
            logger.info(f"Error al verificar la existencia de la conexión '{ssid}': {e}")
            return False
        
    async def start_web_server(self):

        logWeb = logging.getLogger("werkzeug")
        logWeb.setLevel(logging.WARNING)
        
        def run_flask():
            self.app.run(debug=False, host=self.host, port=self.port)

        threading.Thread(target=run_flask, daemon=True).start()
        logger.info(f"WiFiManager escuchando en {self.host}:{self.port}")
        while True:
            await asyncio.sleep(3600)

#     # depuracion eliminarlo
#     def start_web_server(self):
#         self.app.run(debug=True, host=self.host, port=self.port)



# # depuracion eliminarlo
# wifi = WiFiManager(host="0.0.0.0", port=8081)
# wifi.start_web_server()

