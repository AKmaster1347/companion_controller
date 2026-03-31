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

# ---------------- USER CONFIG ----------------
companion_hostname_list = ["Lighting.local","Aspire14.local"]

osc_port = 7777

send_path = "/custom-variable/RaspberryPiData/value"
receive_path = "/python/script/data"

pi_name = "RaspberryPi #1 - User's Pi"
MAX_LOGS = 100

REPO_DIR = "/home/tech-ministry/companion_controller"
VENV_PYTHON = "/home/tech-ministry/companion-env/bin/python"
# --------------------------------------------


# ---------------- RUNTIME VARIABLES ----------------
SCRIPT_PATH = f"{REPO_DIR}/companion_connect.py"

companion_network_address = None
local_ip = None
satellite_api = "http://127.0.0.1:9999/api"

log_buffer = []
clients = {}
# --------------------------------------------------


# ---------------- COMMANDS ----------------
def receive(sender, command, data, buffer):
    log(f"[OSC RECEIVE] {command} from {sender}", buffer)

    match command:

        case "Send_Ping":
            send([pi_name, local_ip])

        case "Send_Connection_Status":
            send([
                pi_name,
                companion_network_address,
                get_satellite_ip(),
                str(check_satellite_connectivity())
            ])

        case "Send_Hostname_List":
            send(["Hostnames", companion_hostname_list])

        case "Set_Host":
            if data:
                new_host = data[0]
                log(f"[CMD] Setting host → {new_host}", buffer)
                set_satellite_ip(new_host)

        case "Get_Host":
            send(["Host", get_satellite_ip()])

        case "Recv_Reboot_Satellite":
            log("[SAT] Restarting satellite service", buffer)
            os.system("sudo systemctl restart companion-satellite")

        case "Send_System_Stats":
            stats = {
                "cpu": psutil.cpu_percent(),
                "memory": psutil.virtual_memory().percent,
                "uptime": time.time() - psutil.boot_time()
            }
            send(["System_Stats", stats])

        case "Update_Script":
            log("[SCRIPT] Starting safe update...", buffer)

            try:
                # Step 1: Get current commit
                prev_commit = os.popen(f"cd {REPO_DIR} && git rev-parse HEAD").read().strip()
                log(f"[SCRIPT] Current commit: {prev_commit}", buffer)

                # Step 2: Pull update
                if os.system(f"cd {REPO_DIR} && git pull") != 0:
                    log("[SCRIPT] Git pull failed", buffer)
                    return

                log("[SCRIPT] Git pull success", buffer)

                # Step 3: Install dependencies
                if os.system(f"{VENV_PYTHON} -m pip install -r {REPO_DIR}/requirements.txt") != 0:
                    log("[SCRIPT] Dependency install failed", buffer)
                    return

                log("[SCRIPT] Dependencies updated", buffer)

                # Step 4: Syntax check
                if os.system(f"{VENV_PYTHON} -m py_compile {SCRIPT_PATH}") != 0:
                    log("[SCRIPT] Syntax check FAILED → rolling back", buffer)

                    os.system(f"cd {REPO_DIR} && git reset --hard {prev_commit}")
                    log("[SCRIPT] Rollback complete", buffer)
                    return

                log("[SCRIPT] Syntax check passed", buffer)

                # Step 5: Restart
                log("[SCRIPT] Restarting via systemd...", buffer)
                os._exit(0)

            except Exception as e:
                log(f"[SCRIPT ERROR] {e}", buffer)

        case "Restart_Script":
            log("[SCRIPT] Restarting script", buffer)
            os._exit(0)

        case "Recv_Satellite_IP":
            if data:
                set_satellite_ip(data[0])

        case "Recv_System_Shutdown":
            log("[SYSTEM] Shutdown command received", buffer)
            os.system("sudo shutdown now")

        case "Recv_Reboot":
            log("[SYSTEM] Reboot command received", buffer)
            os.system("sudo reboot")

        case "Recv_Script_Shutdown":
            log("[SYSTEM] Script shutdown", buffer)
            os._exit(0)

        case _:
            log(f"[OSC] Unknown command: {command}", buffer)
# -----------------------------------------



# ---------------- MAIN ----------------
def main():
    global local_ip, companion_network_address

    local_ip = wait_for_wifi()

    companion_network_address = resolve_companion_hostname()

    threading.Thread(target=start_osc_server, daemon=True).start()

    set_satellite_ip(companion_network_address)

    log("[MAIN] System ready")

    while True:
        self_heal_connection()
        time.sleep(60)
# -----------------------------------------



# ---------------- OSC----------------

def get_client(ip):
    if ip not in clients:
        clients[ip] = SimpleUDPClient(ip, osc_port)
    return clients[ip]


def log(message, buffer=None):
    print(message)

    log_buffer.append(message)
    if len(log_buffer) > MAX_LOGS:
        log_buffer.pop(0)

    if buffer is not None:
        buffer.append(message)


def send(data):
    try:
        client = get_client(companion_network_address)
        client.send_message(send_path, json.dumps(data))
        log(f"[OSC SEND] {data}")
    except Exception as e:
        log(f"[OSC ERROR] {e}")


def resolve_sender(sender):
    try:
        return socket.gethostbyname(sender)
    except:
        return None


def dispatch_logs(sender, buffer):
    if not buffer:
        return

    sender_ip = resolve_sender(sender)

    targets = set()

    if sender_ip:
        targets.add(sender_ip)
    else:
        # fallback ONLY to current host
        if companion_network_address:
            targets.add(companion_network_address)

    if companion_network_address:
        targets.add(companion_network_address)

    for target in targets:
        try:
            client = get_client(target)

            client.send_message(
                send_path,
                json.dumps([
                    "Recv Raspberry Pi Logs",
                    pi_name,
                    ", ".join(buffer)
                ])
            )

            if target == companion_network_address and sender_ip != companion_network_address:
                client.send_message(
                    send_path,
                    json.dumps([
                        "External Command Notice",
                        pi_name,
                        f"Command from {sender}"
                    ])
                )

        except:
            pass


def osc_handler(address, *args):
    if address != receive_path:
        return

    if not args:
        log("[OSC ERROR] No data received")
        return

    raw = args[0]

    # sanitize the raw string: replace spaces with underscores
    raw_sanitized = raw.replace(" ", "_")

    try:
        # parse the JSON string
        parsed = json.loads(raw_sanitized)
    except Exception as e:
        log(f"[OSC PARSE ERROR] Invalid JSON: {raw} → {e}")
        return

    # minimal validation
    if not isinstance(parsed, list) or len(parsed) < 2:
        log(f"[OSC ERROR] Invalid format after parsing: {parsed}")
        return

    sender = parsed[0]
    command = parsed[1]
    data = parsed[2:] if len(parsed) > 2 else []

    # call receive OUTSIDE the try block to allow things like os._exit() to propagate
    buffer = []
    receive(sender, command, data, buffer)
    dispatch_logs(sender, buffer)

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
        requests.post(f"{satellite_api}/host", json={"host": ip}, timeout=5)
    except Exception as e:
        log(f"[SAT ERROR] {e}")


def check_satellite_connectivity():
    try:
        if get_satellite_ip() != companion_network_address:
            return False

        sock = socket.create_connection((companion_network_address, 16622), timeout=5)
        sock.close()

        return True
    except:
        return False
# -----------------------------------------


# ---------------- NETWORK ----------------
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return None


def wait_for_wifi():
    log("[NETWORK] Waiting for WiFi...")
    while True:
        ip = get_local_ip()
        if ip:
            log(f"[NETWORK] Connected: {ip}")
            return ip
        time.sleep(2)


def resolve_companion_hostname():
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
                return ip

            except:
                try:
                    get_client(host).send_message(
                        send_path,
                        json.dumps(["Resolution Failed", host])
                    )
                except:
                    pass

                log(f"[NETWORK] Failed: {host}")

        log("[NETWORK] Retrying hostname cycle...")


def self_heal_connection():
    global companion_network_address

    if not companion_network_address:
        return

    try:
        socket.create_connection((companion_network_address, 16622), timeout=3)
    except:
        log("[HEAL] Lost connection, re-resolving same host")

        try:
            companion_network_address = socket.gethostbyname(companion_network_address)
            set_satellite_ip(companion_network_address)
        except:
            log("[HEAL] Failed to re-resolve host")
# -----------------------------------------


if __name__ == "__main__":
    main()
