from flask import Flask, request
import requests
import os

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    print("Received:", data)

    if "message" in data and "text" in data["message"]:
        msg = data["message"]["text"]
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", 
                      data={"chat_id": CHAT_ID, "text": f"وصلك: {msg}"})
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)