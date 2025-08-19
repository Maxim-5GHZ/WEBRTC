import os
import json
import uuid
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
import logging
import socket
import ssl
from typing import Dict
from pathlib import Path

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="WebRTC Voice Chat")


def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


local_ip = get_local_ip()
logger.info(f"Локальный IP-адрес: {local_ip}")


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.lock = asyncio.Lock()
        logger.info("Менеджер подключений инициализирован")

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        async with self.lock:
            self.active_connections[client_id] = websocket
            logger.info(f"Клиент {client_id} подключен")

        # Отправляем текущему клиенту список всех пользователей
        await self.send_user_list(websocket)
        # Уведомляем всех о новом пользователе
        await self.broadcast_user_list()

    async def disconnect(self, client_id: str):
        async with self.lock:
            if client_id in self.active_connections:
                del self.active_connections[client_id]
                logger.info(f"Клиент {client_id} отключен")

        # Уведомляем всех оставшихся пользователей об изменении списка
        await self.broadcast_user_list()

    async def send_user_list(self, websocket: WebSocket):
        """Отправляет текущему клиенту список всех пользователей"""
        async with self.lock:
            users = list(self.active_connections.keys())
        message = {"type": "users", "users": users}
        try:
            await websocket.send_json(message)
            logger.info(f"Отправлен список пользователей: {users}")
        except Exception as e:
            logger.error(f"Ошибка отправки списка пользователей: {str(e)}")

    async def broadcast_user_list(self):
        """Рассылает всем клиентам актуальный список пользователей"""
        async with self.lock:
            users = list(self.active_connections.keys())
            connections = list(self.active_connections.items())

        message = {"type": "users", "users": users}
        disconnected_clients = []

        for client_id, connection in connections:
            try:
                await connection.send_json(message)
                logger.debug(f"Список пользователей отправлен {client_id}")
            except Exception as e:
                logger.error(f"Ошибка отправки списка пользователей {client_id}: {str(e)}")
                disconnected_clients.append(client_id)

        # Удаляем отключенных клиентов
        if disconnected_clients:
            async with self.lock:
                for client_id in disconnected_clients:
                    if client_id in self.active_connections:
                        del self.active_connections[client_id]

    async def send_personal_message(self, message: dict, client_id: str):
        async with self.lock:
            connection = self.active_connections.get(client_id)
        if connection:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения {client_id}: {str(e)}")
                await self.disconnect(client_id)


manager = ConnectionManager()

# Получение абсолютного пути к client.html
current_dir = Path(__file__).parent
client_html_path = current_dir / "client.html"


@app.get("/", response_class=HTMLResponse)
async def home():
    """Главная страница, возвращает HTML клиента"""
    if not client_html_path.exists():
        logger.error(f"Файл не найден: {client_html_path}")
        return HTMLResponse(content="<h1>Файл клиента не найден</h1>", status_code=404)

    logger.info("Отправка client.html")
    return FileResponse(client_html_path)


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket endpoint для обработки соединений"""
    logger.info(f"Попытка подключения WebSocket от {client_id}")

    try:
        await manager.connect(websocket, client_id)

        while True:
            try:
                data = await websocket.receive_text()
                logger.debug(f"Получено от {client_id}: {data[:100]}...")

                try:
                    message = json.loads(data)
                    # Пересылаем сообщение целевому пользователю
                    if "target" in message:
                        await manager.send_personal_message(message, message["target"])
                    # Обработка запроса списка пользователей
                    elif message.get("type") == "get_users":
                        await manager.send_user_list(websocket)
                except json.JSONDecodeError:
                    logger.warning(f"Неверный JSON от {client_id}")
            except WebSocketDisconnect:
                logger.info(f"Клиент {client_id} отключился")
                break
            except Exception as e:
                logger.error(f"Ошибка обработки сообщения от {client_id}: {str(e)}")
                break
    except Exception as e:
        logger.error(f"Ошибка WebSocket для {client_id}: {str(e)}")
    finally:
        await manager.disconnect(client_id)
        logger.info(f"WebSocket закрыт для {client_id}")


@app.get("/generate_id")
async def generate_id():
    """Генерирует уникальный ID для клиента"""
    new_id = str(uuid.uuid4())[:8]
    logger.info(f"Сгенерирован новый ID: {new_id}")
    return {"id": new_id}


@app.get("/health")
async def health_check():
    """Проверка здоровья сервера"""
    return {"status": "ok", "connections": len(manager.active_connections)}


def generate_self_signed_cert():
    """Генерация самоподписанного SSL сертификата"""
    import subprocess

    keyfile = "key.pem"
    certfile = "cert.pem"

    if not os.path.exists(keyfile) or not os.path.exists(certfile):
        logger.info("Генерация самоподписанного SSL сертификата...")

        # Создаем команду для генерации сертификата
        cmd = [
            "openssl", "req", "-x509", "-newkey", "rsa:4096",
            "-keyout", keyfile, "-out", certfile, "-days", "365",
            "-nodes", "-subj", f"/CN={local_ip}"
        ]

        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            logger.info("SSL сертификат успешно сгенерирован")
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error("Не удалось сгенерировать SSL сертификат. Убедитесь, что OpenSSL установлен")
            return None, None

    return keyfile, certfile


if __name__ == "__main__":
    import uvicorn

    # Генерация SSL сертификатов
    ssl_keyfile, ssl_certfile = generate_self_signed_cert()

    if ssl_keyfile and ssl_certfile:
        logger.info("Запуск HTTPS сервера...")
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=8000,
            ssl_keyfile=ssl_keyfile,
            ssl_certfile=ssl_certfile,
            log_level="info",
            timeout_keep_alive=60
        )
    else:
        logger.warning("Запуск HTTP сервера (без SSL)...")
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=8000,
            log_level="info",
            timeout_keep_alive=60
        )