import ctypes
from datetime import datetime
import json
import os
import queue
import re
import sys
import threading
import time
import webbrowser
import winsound
from tkinter import filedialog, messagebox

import customtkinter as ctk
import numpy as np
from PIL import ImageGrab
from rapidfuzz import fuzz
import sounddevice as sd
from vosk import KaldiRecognizer, Model

# --- ОПРЕДЕЛЕНИЕ ПУТЕЙ (СОВМЕСТИМОСТЬ С РЕЖИМОМ --onefile) ---
if getattr(sys, 'frozen', False):
    # Папка, где лежит сам .exe файл (для сохранения конфига и скриншотов)
    EXE_DIR = os.path.dirname(sys.executable)
    # Временная системная папка, куда распаковываются ресурсы (включая зашитую модель)
    INTERNAL_DIR = getattr(sys, '_MEIPASS', EXE_DIR)
else:
    EXE_DIR = os.path.dirname(os.path.abspath(__file__))
    INTERNAL_DIR = EXE_DIR

CONFIG_FILE = os.path.join(EXE_DIR, "commands.json")
SCREENSHOTS_DIR = os.path.join(EXE_DIR, "Screenshots")

AUDIO_QUEUE = queue.Queue()
GUI_APP = None

STOP_AUDIO_EVENT = threading.Event()
AUDIO_THREAD = None
VOICE_THREAD = None

# --- БЕЗОПАСНЫЙ ПУТЬ ДЛЯ WINDOWS (ОБХОД КИРИЛЛИЦЫ В C++ VOSK) ---
def get_safe_win_path(path: str) -> str:
    if sys.platform != "win32":
        return path
    buffer = ctypes.create_unicode_buffer(1024)
    res = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, 1024)
    return buffer.value if res > 0 else path

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
                full_p = os.path.join(search_root, item)
                if os.path.isdir(full_p) and "vosk" in item.lower():
                    candidate_dirs.append(full_p)
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

# --- ЗВУКОВЫЕ СИГНАЛЫ ---
def play_sound(sound_type="wake"):
    def _play():
        try:
            if sound_type == "wake":
                winsound.Beep(850, 120)
            elif sound_type == "success":
                winsound.Beep(1000, 70)
                time.sleep(0.05)
                winsound.Beep(1300, 90)
        except Exception:
            pass
    threading.Thread(target=_play, daemon=True).start()

# --- СКРИНШОТ ---
def take_screenshot():
    try:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filepath = os.path.join(SCREENSHOTS_DIR, f"screenshot_{timestamp}.png")
        img = ImageGrab.grab(all_screens=True)
        img.save(filepath, "PNG")
        print(f"[Скриншот] Сохранен: {filepath}")
        play_sound("success")
        return True
    except Exception as e:
        print(f"[Ошибка скриншота]: {e}")
        return False

# --- ХРАНЕНИЕ НАСТРОЕК ---
def load_config():
    default_cfg = {
        "device_index": None,
        "commands": [
            {"aliases": ["ютуб", "youtube", "утуб"], "target": "https://www.youtube.com"},
            {"aliases": ["кс", "cs", "кс2", "контра"], "target": "steam://rungameid/730"}
        ]
    }
    if not os.path.exists(CONFIG_FILE):
        save_config(default_cfg)
        return default_cfg
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"device_index": None, "commands": data}
    except Exception:
        return default_cfg

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def build_grammar(commands_list):
    words = {
        "вась", "вася", "вас", "василий",
        "открой", "включи", "запусти", "перейди",
        "скрин", "скриншот",
        "[unk]"
    }
    for item in commands_list:
        for alias in item.get("aliases", []):
            for part in alias.lower().split():
                clean = re.sub(r'[^а-яА-ЯёЁ0-9]', '', part)
                if clean:
                    words.add(clean)
    return json.dumps(list(words), ensure_ascii=False)

# --- ИСПОЛНЕНИЕ ДЕЙСТВИЙ ---
def execute_target(target: str):
    try:
        if target.startswith(("steam://", "http://", "https://")):
            webbrowser.open(target)
        else:
            os.startfile(target)
        play_sound("success")
        return True
    except Exception as e:
        print(f"[Ошибка запуска]: {e}")
        return False

def check_and_execute(phrase: str, commands_list) -> bool:
    clean = phrase.lower()
    if any(sc in clean for sc in ["скриншот", "скрин"]):
        return take_screenshot()

    words = clean.split()
    for item in commands_list:
        target = item["target"]
        for alias in item["aliases"]:
            if alias in clean:
                return execute_target(target)
            for w in words:
                if fuzz.ratio(w, alias) >= 82:
                    return execute_target(target)
    return False

# --- НЕЗАВИСИМЫЙ ПОТОК ЗАХВАТА МИКРОФОНА ---
def audio_capture_worker(device_idx):
    try:
        device_info = sd.query_devices(device_idx, 'input')
        samplerate = int(device_info['default_samplerate'])
    except Exception as e:
        print(f"[Ошибка устройства #{device_idx}]: {e}")
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
            dev_name = device_info.get('name', 'Микрофон')
            print(f"[Микрофон подключен]: #{device_idx} - {dev_name} ({samplerate} Hz)")
            if GUI_APP:
                GUI_APP.set_status("idle")
            while not STOP_AUDIO_EVENT.is_set():
                time.sleep(0.1)
    except Exception as e:
        print(f"[Ошибка потока звука]: {e}")
        if GUI_APP:
            GUI_APP.set_status("error_mic")

# --- ПОТОК РАСПОЗНАВАНИЯ VOSK ---
def voice_recognizer_worker():
    model_dir = find_valid_model_dir()
    if not model_dir:
        print("\n[ОШИБКА] Папка с моделью Vosk не найдена!")
        if GUI_APP:
            GUI_APP.set_status("error_model")
        return

    print(f"[Vosk] Загрузка модели из: {model_dir}")
    try:
        model = Model(model_dir)
    except Exception as e:
        print(f"\n[Критический сбой Vosk Model]: {e}")
        if GUI_APP:
            GUI_APP.set_status("error_model")
        return

    recognizer = None
    current_samplerate = None
    is_active = False
    active_until = 0.0

    print("[Vosk] Распознавание активно. Ожидаю голос...")

    while not STOP_AUDIO_EVENT.is_set():
        try:
            data, samplerate = AUDIO_QUEUE.get(timeout=0.2)
        except queue.Empty:
            continue

        if recognizer is None or current_samplerate != samplerate:
            current_samplerate = samplerate
            cfg = load_config()
            grammar = build_grammar(cfg.get("commands", []))
            try:
                recognizer = KaldiRecognizer(model, float(current_samplerate), grammar)
            except Exception:
                recognizer = KaldiRecognizer(model, float(current_samplerate))

        if is_active and time.time() > active_until:
            is_active = False
            if GUI_APP:
                GUI_APP.set_status("idle")
            print("[Ассистент] Время ожидания вышло, перехожу в режим сна.")

        # Метод библиотеки Vosk: AcceptWaveform
        if recognizer.AcceptWaveform(data):
            res = json.loads(recognizer.Result())
            text = res.get("text", "").strip()
            if not text or text == "[unk]":
                continue

            print(f"[Услышал]: {text}")
            words = text.split()
            cfg = load_config()
            commands = cfg.get("commands", [])

            clean_words = [re.sub(r'(.)\1+', r'\1', w) for w in words]

            if not is_active:
                for i, w in enumerate(clean_words):
                    if w in ["вас", "вася", "вась", "василий"] or fuzz.ratio(w, "вась") >= 80 or fuzz.ratio(w, "вася") >= 80:
                        is_active = True
                        active_until = time.time() + 5.0
                        play_sound("wake")
                        if GUI_APP:
                            GUI_APP.set_status("listening")
                        print("[Ассистент] Слушаю команду (активен 5 секунд)...")

                        remainder = " ".join(words[i+1:])
                        if remainder and check_and_execute(remainder, commands):
                            is_active = False
                            if GUI_APP:
                                GUI_APP.set_status("done")
                        break
            else:
                if any(w in ["вас", "вася", "вась"] or fuzz.ratio(w, "вась") >= 80 for w in clean_words):
                    active_until = time.time() + 5.0
                    play_sound("wake")
                elif check_and_execute(text, commands):
                    is_active = False
                    if GUI_APP:
                        GUI_APP.set_status("done")

# --- ПЕРЕЗАПУСК СИСТЕМЫ ---
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

# --- ГРАФИЧЕСКИЙ ИНТЕРФЕЙС ---
class AssistantApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Голосовой ассистент «Вася»")
        self.geometry("760x710")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.config = load_config()
        self.audio_devices = self._get_input_devices()

        self._build_ui()
        self._refresh_list()

        selected_dev = self.config.get("device_index")
        restart_audio_system(selected_dev)

    def _get_input_devices(self):
        devs = {}
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get('max_input_channels', 0) > 0:
                    devs[f"#{idx}: {dev['name']}"] = idx
        except Exception as e:
            print(f"[Ошибка сканирования микрофонов]: {e}")
        return devs

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 5))

        title = ctk.CTkLabel(header, text="Ассистент «Вася»", font=ctk.CTkFont(size=22, weight="bold"))
        title.pack(side="left")

        self.status_indicator = ctk.CTkLabel(header, text="● ИНИЦИАЛИЗАЦИЯ", font=ctk.CTkFont(size=12, weight="bold"), text_color="#757575")
        self.status_indicator.pack(side="right", padx=5)

        # Панель выбора микрофона и визуального теста
        mic_frame = ctk.CTkFrame(self, fg_color="#1E1E24")
        mic_frame.pack(fill="x", padx=20, pady=8)

        ctk.CTkLabel(mic_frame, text="Микрофон:", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w", padx=10, pady=8)

        dev_names = list(self.audio_devices.keys())
        if not dev_names:
            dev_names = ["Микрофоны не найдены"]

        current_name = dev_names[0]
        saved_idx = self.config.get("device_index")
        for k, v in self.audio_devices.items():
            if v == saved_idx:
                current_name = k
                break

        self.mic_dropdown = ctk.CTkOptionMenu(
            mic_frame, values=dev_names, command=self._on_device_changed, width=330
        )
        self.mic_dropdown.set(current_name)
        self.mic_dropdown.grid(row=0, column=1, sticky="w", padx=5, pady=8)

        ctk.CTkLabel(mic_frame, text="Тест:").grid(row=0, column=2, sticky="e", padx=(10, 5), pady=8)
        self.meter = ctk.CTkProgressBar(mic_frame, width=150, progress_color="#6A1B9A")
        self.meter.set(0.0)
        self.meter.grid(row=0, column=3, sticky="w", padx=(0, 10), pady=8)

        # Форма добавления команд
        add_frame = ctk.CTkFrame(self)
        add_frame.pack(fill="x", padx=20, pady=5)

        ctk.CTkLabel(add_frame, text="Слова вызова (через запятую):").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 2))
        self.alias_entry = ctk.CTkEntry(add_frame, placeholder_text="ютуб, youtube, видосы")
        self.alias_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

        ctk.CTkLabel(add_frame, text="Путь к .exe, URL или Steam URI:").grid(row=2, column=0, sticky="w", padx=10, pady=(0, 2))
        path_row = ctk.CTkFrame(add_frame, fg_color="transparent")
        path_row.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

        self.target_entry = ctk.CTkEntry(path_row, placeholder_text="C:\\Games\\game.exe или steam://rungameid/730")
        self.target_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        browse_btn = ctk.CTkButton(path_row, text="Обзор .exe", width=90, command=self._browse_file)
        browse_btn.pack(side="right")

        add_frame.columnconfigure(0, weight=1)

        add_btn = ctk.CTkButton(add_frame, text="+ Добавить команду", command=self._add_command, fg_color="#6A1B9A", hover_color="#4A148C")
        add_btn.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

        # Список сохраненных команд
        list_label = ctk.CTkLabel(self, text="Активные команды:", font=ctk.CTkFont(size=14, weight="bold"))
        list_label.pack(anchor="w", padx=20, pady=(10, 5))

        self.scrollable_frame = ctk.CTkScrollableFrame(self, height=220)
        self.scrollable_frame.pack(fill="both", expand=True, padx=20, pady=(0, 15))

    def update_meter(self, level: float):
        def _update():
            self.meter.set(level)
        self.after(1, _update)

    def _on_device_changed(self, choice):
        device_idx = self.audio_devices.get(choice)
        self.config["device_index"] = device_idx
        save_config(self.config)
        restart_audio_system(device_idx)

    def set_status(self, state: str):
        if state == "listening":
            self.status_indicator.configure(text="● СЛУШАЮ (5 сек)", text_color="#BA68C8")
        elif state == "done":
            self.status_indicator.configure(text="● ВЫПОЛНЕНО", text_color="#4CAF50")
            self.after(1500, lambda: self.set_status("idle"))
        elif state == "error_model":
            self.status_indicator.configure(text="● НЕТ ПАПКИ MODEL", text_color="#E53935")
        elif state == "error_mic":
            self.status_indicator.configure(text="● ОШИБКА МИКРОФОНА", text_color="#E53935")
        else:
            self.status_indicator.configure(text="● ОЖИДАНИЕ («Вась»)", text_color="#757575")

    def _browse_file(self):
        f = filedialog.askopenfilename(
            title="Выберите программу или игру",
            filetypes=[("Исполняемые файлы", "*.exe *.bat *.cmd"), ("Все файлы", "*.*")]
        )
        if f:
            self.target_entry.delete(0, "end")
            self.target_entry.insert(0, os.path.normpath(f))

    def _add_command(self):
        raw_aliases = self.alias_entry.get().strip()
        target = self.target_entry.get().strip()
        if not raw_aliases or not target:
            messagebox.showwarning("Внимание", "Заполните фразы и путь к файлу.")
            return

        aliases = [a.strip().lower() for a in raw_aliases.split(",") if a.strip()]
        self.config["commands"].append({"aliases": aliases, "target": target})
        save_config(self.config)

        self.alias_entry.delete(0, "end")
        self.target_entry.delete(0, "end")
        self._refresh_list()
        restart_audio_system(self.config.get("device_index"))

    def _delete_command(self, index):
        if 0 <= index < len(self.config["commands"]):
            self.config["commands"].pop(index)
            save_config(self.config)
            self._refresh_list()
            restart_audio_system(self.config.get("device_index"))

    def _refresh_list(self):
        for w in self.scrollable_frame.winfo_children():
            w.destroy()

        cmds = self.config.get("commands", [])
        if not cmds:
            ctk.CTkLabel(self.scrollable_frame, text="Команд нет. Добавьте первую сверху!", text_color="gray").pack(pady=20)
            return

        for idx, item in enumerate(cmds):
            card = ctk.CTkFrame(self.scrollable_frame, fg_color="#1E1E24")
            card.pack(fill="x", padx=5, pady=4)

            text_box = ctk.CTkFrame(card, fg_color="transparent")
            text_box.pack(side="left", fill="both", expand=True, padx=10, pady=5)

            aliases_str = ", ".join(item["aliases"])
            ctk.CTkLabel(text_box, text=f"Фразы: {aliases_str}", font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
            ctk.CTkLabel(text_box, text=item["target"], text_color="gray", anchor="w").pack(fill="x")

            del_btn = ctk.CTkButton(
                card, text="Удалить", width=70, fg_color="#B71C1C", hover_color="#7F0000",
                command=lambda i=idx: self._delete_command(i)
            )
            del_btn.pack(side="right", padx=10, pady=5)

if __name__ == "__main__":
    app = AssistantApp()
    GUI_APP = app
    app.mainloop()