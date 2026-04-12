import subprocess
import threading
import time
from flask import Flask, request, jsonify

app = Flask(__name__)

JELLYFIN_URL = "http://192.168.50.13:8096"
CEC_DEVICE = "/dev/cec1"
JELLYFIN_API_KEY = "da2d114027b54d8abc4b9b38cb3faac4"
JELLYFIN_USER_ID = "6a88eece9ca74c3db6f52ff5d1850111"

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


def cec_tv_on():
    """Turn on the TV and switch to this Pi's HDMI input."""
    subprocess.run(
        ["cec-client", "-s", "-d", "1", CEC_DEVICE],
        input="on 0\n",
        text=True,
        timeout=10,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["cec-client", "-s", "-d", "1", CEC_DEVICE],
        input="as\n",
        text=True,
        timeout=10,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cec_tv_power_status():
    """Return the TV power status string, e.g. 'on', 'standby'."""
    result = subprocess.run(
        ["cec-client", "-s", "-d", "1", CEC_DEVICE],
        input="pow 0\n",
        text=True,
        timeout=10,
        capture_output=True,
    )
    for line in result.stdout.splitlines():
        if "power status:" in line:
            return line.split("power status:")[-1].strip()
    return "unknown"


def kill_vlc():
    global vlc_process, current_item_id
    if vlc_process and vlc_process.poll() is None:
        vlc_process.terminate()
        vlc_process.wait(timeout=5)
    vlc_process = None
    current_item_id = None
    # Kill any orphaned VLC processes from previous sessions
    subprocess.run(["pkill", "-f", "/usr/bin/vlc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cec_monitor():
    """Background thread: poll TV power status and stop VLC if TV turns off."""
    while True:
        time.sleep(5)
        if vlc_process and vlc_process.poll() is None:
            status = cec_tv_power_status()
            if status != "on":
                kill_vlc()


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

    # Turn on the TV and switch to this input
    cec_tv_on()

    # Kill any existing playback
    kill_vlc()

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
            stream_url,
            "vlc://quit",  # quit VLC when playback ends
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    current_item_id = item_id

    return jsonify({"status": "playing", "item_id": item_id, "pid": vlc_process.pid})


@app.route("/stop", methods=["POST"])
def stop():
    kill_vlc()
    return jsonify({"status": "stopped"})


if __name__ == "__main__":
    # Kill any orphaned VLC from previous sessions on startup
    subprocess.run(["pkill", "-f", "/usr/bin/vlc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Start CEC monitor thread
    threading.Thread(target=cec_monitor, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
