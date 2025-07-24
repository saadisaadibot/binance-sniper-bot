from flask import Flask, request
import os

app = Flask(__name__)

@app.route('/', methods=['POST'])
def telegram_webhook():
    data = request.json
    if data.get("message"):
        chat_id = data['message']['chat']['id']
        message = data['message'].get('text', '')
        print(f"💬 Received message: {message}")
        print(f"👤 Chat ID: {chat_id}")
    return "ok"

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)