import os
from flask import Flask, request, jsonify
import json

app = Flask(__name__)
logs = []

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    logs.append(data)
    with open("webhook_logs.json", "w") as f:
        json.dump(logs, f, indent=2)
    return jsonify({"status": "success"}), 200

@app.route("/webhook/logs", methods=["GET"])
def get_logs():
    try:
        with open("webhook_logs.json", "r") as f:
            content = f.read()
        return content, 200, {"Content-Type": "application/json"}
    except FileNotFoundError:
        return jsonify([]), 200

@app.route("/", methods=["GET"])
def home():
    return "Flask app is running.", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
