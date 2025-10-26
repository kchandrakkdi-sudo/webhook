import os
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- CONFIG: List your sub-endpoints here ---
WEBHOOKS = [
    "webhook1",
    "webhook2",
    # Add more like "webhook3"
]

DOWNLOAD_SECRET = "my_download_secret_9876"  # Change to a strong secret

# --- Helper: Purge log entries older than 7 days ---
def purge_old_entries(logfile):
    if not os.path.exists(logfile):
        return
    now = datetime.utcnow()
    new_logs = []
    with open(logfile, "r") as f:
        for line in f:
            try:
                entry = json.loads(line)
                ts_str = entry.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except ValueError:
                        ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
                else:
                    ts = now  # Keep if no timestamp found
                if (now - ts).days <= 7:
                    new_logs.append(entry)
            except Exception:
                continue
    with open(logfile, "w") as f:
        for entry in new_logs:
            f.write(json.dumps(entry) + "\n")

# --- Dynamic endpoints creation ---
for webhook_name in WEBHOOKS:
    log_file = f"{webhook_name}_logs.json"

    def make_post_handler(logfile):
        def handler():
            data = request.get_json(force=True)
            # Attach standardized UTC ISO timestamp
            data["timestamp"] = datetime.utcnow().isoformat()
            purge_old_entries(logfile)
            with open(logfile, "a") as f:
                f.write(json.dumps(data) + "\n")
            return "", 200
        return handler

    def make_get_handler(logfile):
        def handler():
            # Security: Require ?token=DOWNLOAD_SECRET
            token = request.args.get("token", None)
            if token != DOWNLOAD_SECRET:
                return jsonify({"error": "Unauthorized"}), 401
            purge_old_entries(logfile)
            if not os.path.exists(logfile):
                return jsonify([]), 200
            with open(logfile, "r") as f:
                lines = f.readlines()
            logs = [json.loads(line) for line in lines if line.strip()]
            return jsonify(logs), 200
        return handler

    def make_clear_handler(logfile):
        def handler():
            # Security: Require ?token=DOWNLOAD_SECRET
            token = request.args.get("token", None)
            if token != DOWNLOAD_SECRET:
                return jsonify({"error": "Unauthorized"}), 401
            open(logfile, "w").close()
            return jsonify({"status": f"{logfile} cleared"}), 200
        return handler

    app.add_url_rule(f"/{webhook_name}", f"post_{webhook_name}", make_post_handler(log_file), methods=["POST"])
    app.add_url_rule(f"/{webhook_name}/logs", f"getlogs_{webhook_name}", make_get_handler(log_file), methods=["GET"])
    app.add_url_rule(f"/{webhook_name}/clearlogs", f"clearlogs_{webhook_name}", make_clear_handler(log_file), methods=["POST"])

@app.route("/", methods=["GET"])
def home():
    #return "Flask app is running. Valid endpoints: " + ", ".join([f"/{w}" for w in WEBHOOKS]), 200
    return "Flask app is running" , 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

