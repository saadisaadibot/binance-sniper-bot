from flask import Flask, request
import os

app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    data = request.get_json()
    print("🚀 وصلك شي من Telegram:")
    print(data)  # طباعة البيانات اللي وصلت
    return '', 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)