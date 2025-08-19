import os
import json
import uuid
import asyncio
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
import logging
import socket
import ssl


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


local_ip = get_local_ip()
print(f"Ваш локальный IP-адрес: {local_ip}")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()


class ConnectionManager:
    def __init__(self):
        self.active_connections = {}
        self.lock = asyncio.Lock()
        logger.info("Connection manager initialized")

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        async with self.lock:
            self.active_connections[client_id] = websocket
            logger.info(f"Client {client_id} connected")
            await self.notify_users()

    async def disconnect(self, client_id: str):
        async with self.lock:
            if client_id in self.active_connections:
                del self.active_connections[client_id]
                logger.info(f"Client {client_id} disconnected")
                await self.notify_users()

    async def send_message(self, message: dict, client_id: str):
        if client_id in self.active_connections:
            try:
                await self.active_connections[client_id].send_json(message)
            except Exception as e:
                logger.error(f"Error sending to {client_id}: {str(e)}")
                await self.disconnect(client_id)

    async def notify_users(self):
        users = list(self.active_connections.keys())
        message = {"type": "users", "users": users}
        for client_id in list(self.active_connections.keys()):
            await self.send_message(message, client_id)


manager = ConnectionManager()

# Получение абсолютного пути к client.html
current_dir = os.path.dirname(os.path.abspath(__file__))
client_html_path = os.path.join(current_dir, "client.html")


@app.get("/")
async def home():
    """Главная страница, возвращает HTML клиента"""
    if not os.path.exists(client_html_path):
        logger.error(f"File not found: {client_html_path}")
        return {"error": "Client file not found"}, 404

    logger.info("Serving client.html")
    return FileResponse(client_html_path)


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket endpoint для обработки соединений"""
    logger.info(f"WebSocket connection attempt from {client_id}")
    try:
        await manager.connect(websocket, client_id)
        while True:
            try:
                data = await websocket.receive_text()
                logger.debug(f"Received from {client_id}: {data[:50]}...")
                try:
                    message = json.loads(data)
                    if "target" in message:
                        await manager.send_message(message, message["target"])
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON from {client_id}")
            except Exception as e:
                logger.error(f"Error handling message from {client_id}: {str(e)}")
                break
    except Exception as e:
        logger.error(f"WebSocket error for {client_id}: {str(e)}")
    finally:
        await manager.disconnect(client_id)
        logger.info(f"WebSocket closed for {client_id}")


@app.get("/generate_id")
async def generate_id():
    """Генерирует уникальный ID для клиента"""
    new_id = str(uuid.uuid4())[:8]
    logger.info(f"Generated new ID: {new_id}")
    return {"id": new_id}


if __name__ == "__main__":
    import uvicorn

    # Пути к SSL-сертификатам (создайте их заранее)
    ssl_keyfile = "key.pem"
    ssl_certfile = "cert.pem"

    # Проверяем существование SSL-файлов
    if not os.path.exists(ssl_keyfile) or not os.path.exists(ssl_certfile):
        logger.error("SSL files not found! Generating self-signed certificates...")
        # Генерируем самоподписанный сертификат
        os.system(
            f"openssl req -x509 -newkey rsa:4096 -keyout {ssl_keyfile} -out {ssl_certfile} -days 365 -nodes -subj '/CN=localhost'")

    # Конфигурация SSL
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=ssl_certfile, keyfile=ssl_keyfile)

    logger.info("Starting HTTPS server...")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
        log_level="info",
        timeout_keep_alive=60
    )