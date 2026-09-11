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
import psutil
import pyautogui
import pygetwindow as gw
import pyperclip
from PIL import ImageGrab
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

AUDIO_QUEUE = queue.Queue()
GUI_APP = None

STOP_AUDIO_EVENT = threading.Event()
AUDIO_THREAD = None
VOICE_THREAD = None


# --- БЕЗОПАСНЫЙ ПУТЬ ДЛЯ WINDOWS ---
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
                if os.path.isdir(sub_path) and (
                        os.path.exists(os.path.join(sub_path, "am")) or os.path.exists(os.path.join(sub_path, "conf"))):
                    return get_safe_win_path(sub_path)
        except Exception:
            pass
    return None


# --- ЗВУКОВЫЕ СИГНАЛЫ ---
def play_sound(sound_type="wake"):
    def _play():
        try:
            if sound_type == "wake":
                winsound.Beep(750, 75)
            elif sound_type == "success":
                winsound.Beep(800, 50)
                time.sleep(0.04)
                winsound.Beep(1000, 60)
            elif sound_type == "blocked":
                # Глухой нисходящий сигнал (блокировка/отмена)
                winsound.Beep(450, 100)
                time.sleep(0.03)
                winsound.Beep(350, 120)
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
            {
                "aliases": ["кс", "cs", "контра"],
                "target": "steam://rungameid/730",
                "is_game": True,
                "process_name": "cs2.exe"
            },
            {
                "aliases": ["ютуб", "youtube"],
                "target": "https://www.youtube.com",
                "is_game": False,
                "process_name": ""
            }
        ],
        "plans": [
            {
                "name": "Дискорд с запретом",
                "aliases": ["дискорд", "дс", "войс"],
                "steps": [
                    "C:\\zapret\\discord.bat",
                    "https://discord.com/app"
                ]
            }
        ],
        "contacts": [
            {"aliases": ["андрею", "андрей", "дрон"], "discord_tag": "Andrey"}
        ]
    }
    if not os.path.exists(CONFIG_FILE):
        save_config(default_cfg)
        return default_cfg
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "plans" not in data:
                data["plans"] = default_cfg["plans"]
            if "contacts" not in data:
                data["contacts"] = default_cfg["contacts"]
            return data
    except Exception:
        return default_cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# --- ПРОВЕРКА ЗАПУЩЕННЫХ ИГР ---
def is_game_running(cfg: dict) -> tuple[bool, str]:
    """Проверяет, запущен ли хоть один процесс, помеченный как игра."""
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
    except Exception as e:
        print(f"[Ошибка сканирования процессов]: {e}")

    return False, ""


# --- ИСПОЛНЕНИЕ ДЕЙСТВИЙ ---
def execute_target(target: str, play_snd=True):
    try:
        if target.startswith(("steam://", "http://", "https://")):
            webbrowser.open(target)
        else:
            os.startfile(target)
        if play_snd:
            play_sound("success")
        return True
    except Exception as e:
        print(f"[Ошибка запуска {target}]: {e}")
        return False


def execute_plan(steps: list):
    def _run():
        for idx, step_target in enumerate(steps):
            if not step_target:
                continue
            print(f"[План] Выполнение шага {idx + 1}: {step_target}")
            execute_target(step_target, play_snd=False)
            if idx < len(steps) - 1:
                time.sleep(1.2)
        play_sound("success")

    threading.Thread(target=_run, daemon=True).start()


# --- ОТПРАВКА В DISCORD С ЗАЩИТОЙ ОТ АЛЬТ-ТАБА В ИГРЕ ---
def send_discord_message(discord_tag: str, text: str, cfg: dict):
    def _send():
        # 1. ПРОВЕРКА: активна ли игра
        game_active, game_name = is_game_running(cfg)
        if game_active:
            print(f"\n[ОТМЕНА] Обнаружена запущенная игра: {game_name}!")
            print("[Вася] Сообщение отменено, чтобы не сворачивать игру.")
            play_sound("blocked")
            if GUI_APP:
                GUI_APP.set_status("blocked")
            return

        # 2. Поиск окна Discord
        try:
            windows = [w for w in gw.getAllWindows() if 'discord' in w.title.lower()]
            if not windows:
                print("[Discord] Окно Discord не найдено.")
                play_sound("blocked")
                return

            win = windows[0]
            if win.isMinimized:
                win.restore()
            win.activate()
            time.sleep(0.12)

            pyautogui.hotkey('ctrl', 'k')
            time.sleep(0.12)

            pyperclip.copy(discord_tag)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.15)
            pyautogui.press('enter')
            time.sleep(0.15)

            pyperclip.copy(text)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.08)
            pyautogui.press('enter')

            play_sound("success")
            print(f"[Discord] Успешно отправлено {discord_tag}: {text}")
        except Exception as e:
            print(f"[Ошибка Discord автоматизации]: {e}")
            play_sound("blocked")

    threading.Thread(target=_send, daemon=True).start()


# --- АНАЛИЗАТОР ГОЛОСА ---
def check_and_execute(phrase: str, cfg: dict) -> bool:
    clean = phrase.lower().strip()
    words = clean.split()
    if not words:
        return False

    if any(sc in clean for sc in ["скриншот", "скрин"]):
        return take_screenshot()

    # Отправка сообщений
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
                        else:
                            return False

    # Планы
    for plan in cfg.get("plans", []):
        for alias in plan.get("aliases", []):
            if alias in clean or any(fuzz.ratio(w, alias) >= 82 for w in words):
                execute_plan(plan.get("steps", []))
                return True

    # Одиночные команды
    for item in cfg.get("commands", []):
        target = item["target"]
        for alias in item.get("aliases", []):
            if alias in clean or any(fuzz.ratio(w, alias) >= 82 for w in words):
                return execute_target(target)

    return False


# --- АУДИОПОТОК ---
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
        print(f"[Сбой модели Vosk]: {e}")
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
                        play_sound("wake")
                        if GUI_APP:
                            GUI_APP.set_status("listening")

                        remainder = " ".join(words[i + 1:])
                        if remainder and check_and_execute(remainder, cfg):
                            is_active = False
                            if GUI_APP:
                                GUI_APP.set_status("done")
                        break
            else:
                if any(w in ["вас", "вася", "вась"] or fuzz.ratio(w, "вась") >= 80 for w in clean_words):
                    active_until = time.time() + 5.0
                    play_sound("wake")
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


# --- ГРАФИЧЕСКИЙ ИНТЕРФЕЙС ---
class AssistantApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Голосовой ассистент «Вася»")
        self.geometry("790x760")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.config = load_config()
        self.audio_devices = self._get_input_devices()

        self._build_ui()
        self._refresh_all_lists()

        selected_dev = self.config.get("device_index")
        restart_audio_system(selected_dev)

    def _get_input_devices(self):
        devs = {}
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get('max_input_channels', 0) > 0:
                    devs[f"#{idx}: {dev['name']}"] = idx
        except Exception:
            pass
        return devs

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 5))

        title = ctk.CTkLabel(header, text="Ассистент «Вася»", font=ctk.CTkFont(size=22, weight="bold"))
        title.pack(side="left")

        self.status_indicator = ctk.CTkLabel(header, text="● ИНИЦИАЛИЗАЦИЯ", font=ctk.CTkFont(size=12, weight="bold"),
                                             text_color="#757575")
        self.status_indicator.pack(side="right", padx=5)

        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True, padx=20, pady=(5, 15))

        self.tab_cmds = self.tabview.add("⚡ Команды")
        self.tab_plans = self.tabview.add("📋 Планы")
        self.tab_ds = self.tabview.add("💬 Discord")
        self.tab_settings = self.tabview.add("⚙️ Настройки")

        self._build_commands_tab()
        self._build_plans_tab()
        self._build_discord_tab()
        self._build_settings_tab()

    # --- ВКЛАДКА 1: КОМАНДЫ (С МЕТКОЙ ИГРЫ) ---
    def _build_commands_tab(self):
        add_frame = ctk.CTkFrame(self.tab_cmds)
        add_frame.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(add_frame, text="Фразы вызова (через запятую):").grid(row=0, column=0, sticky="w", padx=10,
                                                                           pady=(10, 2))
        self.cmd_alias_entry = ctk.CTkEntry(add_frame, placeholder_text="кс, cs, контра")
        self.cmd_alias_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))

        ctk.CTkLabel(add_frame, text="Путь к файлу / Steam URI / URL:").grid(row=2, column=0, sticky="w", padx=10,
                                                                             pady=(0, 2))
        p_row = ctk.CTkFrame(add_frame, fg_color="transparent")
        p_row.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))

        self.cmd_target_entry = ctk.CTkEntry(p_row, placeholder_text="steam://rungameid/730 или C:\\Games\\game.exe")
        self.cmd_target_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        browse_btn = ctk.CTkButton(p_row, text="Обзор", width=80, command=self._browse_command_target)
        browse_btn.pack(side="right")

        # Настройки игры и процесса
        game_options_row = ctk.CTkFrame(add_frame, fg_color="transparent")
        game_options_row.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

        self.is_game_checkbox = ctk.CTkCheckBox(
            game_options_row, text="🎮 Это игра (блокировать сообщения Discord во время работы)",
            command=self._toggle_game_proc_state
        )
        self.is_game_checkbox.pack(side="left", padx=(0, 15))

        self.cmd_proc_entry = ctk.CTkEntry(game_options_row, placeholder_text="Имя процесса (например: cs2.exe)",
                                           width=230)
        self.cmd_proc_entry.pack(side="right")

        add_frame.columnconfigure(0, weight=1)

        add_btn = ctk.CTkButton(add_frame, text="+ Добавить команду", command=self._add_command, fg_color="#6A1B9A",
                                hover_color="#4A148C")
        add_btn.grid(row=5, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

        self.cmds_scroll = ctk.CTkScrollableFrame(self.tab_cmds, height=270)
        self.cmds_scroll.pack(fill="both", expand=True, padx=10, pady=5)

    def _toggle_game_proc_state(self):
        if not self.is_game_checkbox.get():
            self.cmd_proc_entry.delete(0, "end")

    def _browse_command_target(self):
        f = filedialog.askopenfilename(
            title="Выберите исполняемый файл",
            filetypes=[("Исполняемые файлы", "*.exe *.bat *.cmd"), ("Все файлы", "*.*")]
        )
        if f:
            norm_path = os.path.normpath(f)
            self.cmd_target_entry.delete(0, "end")
            self.cmd_target_entry.insert(0, norm_path)

            # Автоматически заполняем имя процесса
            base_name = os.path.basename(norm_path).lower()
            if base_name.endswith(".exe"):
                self.cmd_proc_entry.delete(0, "end")
                self.cmd_proc_entry.insert(0, base_name)
                self.is_game_checkbox.select()

    # --- ВКЛАДКА 2: МУЛЬТИ-ПЛАНЫ ---
    def _build_plans_tab(self):
        p_frame = ctk.CTkFrame(self.tab_plans)
        p_frame.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(p_frame, text="Название плана:").grid(row=0, column=0, sticky="w", padx=10, pady=(5, 2))
        self.plan_name_entry = ctk.CTkEntry(p_frame, placeholder_text="Дискорд с запретом")
        self.plan_name_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))

        ctk.CTkLabel(p_frame, text="Фразы вызова плана (через запятую):").grid(row=2, column=0, sticky="w", padx=10,
                                                                               pady=(0, 2))
        self.plan_aliases_entry = ctk.CTkEntry(p_frame, placeholder_text="дискорд, запусти дискорд, дс")
        self.plan_aliases_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))

        # Шаг 1
        ctk.CTkLabel(p_frame, text="Шаг 1 (например, .bat Запрета):").grid(row=4, column=0, sticky="w", padx=10,
                                                                           pady=(0, 2))
        s1_row = ctk.CTkFrame(p_frame, fg_color="transparent")
        s1_row.grid(row=5, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))
        self.plan_step1_entry = ctk.CTkEntry(s1_row, placeholder_text="C:\\zapret\\discord.bat")
        self.plan_step1_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(s1_row, text="Обзор", width=80, command=lambda: self._browse_into(self.plan_step1_entry)).pack(
            side="right")

        # Шаг 2
        ctk.CTkLabel(p_frame, text="Шаг 2 (например, Discord или игра):").grid(row=6, column=0, sticky="w", padx=10,
                                                                               pady=(0, 2))
        s2_row = ctk.CTkFrame(p_frame, fg_color="transparent")
        s2_row.grid(row=7, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        self.plan_step2_entry = ctk.CTkEntry(s2_row, placeholder_text="C:\\Users\\...\\Discord.exe")
        self.plan_step2_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(s2_row, text="Обзор", width=80, command=lambda: self._browse_into(self.plan_step2_entry)).pack(
            side="right")

        p_frame.columnconfigure(0, weight=1)

        add_plan_btn = ctk.CTkButton(p_frame, text="+ Сохранить план", command=self._add_plan, fg_color="#6A1B9A",
                                     hover_color="#4A148C")
        add_plan_btn.grid(row=8, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

        self.plans_scroll = ctk.CTkScrollableFrame(self.tab_plans, height=180)
        self.plans_scroll.pack(fill="both", expand=True, padx=10, pady=5)

    # --- ВКЛАДКА 3: DISCORD ---
    def _build_discord_tab(self):
        ds_frame = ctk.CTkFrame(self.tab_ds)
        ds_frame.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(ds_frame, text="Кому сказать (имя и синонимы через запятую):").grid(row=0, column=0, sticky="w",
                                                                                         padx=10, pady=(10, 2))
        self.ds_alias_entry = ctk.CTkEntry(ds_frame, placeholder_text="андрею, андрей, дрон")
        self.ds_alias_entry.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))

        ctk.CTkLabel(ds_frame, text="Точный ник в Discord (отображаемый или логин):").grid(row=2, column=0, sticky="w",
                                                                                           padx=10, pady=(0, 2))
        self.ds_tag_entry = ctk.CTkEntry(ds_frame, placeholder_text="Andrey_Best")
        self.ds_tag_entry.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))

        ds_frame.columnconfigure(0, weight=1)

        add_ds_btn = ctk.CTkButton(ds_frame, text="+ Добавить контакт Discord", command=self._add_contact,
                                   fg_color="#6A1B9A", hover_color="#4A148C")
        add_ds_btn.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 10))

        info_lbl = ctk.CTkLabel(
            self.tab_ds,
            text="🔒 ЗАЩИТА ОТ СВОРАЧИВАНИЯ: Если в '⚡ Команды' запущена игра с флагом [🎮 Игра],\n"
                 "сообщение НЕ отправится, а ассистент тихо предупредит звуком, чтобы не сбивать фокус.",
            text_color="#BA68C8", font=ctk.CTkFont(size=12, weight="bold")
        )
        info_lbl.pack(pady=(0, 5))

        self.ds_scroll = ctk.CTkScrollableFrame(self.tab_ds, height=220)
        self.ds_scroll.pack(fill="both", expand=True, padx=10, pady=5)

    # --- ВКЛАДКА 4: НАСТРОЙКИ ---
    def _build_settings_tab(self):
        sett_frame = ctk.CTkFrame(self.tab_settings, fg_color="#1E1E24")
        sett_frame.pack(fill="x", padx=10, pady=15)

        ctk.CTkLabel(sett_frame, text="Микрофон:", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w",
                                                                                         padx=10, pady=12)

        dev_names = list(self.audio_devices.keys()) or ["Микрофоны не найдены"]
        current_name = dev_names[0]
        saved_idx = self.config.get("device_index")
        for k, v in self.audio_devices.items():
            if v == saved_idx:
                current_name = k
                break

        self.mic_dropdown = ctk.CTkOptionMenu(
            sett_frame, values=dev_names, command=self._on_device_changed, width=320
        )
        self.mic_dropdown.set(current_name)
        self.mic_dropdown.grid(row=0, column=1, sticky="w", padx=5, pady=12)

        ctk.CTkLabel(sett_frame, text="Тест:").grid(row=0, column=2, sticky="e", padx=(10, 5), pady=12)
        self.meter = ctk.CTkProgressBar(sett_frame, width=150, progress_color="#6A1B9A")
        self.meter.set(0.0)
        self.meter.grid(row=0, column=3, sticky="w", padx=(0, 10), pady=12)

        help_box = ctk.CTkTextbox(self.tab_settings, height=220)
        help_box.pack(fill="both", expand=True, padx=10, pady=10)
        help_box.insert("end",
                        "СПРАВКА:\n"
                        "• Права администратора: Если запускать Васю от админа, любые дочерние батники (Запрет)\n"
                        "  запускаются с правами админа автоматически без окон UAC.\n"
                        "• Метка игры: Позволяет указать имя процесса (например, cs2.exe).\n"
                        "  Пока этот процесс запущен, ассистент блокирует сворачивание окон для сообщений Discord.\n"
                        )
        help_box.configure(state="disabled")

    # --- ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ---
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
        f = filedialog.askopenfilename(
            title="Выберите программу или файл",
            filetypes=[("Все исполняемые", "*.exe *.bat *.cmd"), ("Все файлы", "*.*")]
        )
        if f:
            entry_widget.delete(0, "end")
            entry_widget.insert(0, os.path.normpath(f))

    # --- СОХРАНЕНИЕ / УДАЛЕНИЕ ---
    def _add_command(self):
        raw_aliases = self.cmd_alias_entry.get().strip()
        target = self.cmd_target_entry.get().strip()
        is_game = bool(self.is_game_checkbox.get())
        proc_name = self.cmd_proc_entry.get().strip().lower()

        if not raw_aliases or not target:
            messagebox.showwarning("Внимание", "Заполните фразы и цель.")
            return

        aliases = [a.strip().lower() for a in raw_aliases.split(",") if a.strip()]

        self.config["commands"].append({
            "aliases": aliases,
            "target": target,
            "is_game": is_game,
            "process_name": proc_name
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

            is_game_tag = f" [🎮 Игра: {item.get('process_name')}]" if item.get("is_game") else ""
            title_txt = f"Фразы: {', '.join(item['aliases'])}{is_game_tag}"

            ctk.CTkLabel(tb, text=title_txt, font=ctk.CTkFont(weight="bold"),
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
            ctk.CTkLabel(tb, text=f"План: {item.get('name', '')} | Фразы: {', '.join(item['aliases'])}",
                         font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
            steps_txt = " ➔ ".join([os.path.basename(s) if os.path.exists(s) else s for s in item.get("steps", [])])
            ctk.CTkLabel(tb, text=f"Шаги: {steps_txt}", text_color="#BA68C8", anchor="w").pack(fill="x")
            ctk.CTkButton(card, text="Удалить", width=70, fg_color="#B71C1C", hover_color="#7F0000",
                          command=lambda i=idx: self._delete_item("plans", i)).pack(side="right", padx=10, pady=5)

        # 3. Контакты
        for w in self.ds_scroll.winfo_children():
            w.destroy()
        for idx, item in enumerate(self.config.get("contacts", [])):
            card = ctk.CTkFrame(self.ds_scroll, fg_color="#1E1E24")
            card.pack(fill="x", padx=5, pady=4)
            tb = ctk.CTkFrame(card, fg_color="transparent")
            tb.pack(side="left", fill="both", expand=True, padx=10, pady=5)
            ctk.CTkLabel(tb, text=f"Обращение: {', '.join(item['aliases'])}", font=ctk.CTkFont(weight="bold"),
                         anchor="w").pack(fill="x")
            ctk.CTkLabel(tb, text=f"Discord  Ник: {item['discord_tag']}", text_color="#64B5F6", anchor="w").pack(
                fill="x")
            ctk.CTkButton(card, text="Удалить", width=70, fg_color="#B71C1C", hover_color="#7F0000",
                          command=lambda i=idx: self._delete_item("contacts", i)).pack(side="right", padx=10, pady=5)

    def _delete_item(self, category: str, index: int):
        if 0 <= index < len(self.config[category]):
            self.config[category].pop(index)
            save_config(self.config)
            self._refresh_all_lists()


if __name__ == "__main__":
    app = AssistantApp()
    GUI_APP = app
    app.mainloop()