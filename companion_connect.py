import sys
import socket
import time
import threading
import json
import requests
import os
import psutil
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient
# TODO: create log buffer or something, since osc gets started before add checks to make sure things dont break when companion_host_name/_ip is None
# ---------------- USER CONFIG ----------------
companion_hostname_list = ["Aspire14.local", "Skybox-Lighting.local", "Cave-Video-Switcher.local", "Chapel.local"]

osc_port = 7777

send_path = "/custom-variable/RaspberryPiData/value"
receive_path = "/python/script/command"

pi_name = "RaspberryPi #1 - User's Pi"
MAX_LOGS = 100

REPO_DIR = "/home/tech-ministry/companion_controller"
VENV_PYTHON = "/home/tech-ministry/companion-env/bin/python"
# --------------------------------------------


# ---------------- RUNTIME VARIABLES ----------------
SCRIPT_VERSION = 1.0

SCRIPT_PATH = f"{REPO_DIR}/companion_connect.py"

companion_host_name = None
companion_host_ip = None
companion_sender_host_name = None
companion_sender_host_ip = None
local_ip = None
satellite_api = "http://127.0.0.1:9999/api"

log_main = []
log_command = []
clients = {}
# --------------------------------------------------


# ---------------- MAIN FUNCTIONS ----------------
def receive(command, data):
    log(f"[OSC RECEIVE] '{command}' command recieved from '{companion_sender_host_name}' with data '{data}'")

    match command:
        # Send commands
        case "Send Ping":
            log(f"[OSC SEND CMD] Sending ping")
            send(["Recv RaspberryPi Ping", pi_name, local_ip])

        case "Send Connection Status":
            log(f"[OSC SEND CMD] Sending connection status")
            send([
                "Recv RaspberryPi Connection Status",
                pi_name,
                companion_host_ip,
                get_satellite_ip(),
                str(check_satellite_connectivity())
            ])

        case "Send Hostname List":
            log(f"[OSC SEND CMD] Sending hostname list")
            send(["Recv RaspberryPi Hostname List", pi_name, companion_hostname_list])

        case "Send System Status":
            log(f"[OSC SEND CMD] Sending system status")
            stats = {
                "cpu": psutil.cpu_percent(),
                "memory": psutil.virtual_memory().percent,
                "uptime": time.time() - psutil.boot_time()
            }
            send(["System Stats", stats])

        # Recieve commands
        case "Recv Set Hostname":
            if data:
                new_host = data[0]
                log(f"[OSC RECV CMD] Setting host → {new_host}")
                set_hostname(new_host)
            else:
                log(f"[ERROR] Missing required data: {data}")

        case "Recv Satellite Reboot":
            log("[OSC RECV CMD] Restarting satellite service")
            os.system("sudo systemctl restart companion-satellite")
        
        case "Recv Script Update":
            log("[OSC RECV CMD] Updating script")

            try:
                # Step 1: Get current commit
                prev_commit = os.popen(f"cd {REPO_DIR} && git rev-parse HEAD").read().strip()
                log(f"[SCRIPT] Current commit: {prev_commit}")

                # Step 2: Pull update
                if os.system(f"cd {REPO_DIR} && git pull") != 0:
                    log("[SCRIPT] Git pull failed")
                    return

                log("[SCRIPT] Git pull success")

                # Step 3: Install dependencies
                if os.system(f"{VENV_PYTHON} -m pip install -r {REPO_DIR}/requirements.txt") != 0:
                    log("[SCRIPT] Dependency install failed")
                    return

                log("[SCRIPT] Dependencies updated")

                # Step 4: Syntax check
                if os.system(f"{VENV_PYTHON} -m py_compile {SCRIPT_PATH}") != 0:
                    log("[SCRIPT] Syntax check FAILED → rolling back")

                    os.system(f"cd {REPO_DIR} && git reset --hard {prev_commit}")
                    log("[SCRIPT] Rollback complete")
                    return

                log("[SCRIPT] Syntax check passed")

                # Step 5: Restart
                log("[SCRIPT] Restarting via systemd...")
                os._exit(0)

            except Exception as e:
                log(f"[SCRIPT ERROR] {e}")

        case "Recv System Shutdown":
            log("[RECV OSC CMD] System shutting down")
            os.system("sudo shutdown now")

        case "Recv System Reboot":
            log("[RECV OSC CMD] System rebooting")
            os.system("sudo reboot")

        case "Recv Script Shutdown":
            log("[RECV OSC CMD] Script shutting down")
            os._exit(0)

        case _:
            log(f"[OSC CMD] Unknown command: {command}")

def osc_handler(address, *args):
    if address != receive_path:
        return

    if not args:
        log("[OSC ERROR] No data received")
        return
    
    global log_command, companion_sender_host_name, companion_sender_host_ip
    log_command = ["Recv RaspberryPi Logs"]
    
    command_data = args[0]
    log(f"[OSC] Raw command data {command_data}")

    try:
        # parse the JSON string
        parsed = json.loads(command_data)
    except Exception as e:
        log(f"[ERROR] Invalid command data JSON: {command_data} → {e}")
        return

    if not isinstance(parsed, list) or len(parsed) < 2:
        log(f"[ERROR] Invalid command data format after parsing: {parsed}")
        return

    companion_sender_host_name = parsed[0]
    companion_sender_host_ip = convert_hostname(companion_sender_host_name)
    command = parsed[1]
    data = parsed[2:] if len(parsed) > 2 else []

    receive(command, data)
    send(log_command)
    if(companion_sender_host_ip != companion_host_ip):
        log_command[0] = "Recv External RaspberryPi Logs"
        send(log_command,companion_host_ip)

def main():
    global local_ip, companion_host_ip

    local_ip = wait_for_wifi()
    
    companion_connect()

    threading.Thread(target=start_osc_server, daemon=True).start()

    log("[MAIN] System ready")

    while True:
        try:
            socket.create_connection((companion_host_ip, 16622), timeout=3)
        except:
            log("[NETWORK] Lost connection, re-resolving same host")
            try:
                set_hostname(companion_host_name)
            except:
                log("[NETWORK] Failed to re-resolve host")
                continue
        time.sleep(60)
# -----------------------------------------



# ---------------- OSC----------------

def get_client(ip):
    if not ip:
        log(f"[ERROR] Cannot get client from invalid ip '{ip}'")
        return
    if ip not in clients:
        clients[ip] = SimpleUDPClient(ip, osc_port)
    return clients[ip]


def log(message):
    print(message)

    log_main.append(message)
    log_command.append(message)
    if len(log_main) > MAX_LOGS:
        log_main.pop(0)

def send(data, send_ip = companion_sender_host_ip):
    try:
        client = get_client(send_ip)
        client.send_message(send_path, json.dumps(data))
        log(f"[OSC SEND] {data}")
    except Exception as e:
        log(f"[ERROR] Sending error: {e}")


def start_osc_server():
    dispatcher = Dispatcher()
    dispatcher.set_default_handler(osc_handler)

    server = ThreadingOSCUDPServer(("0.0.0.0", osc_port), dispatcher)
    log(f"[OSC] Listening on port {osc_port}")
    server.serve_forever()
# -----------------------------------------



# ---------------- SATELLITE ----------------
def get_satellite_ip():
    try:
        r = requests.get(f"{satellite_api}/host", timeout=5)
        if r.status_code == 200:
            return r.text.strip()
    except Exception as e:
        log(f"[SAT ERROR] {e}")
    return None


def set_satellite_ip(ip):
    log(f"[SAT] Setting IP → {ip}")
    try:
        r = requests.post(f"{satellite_api}/host", json={"host": ip}, timeout=5)
        if r.status_code != 200:
            log(f"[SAT ERROR] Bad response: {r.status_code}")
    except Exception as e:
        log(f"[SAT ERROR] {e}")


def check_satellite_connectivity():
    try:
        if get_satellite_ip() != companion_host_ip:
            return False

        sock = socket.create_connection((companion_host_ip, 16622), timeout=5)
        sock.close()

        return True
    except:
        return False
# -----------------------------------------


# ---------------- NETWORK ----------------

def wait_for_wifi():
    log("[NETWORK] Waiting for WiFi...")
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            log(f"[NETWORK] Connected: {ip}")
            return ip
        except:
            time.sleep(1)

def convert_hostname(hostname):
    try:
        ip = socket.gethostbyname(hostname)
        log(f"[NETWORK] Resolve hostname '{hostname}' to ip '{ip}'")
        return ip
    except:
        log(f"[ERROR] Failed to resolve hostname {hostname}")
        return None

def set_hostname(hostname):
    global companion_host_ip, companion_host_name
    try:
        companion_host_ip = socket.gethostbyname(hostname)
        companion_host_name = hostname
        set_satellite_ip(companion_host_ip)
    except:
        log(f"[NETWORK] Failed to resolve hostname {hostname}")
            
def companion_connect():
    global companion_host_name, companion_host_ip
    log("[NETWORK] Resolving hostnames...")

    while True:
        for host in companion_hostname_list:
            try:
                socket.setdefaulttimeout(1)
                ip = socket.gethostbyname(host)

                try:
                    get_client(ip).send_message(
                        send_path,
                        json.dumps(["Resolution Success", host, ip])
                    )
                except:
                    pass

                log(f"[NETWORK] Resolved {host} → {ip}")
                companion_host_name = host
                companion_host_ip = ip
                return

            except:
                try:
                    get_client(host).send_message(
                        send_path,
                        json.dumps(["Resolution Failed", host])
                    )
                except:
                    pass

                log(f"[NETWORK] Failed to resolve: {host}")

        log("[NETWORK] Retrying hostname cycle...")

# -----------------------------------------


if __name__ == "__main__":
    main()
