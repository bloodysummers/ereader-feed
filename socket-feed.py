import websocket
import json
import time

def on_message(ws, message):
    print(f"Servidor: {message}")

def on_open(ws):
    print("Conectado. Ahora prueba subir un archivo desde la interfaz web...")
    
ws = websocket.WebSocketApp("ws://crosspoint.local:81/",
                           on_open=on_open,
                           on_message=on_message)
ws.run_forever()