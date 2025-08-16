from flask import Flask
app = Flask(__name__)

@app.get("/")
def ok():
    return "OK", 200
