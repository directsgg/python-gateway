import os
import shutil
import subprocess
import sys

SERVICE_NAME = "gatewayapp.service"
APP_DIRECTORY = "./"
ETC_APP_DIRECTORY = "/etc/gateway_app"
REQUIREMENTS_FILE = "requeriments.txt"
PYTHON_ENTRY = "main.py"

def check_root():
    if os.geteuid() != 0:
        print("Please run as root (sudo).")
        exit(1)

def setup_virtualenv():
    """ Setup virtual environment inside ETC_APP_DIRECTORY"""
    full_app_dir = os.path.join(os.path.dirname(__file__), APP_DIRECTORY)

    if not os.path.exists(full_app_dir):
        print(f"The application folder was not found.: {full_app_dir}")
        exit(1)

    if not os.path.exists(ETC_APP_DIRECTORY):
        shutil.copytree(full_app_dir, ETC_APP_DIRECTORY)
        print("Application folder copied to /etc")
    else:
        print("Application folter already exists in /etc")

    venv_path = os.path.join(ETC_APP_DIRECTORY, ".venv")
    subprocess.run(["python3", "-m", "venv", venv_path], check=True)
    print(f"Virtual environment created successfully at {venv_path}")

def install_requeriments():
    """ Install python dependecies from requeriments.txt"""
    requeriments_path = os.path.join(ETC_APP_DIRECTORY, REQUIREMENTS_FILE)
    venv_path = os.path.join(ETC_APP_DIRECTORY, ".venv")
    if os.path.exists(requeriments_path):
        if os.path.exists(venv_path):
            subprocess.run([os.path.join(venv_path, "bin", "pip"), "install", "-r", requeriments_path], check=True)
        else:
            print(f"Virtual environment not found at {venv_path}. Please run setup_virtualenv() first.")
            exit(1)
    else:
        print(f"Requirements file '{REQUIREMENTS_FILE}' not found.")
        exit(1)

def setup_autorun():
    """Setup autorun for the main application"""
    service_file = f"/etc/systemd/system/{SERVICE_NAME}"

    # Stop service if it's already running
    subprocess.run(["systemctl", "stop", SERVICE_NAME], capture_output=True, text=True)

    with open(service_file, "w") as f:
        f.write(f"[Unit]\n"
                f"Description=Gateway ble wifi service\n"
                f"After=multi-user.target\n"
                f"Requires=network.target\n\n"
                f"[Service]\n"
                f"Type=simple\n"
                f"ExecStart={ETC_APP_DIRECTORY}/.venv/bin/python {ETC_APP_DIRECTORY}/{PYTHON_ENTRY}\n"
                f"Restart=always\n"
                f"RestartSec=10\n"
                f"User=root\n\n"
                f"WorkingDirectory={ETC_APP_DIRECTORY}\n\n"
                f"[Install]\n"
                f"WantedBy=multi-user.target\n"
                )
    
    os.chmod(service_file, 0o644)
    daemon_reload = subprocess.run(['systemctl', 'daemon-reload'], capture_output=True, text=True)
    if daemon_reload.returncode != 0:
        print("Error reloading systemd daemon:", daemon_reload.stderr)
        exit(1)

    enable_service = subprocess.run(['systemctl', 'enable', SERVICE_NAME], capture_output=True, text=True)
    if enable_service.returncode != 0:
        print("Error enabling service:", enable_service.stderr)
        exit(1)
    
    start_service = subprocess.run(['systemctl', 'start', SERVICE_NAME], capture_output=True, text=True)
    if start_service.returncode != 0:
        print("Error starting service:", start_service.stderr)
        exit(1)

    print(f"Setup service \"{SERVICE_NAME}\" completed successfully!")

def setup():
    try:
        check_root()
        setup_virtualenv()
        install_requeriments()
        setup_autorun()
        print("Setup completed successfully!")
    
    except Exception as e:
        print(f"An error ocurred during setup: {str(e)}")
        exit(1)

if __name__ == "__main__":
    setup()