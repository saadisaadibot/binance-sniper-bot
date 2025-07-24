from flask import Flask, request

app = Flask(__name__)

@app.route('/', methods=['POST'])
def telegram_webhook():
    data = request.json
    chat_id = data['message']['chat']['id']
    message = data['message']['text']
    print(f"📩 Received message: {message}")
    print(f"💬 Chat ID: {chat_id}")
    return "ok"

if __name__ == '__main__':
    app.run()