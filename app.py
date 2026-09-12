import ctypes
from datetime import datetime
import json
import os
import queue
import random
import re
import sys
import threading
import time
import webbrowser
import winreg
import winsound
from tkinter import filedialog, messagebox

import customtkinter as ctk
import numpy as np
from PIL import Image, ImageDraw, ImageGrab
import psutil
import pystray
import pyautogui
import pygetwindow as gw
import pyperclip
import pyttsx3
from rapidfuzz import fuzz
import sounddevice as sd
from vosk import KaldiRecognizer, Model

pyautogui.FAILSAFE = False

# --- ПУТИ К ФАЙЛАМ ---
if getattr(sys, 'frozen', False):
    EXE_DIR = os.path.dirname(sys.executable)
    INTERNAL_DIR = getattr(sys, '_MEIPASS', EXE_DIR)
else:
    EXE_DIR = os.path.dirname(os.path.abspath(__file__))
    INTERNAL_DIR = EXE_DIR

CONFIG_FILE = os.path.join(EXE_DIR, "commands.json")
SCREENSHOTS_DIR = os.path.join(EXE_DIR, "Screenshots")
REG_AUTORUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_REG_NAME = "VasyaAssistant"

AUDIO_QUEUE = queue.Queue()
TTS_QUEUE = queue.Queue()
GUI_APP = None
TRAY_ICON = None

STOP_AUDIO_EVENT = threading.Event()
AUDIO_THREAD = None
VOICE_THREAD = None
TTS_THREAD = None

# --- КОДЫ МЕДИА-КЛАВИШ WINDOWS ---
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF

def press_media_key(vk_code: int, repeat: int = 1):
    for _ in range(repeat):
        ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
        time.sleep(0.04)
        ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)
        time.sleep(0.03)

# --- АВТОЗАПУСК WINDOWS (РЕЕСТР) ---
def is_autostart_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_AUTORUN_PATH, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_REG_NAME)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False

def set_autostart(enable: bool):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_AUTORUN_PATH, 0, winreg.KEY_SET_VALUE)
        if enable:
            if getattr(sys, 'frozen', False):
                cmd = f'"{sys.executable}"'
            else:
                cmd = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
            winreg.SetValueEx(key, APP_REG_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_REG_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[Ошибка реестра]: {e}")

# --- ФОНОВЫЙ СИНТЕЗАТОР РЕЧИ (TTS) ---
def tts_worker():
    try:
        engine = pyttsx3.init()
    except Exception as e:
        print(f"[Ошибка инициализации TTS]: {e}")
        return

    while True:
        item = TTS_QUEUE.get()
        if item is None:
            break
        text, voice_id, volume, rate = item
        try:
            if voice_id:
                engine.setProperty('voice', voice_id)
            engine.setProperty('volume', max(0.0, min(1.0, volume)))
            engine.setProperty('rate', int(rate))
            engine.say(text)
            engine.runAndWait()
        except Exception as e:
            print(f"[Ошибка TTS]: {e}")
        finally:
            TTS_QUEUE.task_done()

def speak_text(text: str):
    """Поставить фразу в фоновую очередь озвучки."""
    if not text or not GUI_APP:
        return
    cfg = GUI_APP.config
    mode = cfg.get("feedback_mode", "both")
    if mode in ["voice", "both"]:
        voice_id = cfg.get("voice_id")
        volume = cfg.get("voice_volume", 80) / 100.0
        rate = cfg.get("voice_rate", 185)
        TTS_QUEUE.put((text, voice_id, volume, rate))

def play_sound(sound_type="wake"):
    """Звуковые сигналы бипера."""
    if GUI_APP and GUI_APP.config.get("feedback_mode") == "voice":
        return

    def _play():
        try:
            if sound_type == "wake":
                winsound.Beep(750, 70)
            elif sound_type == "success":
                winsound.Beep(800, 45)
                time.sleep(0.04)
                winsound.Beep(1000, 55)
            elif sound_type == "blocked":
                winsound.Beep(450, 90)
                time.sleep(0.03)
                winsound.Beep(350, 110)
            elif sound_type == "coin":
                # Звон подброшенной монетки
                winsound.Beep(1400, 30)
                winsound.Beep(1750, 35)
                winsound.Beep(2100, 40)
        except Exception:
            pass
    threading.Thread(target=_play, daemon=True).start()

def notify_feedback(sound_type: str, voice_text: str = ""):
    """Унифицированный ответ ассистента."""
    play_sound(sound_type)
    if voice_text:
        speak_text(voice_text)

# --- БЕЗОПАСНЫЙ ПУТЬ WINDOWS ---
def get_safe_win_path(path: str) -> str:
    if sys.platform != "win32":
        return path
    buf = ctypes.create_unicode_buffer(1024)
    res = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
    return buf.value if res > 0 else path

def find_valid_model_dir() -> str | None:
    candidate_dirs = [
        os.path.join(INTERNAL_DIR, "model"),
        os.path.join(INTERNAL_DIR, "vosk-model-small-ru-0.22"),
        os.path.join(EXE_DIR, "model"),
        os.path.join(EXE_DIR, "vosk-model-small-ru-0.22"),
    ]
    for search_root in [INTERNAL_DIR, EXE_DIR]:
        try:
            for item in os.listdir(search_root):
                p = os.path.join(search_root, item)
                if os.path.isdir(p) and "vosk" in item.lower():
                    candidate_dirs.append(p)
        except Exception:
            pass

    for candidate in candidate_dirs:
        if not os.path.isdir(candidate):
            continue
        if os.path.exists(os.path.join(candidate, "am")) or os.path.exists(os.path.join(candidate, "conf")):
            return get_safe_win_path(candidate)
        try:
            for sub in os.listdir(candidate):
                sub_path = os.path.join(candidate, sub)
                if os.path.isdir(sub_path) and (os.path.exists(os.path.join(sub_path, "am")) or os.path.exists(os.path.join(sub_path, "conf"))):
                    return get_safe_win_path(sub_path)
        except Exception:
            pass
    return None

# --- СКРИНШОТ ---
def take_screenshot():
    try:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filepath = os.path.join(SCREENSHOTS_DIR, f"screenshot_{timestamp}.png")
        img = ImageGrab.grab(all_screens=True)
        img.save(filepath, "PNG")
        print(f"[Скриншот] Сохранен: {filepath}")
        notify_feedback("success", "Скриншот готов")
        return True
    except Exception as e:
        print(f"[Ошибка скриншота]: {e}")
        return False

# --- ХРАНЕНИЕ НАСТРОЕК ---
def load_config():
    default_cfg = {
        "device_index": None,
        "feedback_mode": "both",    # "sound", "voice", "both"
        "voice_id": None,
        "voice_volume": 85,         # 0 - 100
        "voice_rate": 190,          # wpm
        "commands": [
            {"aliases": ["кс", "cs", "контра"], "target": "steam://rungameid/730", "is_game": True, "process_name": "cs2.exe"},
            {"aliases": ["ютуб", "youtube"], "target": "https://www.youtube.com", "is_game": False, "process_name": ""}
        ],
        "plans": [
            {
                "name": "Дискорд с запретом",
                "aliases": ["дискорд", "дс", "войс"],
                "steps": ["C:\\zapret\\discord.bat", "https://discord.com/app"]
            }
        ],
        "contacts": [
            {"aliases": ["андрею", "андрей", "дрон"], "discord_tag": "Andrey"}
        ],
        "constructors": [
            {
                "name": "Игровой режим",
                "aliases": ["режим катки", "погнали играть", "время играть"],
                "steps": [
                    {"type": "music", "val": "pause"},
                    {"type": "plan", "val": "Дискорд с запретом"},
                    {"type": "app", "val": "steam://rungameid/730"}
                ]
            }
        ]
    }
    if not os.path.exists(CONFIG_FILE):
        save_config(default_cfg)
        return default_cfg
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in default_cfg.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception:
        return default_cfg

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# --- ПРОВЕРКА АКТИВНОЙ ИГРЫ ---
def is_game_running(cfg: dict) -> tuple[bool, str]:
    game_processes = set()
    for item in cfg.get("commands", []):
        if item.get("is_game", False):
            proc = item.get("process_name", "").lower().strip()
            if proc:
                game_processes.add(proc)

    if not game_processes:
        return False, ""

    try:
        for p in psutil.process_iter(['name']):
            name = (p.info['name'] or '').lower()
            if name in game_processes:
                return True, name
    except Exception:
        pass
    return False, ""

# --- ИСПОЛНЕНИЕ ДЕЙСТВИЙ ---
def execute_target(target: str, feedback: bool = True):
    try:
        if target.startswith(("steam://", "http://", "https://")):
            webbrowser.open(target)
        else:
            os.startfile(target)
        if feedback:
            notify_feedback("success", "Запустил")
        return True
    except Exception as e:
        print(f"[Ошибка запуска {target}]: {e}")
        return False

def execute_plan(steps: list, feedback_text="Сценарий выполнен"):
    def _run():
        for idx, step_target in enumerate(steps):
            if not step_target:
                continue
            execute_target(step_target, feedback=False)
            if idx < len(steps) - 1:
                time.sleep(1.2)
        notify_feedback("success", feedback_text)
    threading.Thread(target=_run, daemon=True).start()

def execute_constructor(item: dict, cfg: dict):
    """Исполнение составного супер-макроса из Конструктора."""
    def _run():
        print(f"[Конструктор] Старт цепочки: {item.get('name')}")
        steps = item.get("steps", [])
        for step in steps:
            stype = step.get("type")
            val = step.get("val")

            if stype == "app":
                execute_target(val, feedback=False)
            elif stype == "plan":
                for pl in cfg.get("plans", []):
                    if pl.get("name") == val:
                        for s in pl.get("steps", []):
                            execute_target(s, feedback=False)
                            time.sleep(1.0)
                        break
            elif stype == "music":
                if val == "pause" or val == "play":
                    press_media_key(VK_MEDIA_PLAY_PAUSE)
                elif val == "next":
                    press_media_key(VK_MEDIA_NEXT_TRACK)
                elif val == "prev":
                    press_media_key(VK_MEDIA_PREV_TRACK)
                elif val == "vol_down":
                    press_media_key(VK_VOLUME_DOWN, repeat=4)
                elif val == "vol_up":
                    press_media_key(VK_VOLUME_UP, repeat=4)
                elif val == "mute":
                    press_media_key(VK_VOLUME_MUTE)

            time.sleep(1.0)

        notify_feedback("success", "Конструктор выполнен")
    threading.Thread(target=_run, daemon=True).start()

# --- ОТПРАВКА В DISCORD ---
def send_discord_message(discord_tag: str, text: str, cfg: dict):
    def _send():
        game_active, game_name = is_game_running(cfg)
        if game_active:
            print(f"[Блокировка] Игра {game_name} активна!")
            notify_feedback("blocked", "Отменено: игра уже запущена")
            if GUI_APP:
                GUI_APP.set_status("blocked")
            return

        try:
            windows = [w for w in gw.getAllWindows() if 'discord' in w.title.lower()]
            if not windows:
                webbrowser.open("discord://")
                time.sleep(0.8)
                windows = [w for w in gw.getAllWindows() if 'discord' in w.title.lower()]

            if not windows:
                notify_feedback("blocked", "Не нашел Дискорд")
                return

            win = windows[0]
            try:
                win.restore()
            except Exception:
                pass
            win.activate()
            time.sleep(0.35)

            pyautogui.hotkey('ctrl', 'k')
            time.sleep(0.35)

            pyperclip.copy(discord_tag)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.3)
            pyautogui.press('enter')
            time.sleep(0.3)

            pyperclip.copy(text)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.15)
            pyautogui.press('enter')

            notify_feedback("success", f"Написал {discord_tag}")
        except Exception as e:
            print(f"[Ошибка Discord]: {e}")
            notify_feedback("blocked", "Ошибка отправки")

    threading.Thread(target=_send, daemon=True).start()

# --- МУЗЫКАЛЬНЫЕ КОМАНДЫ ---
def handle_music_commands(phrase: str) -> bool:
    clean = phrase.lower().strip()

    # Пауза / Стоп
    if any(st in clean for st in ["музыка стоп", "стоп музыка", "музыка пауза", "останови музыку", "пауза"]):
        press_media_key(VK_MEDIA_PLAY_PAUSE)
        notify_feedback("success", "Пауза")
        return True

    # Плей / Продолжить
    if any(pt in clean for pt in ["музыка включить", "включи музыку", "музыка играть", "продолжи музыку"]):
        press_media_key(VK_MEDIA_PLAY_PAUSE)
        notify_feedback("success", "Музыка играет")
        return True

    # Далее
    if any(nt in clean for nt in ["музыка следующее", "следующий трек", "переключи трек", "трек дальше", "музыка дальше"]):
        press_media_key(VK_MEDIA_NEXT_TRACK)
        notify_feedback("success", "Следующий")
        return True

    # Назад
    if any(pr in clean for pr in ["музыка предыдущее", "предыдущий трек", "трек назад", "музыка назад"]):
        press_media_key(VK_MEDIA_PREV_TRACK)
        notify_feedback("success", "Предыдущий")
        return True

    # Громкость
    if any(v in clean for v in ["музыка громче", "звук громче", "сделай громче"]):
        press_media_key(VK_VOLUME_UP, repeat=4)
        notify_feedback("success", "Громче")
        return True

    if any(v in clean for v in ["музыка тише", "звук тише", "сделай тише"]):
        press_media_key(VK_VOLUME_DOWN, repeat=4)
        notify_feedback("success", "Тише")
        return True

    if any(v in clean for v in ["выключи звук", "звук мут", "заглуши звук"]):
        press_media_key(VK_VOLUME_MUTE)
        notify_feedback("success", "Звук заглушен")
        return True

    return False

# --- ОСОБЫЕ КОМАНДЫ (МОНЕТКА, ВРЕМЯ, КУБИК) ---
def handle_special_commands(phrase: str) -> bool:
    clean = phrase.lower().strip()

    # 1. Подброс монетки
    if any(c in clean for c in ["подбрось монетку", "брось монетку", "кинь монетку", "монетка", "орел или решка"]):
        notify_feedback("coin")
        time.sleep(0.4)
        res = random.choice(["Выпал орёл!", "Выпала решка!"])
        notify_feedback("success", res)
        return True

    # 2. Текущее время
    if any(t in clean for t in ["который час", "сколько времени", "точное время"]):
        now_str = datetime.now().strftime("%H:%M")
        notify_feedback("success", f"Сейчас {now_str}")
        return True

    # 3. Бросок кубика
    if any(d in clean for d in ["брось кубик", "кинь кубик", "брось кость"]):
        num = random.randint(1, 6)
        notify_feedback("success", f"Выпало {num}")
        return True

    return False

# --- АНАЛИЗАТОР ВСЕХ ГОЛОСОВЫХ КОМАНД ---
def check_and_execute(phrase: str, cfg: dict) -> bool:
    clean = phrase.lower().strip()
    words = clean.split()
    if not words:
        return False

    # 1. Скриншот
    if any(sc in clean for sc in ["скриншот", "скрин"]):
        return take_screenshot()

    # 2. Особые команды (Монетка, время и т.д.)
    if handle_special_commands(clean):
        return True

    # 3. Музыка
    if handle_music_commands(clean):
        return True

    # 4. Discord: "напиши [имя] [текст]"
    msg_triggers = ["напиши", "отправь", "скинь", "передай", "сообщи"]
    for i, w in enumerate(words):
        if w in msg_triggers and i + 1 < len(words):
            target_name = words[i + 1]
            for contact in cfg.get("contacts", []):
                for alias in contact.get("aliases", []):
                    if fuzz.ratio(target_name, alias) >= 78:
                        message_body = " ".join(words[i + 2:])
                        if message_body:
                            send_discord_message(contact["discord_tag"], message_body, cfg)
                            return True
                        return False

    # 5. Конструктор
    for cons in cfg.get("constructors", []):
        for alias in cons.get("aliases", []):
            if alias in clean or any(fuzz.ratio(w, alias) >= 82 for w in words):
                execute_constructor(cons, cfg)
                return True

    # 6. Планы
    for plan in cfg.get("plans", []):
        for alias in plan.get("aliases", []):
            if alias in clean or any(fuzz.ratio(w, alias) >= 82 for w in words):
                execute_plan(plan.get("steps", []))
                return True

    # 7. Одиночные команды
    for item in cfg.get("commands", []):
        target = item["target"]
        for alias in item.get("aliases", []):
            if alias in clean or any(fuzz.ratio(w, alias) >= 82 for w in words):
                return execute_target(target)

    return False

# --- АУДИО И РАСПОЗНАВАНИЕ VOSK ---
def audio_capture_worker(device_idx):
    try:
        device_info = sd.query_devices(device_idx, 'input')
        samplerate = int(device_info['default_samplerate'])
    except Exception as e:
        print(f"[Ошибка микрофона #{device_idx}]: {e}")
        if GUI_APP:
            GUI_APP.set_status("error_mic")
        return

    def _callback(indata, frames, time_info, status):
        audio_data = np.frombuffer(indata, dtype=np.int16)
        if len(audio_data) > 0:
            rms = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
            vol = min(1.0, float(rms) / 2000.0)
        else:
            vol = 0.0

        if GUI_APP:
            GUI_APP.update_meter(vol)

        AUDIO_QUEUE.put((bytes(indata), samplerate))

    try:
        with sd.RawInputStream(samplerate=samplerate, blocksize=4000, device=device_idx,
                               dtype='int16', channels=1, callback=_callback):
            if GUI_APP:
                GUI_APP.set_status("idle")
            while not STOP_AUDIO_EVENT.is_set():
                time.sleep(0.1)
    except Exception as e:
        print(f"[Ошибка аудио]: {e}")
        if GUI_APP:
            GUI_APP.set_status("error_mic")

def voice_recognizer_worker():
    model_dir = find_valid_model_dir()
    if not model_dir:
        if GUI_APP:
            GUI_APP.set_status("error_model")
        return

    try:
        model = Model(model_dir)
    except Exception as e:
        print(f"[Сбой Vosk]: {e}")
        if GUI_APP:
            GUI_APP.set_status("error_model")
        return

    recognizer = None
    current_samplerate = None
    is_active = False
    active_until = 0.0

    while not STOP_AUDIO_EVENT.is_set():
        try:
            data, samplerate = AUDIO_QUEUE.get(timeout=0.2)
        except queue.Empty:
            continue

        if recognizer is None or current_samplerate != samplerate:
            current_samplerate = samplerate
            recognizer = KaldiRecognizer(model, float(current_samplerate))

        if is_active and time.time() > active_until:
            is_active = False
            if GUI_APP:
                GUI_APP.set_status("idle")

        if recognizer.AcceptWaveform(data):
            res = json.loads(recognizer.Result())
            text = res.get("text", "").strip()
            if not text:
                continue

            print(f"[Услышал]: {text}")
            words = text.split()
            cfg = load_config()
            clean_words = [re.sub(r'(.)\1+', r'\1', w) for w in words]

            if not is_active:
                for i, w in enumerate(clean_words):
                    if w in ["вас", "вася", "вась", "василий"] or fuzz.ratio(w, "вась") >= 80:
                        is_active = True
                        active_until = time.time() + 5.0
                        notify_feedback("wake")
                        if GUI_APP:
                            GUI_APP.set_status("listening")

                        remainder = " ".join(words[i+1:])
                        if remainder and check_and_execute(remainder, cfg):
                            is_active = False
                            if GUI_APP:
                                GUI_APP.set_status("done")
                        break
            else:
                if any(w in ["вас", "вася", "вась"] or fuzz.ratio(w, "вась") >= 80 for w in clean_words):
                    active_until = time.time() + 5.0
                    notify_feedback("wake")
                elif check_and_execute(text, cfg):
                    is_active = False
                    if GUI_APP:
                        GUI_APP.set_status("done")

def restart_audio_system(device_idx):
    global AUDIO_THREAD, VOICE_THREAD, STOP_AUDIO_EVENT
    STOP_AUDIO_EVENT.set()

    if AUDIO_THREAD and AUDIO_THREAD.is_alive():
        AUDIO_THREAD.join(timeout=0.8)
    if VOICE_THREAD and VOICE_THREAD.is_alive():
        VOICE_THREAD.join(timeout=0.8)

    while not AUDIO_QUEUE.empty():
        try:
            AUDIO_QUEUE.get_nowait()
        except queue.Empty:
            break

    STOP_AUDIO_EVENT.clear()
    AUDIO_THREAD = threading.Thread(target=audio_capture_worker, args=(device_idx,), daemon=True)
    AUDIO_THREAD.start()

    VOICE_THREAD = threading.Thread(target=voice_recognizer_worker, daemon=True)
    VOICE_THREAD.start()

# --- СИСТЕМНЫЙ ТРЕЙ ---
def create_tray_image():
    """Генерация иконки для трея на лету."""
    img = Image.new('RGBA', (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(106, 27, 154, 255))
    d.line([(18, 22), (32, 48), (46, 22)], fill=(255, 255, 255, 255), width=6)
    return img

def setup_tray():
    global TRAY_ICON

    def on_show(icon, item):
        if GUI_APP:
            GUI_APP.after(0, GUI_APP.show_window)

    def on_quit(icon, item):
        icon.stop()
        if GUI_APP:
            GUI_APP.after(0, GUI_APP.force_quit)

    menu = pystray.Menu(
        pystray.MenuItem("Развернуть", on_show, default=True),
        pystray.MenuItem("Выход", on_quit)
    )
    TRAY_ICON = pystray.Icon("VasyaAssistant", create_tray_image(), "Вася Ассистент", menu)
    threading.Thread(target=TRAY_ICON.run, daemon=True).start()

# --- ГРАФИЧЕСКИЙ ИНТЕРФЕЙС ---
class AssistantApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Голосовой ассистент «Вася»")
        self.geometry("830x780")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self.config = load_config()
        self.audio_devices = self._get_input_devices()
        self.tts_voices = self._get_tts_voices()

        self.current_constructor_steps = []

        self._build_ui()
        self._refresh_all_lists()

        selected_dev = self.config.get("device_index")
        restart_audio_system(selected_dev)

    def hide_to_tray(self):
        self.withdraw()

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def force_quit(self):
        STOP_AUDIO_EVENT.set()
        TTS_QUEUE.put(None)
        self.destroy()
        os._exit(0)

    def _get_input_devices(self):
        devs = {}
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get('max_input_channels', 0) > 0:
                    devs[f"#{idx}: {dev['name']}"] = idx
        except Exception:
            pass
        return devs

    def _get_tts_voices(self):
        voices = {}
        try:
            eng = pyttsx3.init()
            for v in eng.getProperty('voices'):
                name = v.name.replace("Microsoft ", "")
                voices[name] = v.id
        except Exception:
            pass
        return voices

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(12, 4))

        title = ctk.CTkLabel(header, text="Ассистент «Вася»", font=ctk.CTkFont(size=22, weight="bold"))
        title.pack(side="left")

        self.status_indicator = ctk.CTkLabel(header, text="● ИНИЦИАЛИЗАЦИЯ", font=ctk.CTkFont(size=12, weight="bold"), text_color="#757575")
        self.status_indicator.pack(side="right", padx=5)

        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True, padx=20, pady=(5, 12))

        self.tab_cmds = self.tabview.add("⚡ Команды")
        self.tab_plans = self.tabview.add("📋 Планы")
        self.tab_const = self.tabview.add("🛠️ Конструктор")
        self.tab_ds = self.tabview.add("💬 Discord")
        self.tab_music = self.tabview.add("🎵 Музыка")
        self.tab_spec = self.tabview.add("🎲 Особые")
        self.tab_settings = self.tabview.add("⚙️ Настройки")

        self._build_commands_tab()
        self._build_plans_tab()
        self._build_constructor_tab()
        self._build_discord_tab()
        self._build_music_tab()
        self._build_special_tab()
        self._build_settings_tab()

    # --- ВКЛАДКА 1: КОМАНДЫ ---
    def _build_commands_tab(self):
        f = ctk.CTkFrame(self.tab_cmds)
        f.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(f, text="Фразы вызова:").grid(row=0, column=0, sticky="w", padx=10, pady=(8, 2))
        self.cmd_alias_entry = ctk.CTkEntry(f, placeholder_text="кс, cs, контра")
        self.cmd_alias_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))

        ctk.CTkLabel(f, text="Путь / URL / Steam:").grid(row=2, column=0, sticky="w", padx=10, pady=(0, 2))
        p_row = ctk.CTkFrame(f, fg_color="transparent")
        p_row.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
        self.cmd_target_entry = ctk.CTkEntry(p_row, placeholder_text="C:\\Games\\game.exe или steam://rungameid/730")
        self.cmd_target_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(p_row, text="Обзор", width=80, command=self._browse_command_target).pack(side="right")

        opts = ctk.CTkFrame(f, fg_color="transparent")
        opts.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
        self.is_game_checkbox = ctk.CTkCheckBox(opts, text="🎮 Это игра (блокировать Discord во время матча)")
        self.is_game_checkbox.pack(side="left", padx=(0, 15))
        self.cmd_proc_entry = ctk.CTkEntry(opts, placeholder_text="Имя процесса (cs2.exe)", width=210)
        self.cmd_proc_entry.pack(side="right")

        f.columnconfigure(0, weight=1)
        ctk.CTkButton(f, text="+ Добавить команду", command=self._add_command, fg_color="#6A1B9A", hover_color="#4A148C").grid(row=5, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))

        self.cmds_scroll = ctk.CTkScrollableFrame(self.tab_cmds, height=270)
        self.cmds_scroll.pack(fill="both", expand=True, padx=10, pady=5)

    def _browse_command_target(self):
        f = filedialog.askopenfilename(title="Файл", filetypes=[("Исполняемые", "*.exe *.bat *.cmd"), ("Все", "*.*")])
        if f:
            norm = os.path.normpath(f)
            self.cmd_target_entry.delete(0, "end")
            self.cmd_target_entry.insert(0, norm)
            base = os.path.basename(norm).lower()
            if base.endswith(".exe"):
                self.cmd_proc_entry.delete(0, "end")
                self.cmd_proc_entry.insert(0, base)
                self.is_game_checkbox.select()

    # --- ВКЛАДКА 2: ПЛАНЫ ---
    def _build_plans_tab(self):
        f = ctk.CTkFrame(self.tab_plans)
        f.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(f, text="Название плана:").grid(row=0, column=0, sticky="w", padx=10, pady=(5, 2))
        self.plan_name_entry = ctk.CTkEntry(f, placeholder_text="Дискорд с запретом")
        self.plan_name_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))

        ctk.CTkLabel(f, text="Фразы вызова:").grid(row=2, column=0, sticky="w", padx=10, pady=(0, 2))
        self.plan_aliases_entry = ctk.CTkEntry(f, placeholder_text="дискорд, запусти дискорд, дс")
        self.plan_aliases_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))

        ctk.CTkLabel(f, text="Шаг 1 (.bat Запрета):").grid(row=4, column=0, sticky="w", padx=10, pady=(0, 2))
        s1 = ctk.CTkFrame(f, fg_color="transparent")
        s1.grid(row=5, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
        self.plan_step1_entry = ctk.CTkEntry(s1, placeholder_text="C:\\zapret\\discord.bat")
        self.plan_step1_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(s1, text="Обзор", width=80, command=lambda: self._browse_into(self.plan_step1_entry)).pack(side="right")

        ctk.CTkLabel(f, text="Шаг 2 (Discord / Программа):").grid(row=6, column=0, sticky="w", padx=10, pady=(0, 2))
        s2 = ctk.CTkFrame(f, fg_color="transparent")
        s2.grid(row=7, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
        self.plan_step2_entry = ctk.CTkEntry(s2, placeholder_text="C:\\Users\\...\\Discord.exe")
        self.plan_step2_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(s2, text="Обзор", width=80, command=lambda: self._browse_into(self.plan_step2_entry)).pack(side="right")

        f.columnconfigure(0, weight=1)
        ctk.CTkButton(f, text="+ Сохранить план", command=self._add_plan, fg_color="#6A1B9A", hover_color="#4A148C").grid(row=8, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))

        self.plans_scroll = ctk.CTkScrollableFrame(self.tab_plans, height=200)
        self.plans_scroll.pack(fill="both", expand=True, padx=10, pady=5)

    # --- ВКЛАДКА 3: КОНСТРУКТОР СУПЕР-МАКРОСОВ ---
    def _build_constructor_tab(self):
        top_f = ctk.CTkFrame(self.tab_const)
        top_f.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(top_f, text="Название макроса:").grid(row=0, column=0, sticky="w", padx=10, pady=(5, 2))
        self.cons_name_entry = ctk.CTkEntry(top_f, placeholder_text="Игровой режим")
        self.cons_name_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))

        ctk.CTkLabel(top_f, text="Фразы вызова (через запятую):").grid(row=2, column=0, sticky="w", padx=10, pady=(0, 2))
        self.cons_alias_entry = ctk.CTkEntry(top_f, placeholder_text="режим катки, время играть")
        self.cons_alias_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))

        step_builder = ctk.CTkFrame(top_f, fg_color="#1E1E24")
        step_builder.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=5)

        ctk.CTkLabel(step_builder, text="Добавить шаг в сценарий:", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 4))

        self.cons_step_type = ctk.CTkOptionMenu(step_builder, values=["Запустить программу (app)", "Запустить готовый план (plan)", "Музыкальное действие (music)"], width=220, command=self._on_cons_type_changed)
        self.cons_step_type.grid(row=1, column=0, padx=10, pady=6)

        self.cons_step_value = ctk.CTkEntry(step_builder, placeholder_text="Путь или название...")
        self.cons_step_value.grid(row=1, column=1, sticky="ew", padx=5, pady=6)

        self.cons_add_step_btn = ctk.CTkButton(step_builder, text="+ Добавить шаг", width=120, command=self._add_constructor_step)
        self.cons_add_step_btn.grid(row=1, column=2, padx=10, pady=6)
        step_builder.columnconfigure(1, weight=1)

        self.cons_current_steps_label = ctk.CTkLabel(top_f, text="Шаги: [пока пусто]", text_color="#BA68C8", anchor="w")
        self.cons_current_steps_label.grid(row=5, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 6))

        top_f.columnconfigure(0, weight=1)
        save_cons_btn = ctk.CTkButton(top_f, text="💾 Сохранить весь макрос в Конструктор", fg_color="#6A1B9A", hover_color="#4A148C", command=self._save_constructor)
        save_cons_btn.grid(row=6, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 8))

        self.cons_scroll = ctk.CTkScrollableFrame(self.tab_const, height=200)
        self.cons_scroll.pack(fill="both", expand=True, padx=10, pady=5)

    def _on_cons_type_changed(self, choice):
        if "plan" in choice:
            plans = [p["name"] for p in self.config.get("plans", [])]
            self.cons_step_value.delete(0, "end")
            self.cons_step_value.insert(0, plans[0] if plans else "Дискорд с запретом")
        elif "music" in choice:
            self.cons_step_value.delete(0, "end")
            self.cons_step_value.insert(0, "pause")
        else:
            self.cons_step_value.delete(0, "end")
            self.cons_step_value.insert(0, "steam://rungameid/730")

    def _add_constructor_step(self):
        stype_raw = self.cons_step_type.get()
        stype = "app" if "app" in stype_raw else "plan" if "plan" in stype_raw else "music"
        val = self.cons_step_value.get().strip()
        if not val:
            return
        self.current_constructor_steps.append({"type": stype, "val": val})
        self._update_cons_steps_display()

    def _update_cons_steps_display(self):
        if not self.current_constructor_steps:
            self.cons_current_steps_label.configure(text="Шаги: [пока пусто]")
            return
        preview = " ➔ ".join([f"[{s['type']}] {os.path.basename(s['val'])}" for s in self.current_constructor_steps])
        self.cons_current_steps_label.configure(text=f"Шаги: {preview}")

    def _save_constructor(self):
        name = self.cons_name_entry.get().strip() or "Макрос"
        raw_aliases = self.cons_alias_entry.get().strip()
        if not raw_aliases or not self.current_constructor_steps:
            messagebox.showwarning("Внимание", "Укажите фразы вызова и добавьте хотя бы 1 шаг.")
            return

        aliases = [a.strip().lower() for a in raw_aliases.split(",") if a.strip()]
        self.config["constructors"].append({
            "name": name,
            "aliases": aliases,
            "steps": list(self.current_constructor_steps)
        })
        save_config(self.config)

        self.cons_name_entry.delete(0, "end")
        self.cons_alias_entry.delete(0, "end")
        self.current_constructor_steps = []
        self._update_cons_steps_display()
        self._refresh_all_lists()

    # --- ВКЛАДКА 4: DISCORD ---
    def _build_discord_tab(self):
        f = ctk.CTkFrame(self.tab_ds)
        f.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(f, text="Обращение (имя через запятую):").grid(row=0, column=0, sticky="w", padx=10, pady=(6, 2))
        self.ds_alias_entry = ctk.CTkEntry(f, placeholder_text="андрею, андрей, дрон")
        self.ds_alias_entry.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))

        ctk.CTkLabel(f, text="Никнейм в Discord:").grid(row=2, column=0, sticky="w", padx=10, pady=(0, 2))
        self.ds_tag_entry = ctk.CTkEntry(f, placeholder_text="Andrey_Best")
        self.ds_tag_entry.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 8))

        f.columnconfigure(0, weight=1)
        ctk.CTkButton(f, text="+ Добавить контакт Discord", command=self._add_contact, fg_color="#6A1B9A", hover_color="#4A148C").grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 8))

        self.ds_scroll = ctk.CTkScrollableFrame(self.tab_ds, height=260)
        self.ds_scroll.pack(fill="both", expand=True, padx=10, pady=5)

    # --- ВКЛАДКА 5: МУЗЫКА ---
    def _build_music_tab(self):
        f = ctk.CTkFrame(self.tab_music, fg_color="#1E1E24")
        f.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(f, text="Быстрый тест медиа-клавиш:", font=ctk.CTkFont(weight="bold")).pack(pady=(8, 4))
        btn_row = ctk.CTkFrame(f, fg_color="transparent")
        btn_row.pack(pady=(0, 10))

        ctk.CTkButton(btn_row, text="⏮ Назад", width=85, command=lambda: press_media_key(VK_MEDIA_PREV_TRACK)).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="⏯ Пауза/Плей", width=110, fg_color="#6A1B9A", hover_color="#4A148C", command=lambda: press_media_key(VK_MEDIA_PLAY_PAUSE)).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="⏭ Вперед", width=85, command=lambda: press_media_key(VK_MEDIA_NEXT_TRACK)).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="🔉 -", width=45, command=lambda: press_media_key(VK_VOLUME_DOWN, 4)).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="🔊 +", width=45, command=lambda: press_media_key(VK_VOLUME_UP, 4)).pack(side="left", padx=4)

        box = ctk.CTkTextbox(self.tab_music, height=330)
        box.pack(fill="both", expand=True, padx=10, pady=5)
        box.insert("end",
            "ГОЛОСОВЫЕ КОМАНДЫ ДЛЯ МУЗЫКИ:\n\n"
            "• Пауза: «Вась, музыка стоп» / «пауза» / «останови музыку»\n"
            "• Плей: «Вась, музыка включить» / «включи музыку» / «музыка играть»\n"
            "• Вперед: «Вась, следующий трек» / «музыка дальше» / «переключи трек»\n"
            "• Назад: «Вась, предыдущий трек» / «музыка назад»\n"
            "• Громкость: «Вась, музыка громче» / «тише» / «выключи звук»\n"
        )
        box.configure(state="disabled")

    # --- ВКЛАДКА 6: ОСОБЫЕ КОМАНДЫ (С ГОЛОСОМ) ---
    def _build_special_tab(self):
        box = ctk.CTkTextbox(self.tab_spec, height=340)
        box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert("end",
            "ЭКСКЛЮЗИВНЫЕ ОСОБЫЕ КОМАНДЫ (С ГОЛОСОВЫМ ОТВЕТОМ):\n\n"
            "1. 🪙 ПОДБРОС МОНЕТКИ:\n"
            "   Команды: «Вась, подбрось монетку» | «Вась, орел или решка» | «Вась, монетка»\n"
            "   Действие: Раздается звон монетки, после чего Вася озвучивает:\n"
            "   «Выпал орёл!» или «Выпала решка!».\n\n"
            "2. 🕒 ВРЕМЯ:\n"
            "   Команды: «Вась, который час» | «Вась, сколько времени»\n"
            "   Действие: Вася голосом называет точное системное время.\n\n"
            "3. 🎲 КУБИК:\n"
            "   Команды: «Вась, брось кубик» | «Вась, кинь кость»\n"
            "   Действие: Вася называет число от 1 до 6.\n"
        )
        box.configure(state="disabled")

        test_row = ctk.CTkFrame(self.tab_spec, fg_color="transparent")
        test_row.pack(fill="x", padx=10, pady=5)
        ctk.CTkButton(test_row, text="🪙 Проверить монетку прямо сейчас", fg_color="#6A1B9A", hover_color="#4A148C",
                      command=lambda: handle_special_commands("подбрось монетку")).pack(fill="x")

    # --- ВКЛАДКА 7: НАСТРОЙКИ ---
    def _build_settings_tab(self):
        sett_frame = ctk.CTkScrollableFrame(self.tab_settings, height=540)
        sett_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # 1. Автозапуск
        self.autostart_var = ctk.BooleanVar(value=is_autostart_enabled())
        autostart_cb = ctk.CTkCheckBox(sett_frame, text="🚀 Запускать вместе с Windows (в фоне)",
                                       variable=self.autostart_var, command=self._on_autostart_toggle)
        autostart_cb.pack(anchor="w", padx=10, pady=10)

        # 2. Режим ответов
        ctk.CTkLabel(sett_frame, text="Режим откликов ассистента:", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(10, 2))
        self.feedback_mode_menu = ctk.CTkOptionMenu(
            sett_frame, values=["Звуки + Голос (Рекомендуется)", "Только голос", "Только звуки (Бипы)"],
            command=self._on_feedback_mode_changed, width=280
        )
        current_mode = self.config.get("feedback_mode", "both")
        mode_str = "Звуки + Голос (Рекомендуется)" if current_mode == "both" else "Только голос" if current_mode == "voice" else "Только звуки (Бипы)"
        self.feedback_mode_menu.set(mode_str)
        self.feedback_mode_menu.pack(anchor="w", padx=10, pady=(0, 10))

        # 3. Выбор голоса и тест
        ctk.CTkLabel(sett_frame, text="Голос ассистента (Windows SAPI):", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(5, 2))
        voice_names = list(self.tts_voices.keys()) or ["Стандартный голос"]
        self.voice_menu = ctk.CTkOptionMenu(sett_frame, values=voice_names, command=self._on_voice_changed, width=280)

        cur_v_id = self.config.get("voice_id")
        cur_v_name = voice_names[0]
        for k, v in self.tts_voices.items():
            if v == cur_v_id:
                cur_v_name = k
                break
        self.voice_menu.set(cur_v_name)
        self.voice_menu.pack(anchor="w", padx=10, pady=(0, 5))

        test_tts_btn = ctk.CTkButton(sett_frame, text="🔊 Тест голоса", width=120, command=lambda: speak_text("Привет! Я готов к работе."))
        test_tts_btn.pack(anchor="w", padx=10, pady=(0, 10))

        # 4. Громкость голоса
        self.vol_label = ctk.CTkLabel(sett_frame, text=f"Громкость голоса: {self.config.get('voice_volume', 85)}%", font=ctk.CTkFont(weight="bold"))
        self.vol_label.pack(anchor="w", padx=10, pady=(5, 2))
        self.vol_slider = ctk.CTkSlider(sett_frame, from_=0, to=100, number_of_steps=100, command=self._on_voice_vol_slider)
        self.vol_slider.set(self.config.get("voice_volume", 85))
        self.vol_slider.pack(anchor="w", padx=10, pady=(0, 15))

        # 5. Микрофон
        ctk.CTkLabel(sett_frame, text="Микрофон:", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(5, 2))
        dev_names = list(self.audio_devices.keys()) or ["Микрофоны не найдены"]
        current_name = dev_names[0]
        saved_idx = self.config.get("device_index")
        for k, v in self.audio_devices.items():
            if v == saved_idx:
                current_name = k
                break

        self.mic_dropdown = ctk.CTkOptionMenu(sett_frame, values=dev_names, command=self._on_device_changed, width=320)
        self.mic_dropdown.set(current_name)
        self.mic_dropdown.pack(anchor="w", padx=10, pady=(0, 8))

        ctk.CTkLabel(sett_frame, text="Индикатор чувствительности:").pack(anchor="w", padx=10, pady=(0, 2))
        self.meter = ctk.CTkProgressBar(sett_frame, width=280, progress_color="#6A1B9A")
        self.meter.set(0.0)
        self.meter.pack(anchor="w", padx=10, pady=(0, 10))

    # --- ОБРАБОТЧИКИ НАСТРОЕК ---
    def _on_autostart_toggle(self):
        enabled = self.autostart_var.get()
        set_autostart(enabled)

    def _on_feedback_mode_changed(self, choice):
        if "Голос (Рек" in choice:
            self.config["feedback_mode"] = "both"
        elif "Только голос" in choice:
            self.config["feedback_mode"] = "voice"
        else:
            self.config["feedback_mode"] = "sound"
        save_config(self.config)

    def _on_voice_changed(self, choice):
        v_id = self.tts_voices.get(choice)
        self.config["voice_id"] = v_id
        save_config(self.config)

    def _on_voice_vol_slider(self, val):
        vol_int = int(val)
        self.vol_label.configure(text=f"Громкость голоса: {vol_int}%")
        self.config["voice_volume"] = vol_int
        save_config(self.config)

    def _on_device_changed(self, choice):
        device_idx = self.audio_devices.get(choice)
        self.config["device_index"] = device_idx
        save_config(self.config)
        restart_audio_system(device_idx)

    def update_meter(self, level: float):
        def _update():
            self.meter.set(level)
        self.after(1, _update)

    def set_status(self, state: str):
        if state == "listening":
            self.status_indicator.configure(text="● СЛУШАЮ (5 сек)", text_color="#BA68C8")
        elif state == "done":
            self.status_indicator.configure(text="● ВЫПОЛНЕНО", text_color="#4CAF50")
            self.after(1400, lambda: self.set_status("idle"))
        elif state == "blocked":
            self.status_indicator.configure(text="● ЗАБЛОКИРОВАНО (В ИГРЕ)", text_color="#FF9800")
            self.after(2000, lambda: self.set_status("idle"))
        elif state == "error_model":
            self.status_indicator.configure(text="● НЕТ ПАПКИ MODEL", text_color="#E53935")
        elif state == "error_mic":
            self.status_indicator.configure(text="● ОШИБКА МИКРОФОНА", text_color="#E53935")
        else:
            self.status_indicator.configure(text="● ОЖИДАНИЕ («Вась»)", text_color="#757575")

    def _browse_into(self, entry_widget):
        f = filedialog.askopenfilename(title="Файл", filetypes=[("Исполняемые", "*.exe *.bat *.cmd"), ("Все", "*.*")])
        if f:
            entry_widget.delete(0, "end")
            entry_widget.insert(0, os.path.normpath(f))

    # --- СОХРАНЕНИЕ / ОБНОВЛЕНИЕ СПИСКОВ ---
    def _add_command(self):
        raw_aliases = self.cmd_alias_entry.get().strip()
        target = self.cmd_target_entry.get().strip()
        is_game = bool(self.is_game_checkbox.get())
        proc_name = self.cmd_proc_entry.get().strip().lower()

        if not raw_aliases or not target:
            messagebox.showwarning("Внимание", "Заполните фразы и путь к файлу.")
            return

        aliases = [a.strip().lower() for a in raw_aliases.split(",") if a.strip()]
        self.config["commands"].append({
            "aliases": aliases, "target": target, "is_game": is_game, "process_name": proc_name
        })
        save_config(self.config)
        self.cmd_alias_entry.delete(0, "end")
        self.cmd_target_entry.delete(0, "end")
        self.cmd_proc_entry.delete(0, "end")
        self.is_game_checkbox.deselect()
        self._refresh_all_lists()

    def _add_plan(self):
        name = self.plan_name_entry.get().strip() or "Новый план"
        raw_aliases = self.plan_aliases_entry.get().strip()
        step1 = self.plan_step1_entry.get().strip()
        step2 = self.plan_step2_entry.get().strip()

        if not raw_aliases or (not step1 and not step2):
            messagebox.showwarning("Внимание", "Укажите фразы и шаги плана.")
            return

        aliases = [a.strip().lower() for a in raw_aliases.split(",") if a.strip()]
        steps = [s for s in [step1, step2] if s]

        self.config["plans"].append({"name": name, "aliases": aliases, "steps": steps})
        save_config(self.config)
        self.plan_name_entry.delete(0, "end")
        self.plan_aliases_entry.delete(0, "end")
        self.plan_step1_entry.delete(0, "end")
        self.plan_step2_entry.delete(0, "end")
        self._refresh_all_lists()

    def _add_contact(self):
        raw_aliases = self.ds_alias_entry.get().strip()
        tag = self.ds_tag_entry.get().strip()
        if not raw_aliases or not tag:
            messagebox.showwarning("Внимание", "Заполните имя и ник Discord.")
            return

        aliases = [a.strip().lower() for a in raw_aliases.split(",") if a.strip()]
        self.config["contacts"].append({"aliases": aliases, "discord_tag": tag})
        save_config(self.config)
        self.ds_alias_entry.delete(0, "end")
        self.ds_tag_entry.delete(0, "end")
        self._refresh_all_lists()

    def _refresh_all_lists(self):
        # 1. Команды
        for w in self.cmds_scroll.winfo_children():
            w.destroy()
        for idx, item in enumerate(self.config.get("commands", [])):
            card = ctk.CTkFrame(self.cmds_scroll, fg_color="#1E1E24")
            card.pack(fill="x", padx=5, pady=4)
            tb = ctk.CTkFrame(card, fg_color="transparent")
            tb.pack(side="left", fill="both", expand=True, padx=10, pady=5)
            tag = f" [🎮 Игра: {item.get('process_name')}]" if item.get("is_game") else ""
            ctk.CTkLabel(tb, text=f"Фразы: {', '.join(item['aliases'])}{tag}", font=ctk.CTkFont(weight="bold"),
                         text_color="#BA68C8" if item.get("is_game") else "white", anchor="w").pack(fill="x")
            ctk.CTkLabel(tb, text=item["target"], text_color="gray", anchor="w").pack(fill="x")
            ctk.CTkButton(card, text="Удалить", width=70, fg_color="#B71C1C", hover_color="#7F0000",
                          command=lambda i=idx: self._delete_item("commands", i)).pack(side="right", padx=10, pady=5)

        # 2. Планы
        for w in self.plans_scroll.winfo_children():
            w.destroy()
        for idx, item in enumerate(self.config.get("plans", [])):
            card = ctk.CTkFrame(self.plans_scroll, fg_color="#1E1E24")
            card.pack(fill="x", padx=5, pady=4)
            tb = ctk.CTkFrame(card, fg_color="transparent")
            tb.pack(side="left", fill="both", expand=True, padx=10, pady=5)
            ctk.CTkLabel(tb, text=f"План: {item.get('name')} | {', '.join(item['aliases'])}", font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
            steps_txt = " ➔ ".join([os.path.basename(s) for s in item.get("steps", [])])
            ctk.CTkLabel(tb, text=f"Шаги: {steps_txt}", text_color="#BA68C8", anchor="w").pack(fill="x")
            ctk.CTkButton(card, text="Удалить", width=70, fg_color="#B71C1C", hover_color="#7F0000",
                          command=lambda i=idx: self._delete_item("plans", i)).pack(side="right", padx=10, pady=5)

        # 3. Конструктор
        for w in self.cons_scroll.winfo_children():
            w.destroy()
        for idx, item in enumerate(self.config.get("constructors", [])):
            card = ctk.CTkFrame(self.cons_scroll, fg_color="#1E1E24")
            card.pack(fill="x", padx=5, pady=4)
            tb = ctk.CTkFrame(card, fg_color="transparent")
            tb.pack(side="left", fill="both", expand=True, padx=10, pady=5)
            ctk.CTkLabel(tb, text=f"Макрос: {item.get('name')} | {', '.join(item['aliases'])}", font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
            steps_txt = " ➔ ".join([f"[{s['type']}] {os.path.basename(s['val'])}" for s in item.get("steps", [])])
            ctk.CTkLabel(tb, text=f"Цепочка: {steps_txt}", text_color="#64B5F6", anchor="w").pack(fill="x")
            ctk.CTkButton(card, text="Удалить", width=70, fg_color="#B71C1C", hover_color="#7F0000",
                          command=lambda i=idx: self._delete_item("constructors", i)).pack(side="right", padx=10, pady=5)

        # 4. Контакты
        for w in self.ds_scroll.winfo_children():
            w.destroy()
        for idx, item in enumerate(self.config.get("contacts", [])):
            card = ctk.CTkFrame(self.ds_scroll, fg_color="#1E1E24")
            card.pack(fill="x", padx=5, pady=4)
            tb = ctk.CTkFrame(card, fg_color="transparent")
            tb.pack(side="left", fill="both", expand=True, padx=10, pady=5)
            ctk.CTkLabel(tb, text=f"Обращение: {', '.join(item['aliases'])}", font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
            ctk.CTkLabel(tb, text=f"Discord: {item['discord_tag']}", text_color="#64B5F6", anchor="w").pack(fill="x")
            ctk.CTkButton(card, text="Удалить", width=70, fg_color="#B71C1C", hover_color="#7F0000",
                          command=lambda i=idx: self._delete_item("contacts", i)).pack(side="right", padx=10, pady=5)

    def _delete_item(self, category: str, index: int):
        if 0 <= index < len(self.config[category]):
            self.config[category].pop(index)
            save_config(self.config)
            self._refresh_all_lists()

if __name__ == "__main__":
    TTS_THREAD = threading.Thread(target=tts_worker, daemon=True)
    TTS_THREAD.start()

    app = AssistantApp()
    GUI_APP = app
    setup_tray()
    app.mainloop()