import base64
import logging
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

JELLYFIN_URL = "http://192.168.50.13:8096"
CEC_DEVICE = "/dev/cec1"
JELLYFIN_API_KEY = "da2d114027b54d8abc4b9b38cb3faac4"
JELLYFIN_USER_ID = "6a88eece9ca74c3db6f52ff5d1850111"

VLC_HTTP_PORT = 8080
VLC_HTTP_PASSWORD = "flix"

# NFC UID -> Jellyfin item ID mapping
UID_MAP = {
    "04:36:41:2f:4e:61:80": "0367258676e12c89b1ca3d407b6f1b56",
    "04:2e:64:76:1e:61:81": "c8c2d18a2ce6c9c8090937f0f4ba2db6",
    "04:fc:c8:3f:4e:61:80": "eba4cbb385b261f1156e8b07a0dd26af",
    "04:a1:09:75:1e:61:80": "357f19ef861a93f671463203ead75d5c",
    "04:41:19:7c:1e:61:81": "e85cf0e786f4c58fcaa22ecc77ee134a",
}

# Track the current VLC process and what's playing
vlc_process = None
current_item_id = None

# Long-running cec-client for both sending commands and receiving remote key events
cec_process = None
cec_stdin_lock = threading.Lock()
cec_ready = threading.Event()

# CEC opcodes and user-control key codes we care about
CEC_OP_STANDBY = 0x36
CEC_OP_USER_CONTROL_PRESSED = 0x44

CEC_KEY_PLAY = 0x44
CEC_KEY_STOP = 0x45
CEC_KEY_PAUSE = 0x46
CEC_KEY_REWIND = 0x48
CEC_KEY_FAST_FORWARD = 0x49
CEC_KEY_PLAY_FUNCTION = 0x60
CEC_KEY_PAUSE_PLAY_FUNCTION = 0x61
CEC_KEY_STOP_FUNCTION = 0x64

# Matches cec-client TRAFFIC lines like: ">> 0f:36" or ">> 0f:44:44"
TRAFFIC_RE = re.compile(r">>\s+([0-9a-f]{2}):([0-9a-f]{2})(?::([0-9a-f]{2}))?")


def cec_start():
    """Start the long-running cec-client subprocess and reader thread."""
    global cec_process
    cec_ready.clear()
    cec_process = subprocess.Popen(
        ["stdbuf", "-oL", "cec-client", "-d", "8", CEC_DEVICE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    threading.Thread(target=cec_reader, daemon=True).start()
    # Wait for cec-client to complete its bus handshake before accepting commands
    if not cec_ready.wait(timeout=15):
        logging.warning("cec-client did not signal ready within 15s — commands may fail")
    else:
        logging.info("cec-client ready")


def cec_ensure_alive():
    """Restart cec-client if it has exited."""
    if cec_process and cec_process.poll() is None:
        return
    logging.warning("cec-client is dead — restarting")
    cec_start()


def cec_send(command):
    """Send a command line to the running cec-client, restarting it if necessary."""
    cec_ensure_alive()
    with cec_stdin_lock:
        try:
            cec_process.stdin.write(command + "\n")
            cec_process.stdin.flush()
        except Exception:
            logging.exception("Failed to send CEC command: %s", command)


def cec_tv_on():
    """Turn on the TV and switch to this Pi's HDMI input."""
    cec_send("on 0")
    time.sleep(3)
    cec_send("as")


def cec_reader():
    """Parse cec-client output for remote key events and TV standby broadcasts."""
    try:
        for line in cec_process.stdout:
            # cec-client prints "waiting for input" once the bus handshake is done
            if not cec_ready.is_set() and "waiting for input" in line:
                cec_ready.set()
            m = TRAFFIC_RE.search(line)
            if not m:
                continue
            addr = int(m.group(1), 16)
            opcode = int(m.group(2), 16)
            operand = int(m.group(3), 16) if m.group(3) else None
            src = addr >> 4

            if opcode == CEC_OP_STANDBY and src == 0:
                # TV is going to standby — stop playback
                kill_vlc()
            elif opcode == CEC_OP_USER_CONTROL_PRESSED and operand is not None:
                handle_remote_key(operand)
    except Exception:
        logging.exception("cec_reader crashed")
    finally:
        logging.warning("cec_reader exited")


def handle_remote_key(code):
    """Map a CEC user-control key code to a VLC action."""
    if code in (
        CEC_KEY_PLAY,
        CEC_KEY_PAUSE,
        CEC_KEY_PLAY_FUNCTION,
        CEC_KEY_PAUSE_PLAY_FUNCTION,
    ):
        vlc_command("pl_pause")
    elif code in (CEC_KEY_STOP, CEC_KEY_STOP_FUNCTION):
        kill_vlc()
    elif code == CEC_KEY_FAST_FORWARD:
        vlc_command("seek", "+30")
    elif code == CEC_KEY_REWIND:
        vlc_command("seek", "-10")


def vlc_command(command, val=None):
    """Send a command to VLC via its HTTP interface."""
    url = f"http://127.0.0.1:{VLC_HTTP_PORT}/requests/status.xml?command={command}"
    if val is not None:
        url += f"&val={urllib.parse.quote(val)}"
    auth = base64.b64encode(f":{VLC_HTTP_PASSWORD}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def kill_vlc():
    global vlc_process, current_item_id
    if vlc_process and vlc_process.poll() is None:
        vlc_process.terminate()
        try:
            vlc_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            vlc_process.kill()
    vlc_process = None
    current_item_id = None
    # Kill any orphaned VLC processes from previous sessions
    subprocess.run(["pkill", "-f", "/usr/bin/vlc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@app.route("/play", methods=["POST"])
def play():
    global vlc_process, current_item_id

    data = request.get_json()
    uid = data.get("uid", "").strip()

    item_id = UID_MAP.get(uid)
    if not item_id:
        return jsonify({"error": f"Unknown UID: {uid}"}), 404

    # Ignore if this item is already playing
    if item_id == current_item_id and vlc_process and vlc_process.poll() is None:
        return jsonify({"status": "already_playing", "item_id": item_id})

    # Build the direct stream URL
    stream_url = (
        f"{JELLYFIN_URL}/Videos/{item_id}/stream"
        f"?Static=true&api_key={JELLYFIN_API_KEY}"
    )

    # Kill any existing playback before turning on the TV
    kill_vlc()

    # Turn on the TV and switch to this input
    cec_tv_on()

    # Launch VLC fullscreen on the Wayland display
    env = {
        "WAYLAND_DISPLAY": "wayland-0",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    vlc_process = subprocess.Popen(
        [
            "cvlc",
            "--fullscreen",
            "--no-video-title-show",
            "--aout=alsa",
            "--alsa-audio-device=plughw:1",
            "--extraintf", "http",
            "--http-host", "127.0.0.1",
            "--http-port", str(VLC_HTTP_PORT),
            "--http-password", VLC_HTTP_PASSWORD,
            stream_url,
            "vlc://quit",  # quit VLC when playback ends
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    current_item_id = item_id

    # Re-assert active source after the TV has had more time to finish its
    # own startup/CEC negotiation — avoids the TV briefly switching away
    # from this input on cold start.
    def deferred_active_source():
        time.sleep(5)
        cec_send("as")

    threading.Thread(target=deferred_active_source, daemon=True).start()

    return jsonify({"status": "playing", "item_id": item_id, "pid": vlc_process.pid})


@app.route("/stop", methods=["POST"])
def stop():
    kill_vlc()
    return jsonify({"status": "stopped"})


if __name__ == "__main__":
    # Kill any orphaned VLC from previous sessions on startup
    subprocess.run(["pkill", "-f", "/usr/bin/vlc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Start the long-running cec-client (handles commands + remote key events + TV-off detection)
    cec_start()
    app.run(host="0.0.0.0", port=5000)
