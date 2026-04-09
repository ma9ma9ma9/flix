import subprocess
import signal
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

JELLYFIN_URL = "http://192.168.50.13:8096"
JELLYFIN_API_KEY = "da2d114027b54d8abc4b9b38cb3faac4"
JELLYFIN_USER_ID = "6a88eece9ca74c3db6f52ff5d1850111"

# NFC UID -> Jellyfin item ID mapping
UID_MAP = {
    "04:36:41:2f:4e:61:80": "0367258676e12c89b1ca3d407b6f1b56",
}

# Track the current VLC process so we can stop it
vlc_process = None


def kill_vlc():
    global vlc_process
    if vlc_process and vlc_process.poll() is None:
        vlc_process.terminate()
        vlc_process.wait(timeout=5)
    vlc_process = None


@app.route("/play", methods=["POST"])
def play():
    global vlc_process

    data = request.get_json()
    uid = data.get("uid", "").strip()

    item_id = UID_MAP.get(uid)
    if not item_id:
        return jsonify({"error": f"Unknown UID: {uid}"}), 404

    # Build the direct stream URL
    stream_url = (
        f"{JELLYFIN_URL}/Videos/{item_id}/stream"
        f"?Static=true&api_key={JELLYFIN_API_KEY}"
    )

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

    return jsonify({"status": "playing", "item_id": item_id, "pid": vlc_process.pid})


@app.route("/stop", methods=["POST"])
def stop():
    kill_vlc()
    return jsonify({"status": "stopped"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
