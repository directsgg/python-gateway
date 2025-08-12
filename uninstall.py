import os 
import shutil
import subprocess

from install import SERVICE_NAME, ETC_APP_DIRECTORY

def check_root():
    if os.geteuid() != 0:
        print("Please run as root (sudo).")
        exit(1)

def stop_and_disable_service():
    """Stop and disable the systemd service"""
    print(f"Stopping and disabling {SERVICE_NAME}...")
    subprocess.run(['systemctl', 'stop', SERVICE_NAME], capture_output=True, text=True)
    subprocess.run(['systemctl', 'disable', SERVICE_NAME], capture_output=True, text=True)

    """Remove the service file."""
    service_file = f"/etc/systemd/system/{SERVICE_NAME}"
    if os.path.exists(service_file):
        os.remove(service_file)
        print(f"Service file {service_file} removed.")
    else:
        print(f"Service file {service_file} not found.")

def remove_application_folder():
    """Remove the application folder from /etc."""
    if os.path.exists(ETC_APP_DIRECTORY):
        shutil.rmtree(ETC_APP_DIRECTORY)
        print(f"Removed application directory: {ETC_APP_DIRECTORY}")
    else:
        print(f"Application directory not found: {ETC_APP_DIRECTORY}")

# def remove_hotspot():
#     """Remove the created Wi-Fi hotspot."""
#     subprocess.run(['nmcli', 'connection', 'delete', DEFAULT_HOTSPOT_NAME], capture_output=True, text=True)
def reload_systemd():
    """Reload systemd configuration"""
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True)
    subprocess.run(["systemctl", "reset-failed"], capture_output=True, text=True)
    print("Systemd daemon reloaded.")

# def uninstall():
#     remove_service_file()
#     remove_application_folder()
#     # remove_hotspot()
#     print("Uninstallation completed successfully!")

def uninstall():
    """Main uninstall function."""
    try:
        check_root()
        stop_and_disable_service()
        remove_application_folder()
        reload_systemd()
        print("Uninstall completed successfully!")
    except Exception as e:
        print(f"An error occurred during uninstall: {str(e)}")
        exit(1)

if __name__ == '__main__':
    uninstall()