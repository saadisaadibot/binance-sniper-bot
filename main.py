import os
import requests
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    requests.post(url, json=payload)

@app.route('/', methods=['POST'])
def telegram_webhook():
    data = request.json
    chat_id = data['message']['chat']['id']
    message = data['message']['text']
    print(f"📩 Received message: {message}")
    print(f"💬 Chat ID: {chat_id}")
    send_message(chat_id, f"📬 وصلتني رسالتك: {message}")
    return "ok"

if __name__ == '__main__':
    app.run()