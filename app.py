import os
import sys
import time
import datetime
import subprocess
import threading
import re
from pathlib import Path

import customtkinter as ctk

# Base paths
if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(sys._MEIPASS)
    APP_DIR = Path(sys.executable).parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    APP_DIR = BUNDLE_DIR

BIN_DIR = BUNDLE_DIR / "bin"
RECORDINGS_DIR = APP_DIR / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)

ADB_PATH = BIN_DIR / "adb.exe"
SCRCPY_PATH = BIN_DIR / "scrcpy.exe"

# Ensure scrcpy finds adb and scrcpy-server even when packaged into a single exe
os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")
os.environ["ADB"] = str(ADB_PATH)
if (BIN_DIR / "scrcpy-server").exists():
    os.environ["SCRCPY_SERVER_PATH"] = str(BIN_DIR / "scrcpy-server")


class AdbManager:
    def __init__(self, adb_path: Path):
        self.adb_path = str(adb_path)

    def _run(self, *args, timeout=5) -> tuple[int, str]:
        try:
            cmd = [self.adb_path] + list(args)
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=creationflags
            )
            return res.returncode, res.stdout.strip()
        except Exception as e:
            return -1, str(e)

    def get_devices(self) -> list[dict]:
        code, out = self._run("devices", "-l")
        devices = []
        if code != 0 or not out:
            return devices

        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("*") or line.startswith("List of"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                serial = parts[0]
                status = parts[1]
                info = " ".join(parts[2:]) if len(parts) > 2 else ""
                devices.append({
                    "serial": serial,
                    "status": status,
                    "info": info,
                    "is_wireless": ":" in serial
                })
        return devices

    def get_battery(self, serial: str = None) -> str:
        args = ["-s", serial, "shell", "dumpsys", "battery"] if serial else ["shell", "dumpsys", "battery"]
        code, out = self._run(*args)
        if code == 0:
            match = re.search(r"level:\s*(\d+)", out)
            if match:
                return f"{match.group(1)}%"
        return "н/д"

    def get_device_ip(self, serial: str = None) -> str | None:
        args = ["-s", serial, "shell", "ip", "route"] if serial else ["shell", "ip", "route"]
        code, out = self._run(*args)
        if code == 0:
            # Look for: 192.168.x.x dev wlan0 src 192.168.x.y
            match = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", out)
            if match:
                return match.group(1)

        # Fallback to ip addr show wlan0
        args2 = ["-s", serial, "shell", "ip", "-f", "inet", "addr", "show", "wlan0"] if serial else ["shell", "ip", "-f", "inet", "addr", "show", "wlan0"]
        code2, out2 = self._run(*args2)
        if code2 == 0:
            match2 = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out2)
            if match2:
                return match2.group(1)
        return None

    def enable_tcpip(self, port=5555, serial: str = None) -> tuple[bool, str]:
        args = ["-s", serial, "tcpip", str(port)] if serial else ["tcpip", str(port)]
        code, out = self._run(*args)
        return (code == 0), out

    def connect_wireless(self, ip: str, port=5555) -> tuple[bool, str]:
        code, out = self._run("connect", f"{ip}:{port}")
        return ("connected to" in out.lower()), out

    def apply_quest_optimizations(self, fps: int = 60, width: int = 1920, bitrate: int = 30000000, serial: str = None):
        commands = [
            ["setprop", "debug.oculus.capture.fps", str(fps)],
            ["setprop", "debug.oculus.capture.width", str(width)],
            ["setprop", "debug.oculus.capture.bitrate", str(bitrate)],
            ["setprop", "debug.oculus.fullProcessTexturing", "1"]
        ]
        for subcmd in commands:
            args = ["-s", serial, "shell"] + subcmd if serial else ["shell"] + subcmd
            self._run(*args)


class QuestMirrorApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Window configuration
        self.title("Meta Quest 3S Mirror Studio (60+ FPS)")
        self.geometry("980x700")
        self.minsize(880, 620)
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        self.adb = AdbManager(ADB_PATH)
        self.scrcpy_proc: subprocess.Popen | None = None
        self.is_monitoring = True
        self.current_device = None

        self._build_ui()
        self._start_status_loop()

    def _build_ui(self):
        # Grid config
        self.grid_columnconfigure(0, weight=0)  # Left panel (Controls)
        self.grid_columnconfigure(1, weight=1)  # Right panel (Settings & Tabs)
        self.grid_rowconfigure(0, weight=1)

        # ------------------ LEFT SIDEBAR ------------------
        self.sidebar = ctk.CTkFrame(self, width=280, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self.sidebar.grid_rowconfigure(8, weight=1)

        # Logo / Title
        self.logo_label = ctk.CTkLabel(
            self.sidebar,
            text="Meta Quest 3S\nMirror Studio",
            font=ctk.CTkFont(size=20, weight="bold")
        )
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        # Device Status Card
        self.device_card = ctk.CTkFrame(self.sidebar, corner_radius=10, fg_color="#1E1E24")
        self.device_card.grid(row=1, column=0, padx=15, pady=10, sticky="ew")

        self.status_indicator = ctk.CTkLabel(
            self.device_card,
            text="● Поиск шлема...",
            text_color="#F39C12",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.status_indicator.pack(anchor="w", padx=12, pady=(10, 2))

        self.device_details_label = ctk.CTkLabel(
            self.device_card,
            text="Кабель: Не подключен\nБатарея: --",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color="#A0A0A0"
        )
        self.device_details_label.pack(anchor="w", padx=12, pady=(0, 10))

        # Refresh button
        self.btn_refresh = ctk.CTkButton(
            self.sidebar,
            text="🔄 Проверить подключение",
            command=self.refresh_device_status,
            height=32,
            fg_color="#34495E",
            hover_color="#2C3E50"
        )
        self.btn_refresh.grid(row=2, column=0, padx=15, pady=5, sticky="ew")

        # Switch to Wi-Fi button
        self.btn_wifi = ctk.CTkButton(
            self.sidebar,
            text="📶 Включить Wi-Fi режим",
            command=self.switch_to_wifi,
            height=34,
            fg_color="#2980B9",
            hover_color="#1F618D"
        )
        self.btn_wifi.grid(row=3, column=0, padx=15, pady=5, sticky="ew")

        # Separator
        self.sep = ctk.CTkFrame(self.sidebar, height=2, fg_color="#2C3E50")
        self.sep.grid(row=4, column=0, padx=15, pady=15, sticky="ew")

        # Stream Control Buttons
        self.btn_start = ctk.CTkButton(
            self.sidebar,
            text="▶ ЗАПУСК ТРАНСЛЯЦИИ",
            font=ctk.CTkFont(size=15, weight="bold"),
            height=46,
            fg_color="#27AE60",
            hover_color="#219150",
            command=self.start_stream
        )
        self.btn_start.grid(row=5, column=0, padx=15, pady=8, sticky="ew")

        self.btn_stop = ctk.CTkButton(
            self.sidebar,
            text="⏹ ОСТАНОВИТЬ",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=38,
            fg_color="#C0392B",
            hover_color="#962D22",
            state="disabled",
            command=self.stop_stream
        )
        self.btn_stop.grid(row=6, column=0, padx=15, pady=5, sticky="ew")

        # Quick Presets Label
        self.preset_label = ctk.CTkLabel(
            self.sidebar,
            text="Быстрые пресеты:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#888888"
        )
        self.preset_label.grid(row=7, column=0, padx=15, pady=(15, 5), sticky="w")

        self.preset_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.preset_frame.grid(row=8, column=0, padx=15, pady=0, sticky="new")

        self.btn_preset_60 = ctk.CTkButton(
            self.preset_frame,
            text="⚡ Плавный летсплей (60 FPS 16:9)",
            height=28,
            fg_color="#2A2F3D",
            hover_color="#3B4254",
            command=lambda: self.apply_preset("60_stream")
        )
        self.btn_preset_60.pack(fill="x", pady=3)

        self.btn_preset_90 = ctk.CTkButton(
            self.preset_frame,
            text="🥽 Максимум VR (90 FPS 40M)",
            height=28,
            fg_color="#2A2F3D",
            hover_color="#3B4254",
            command=lambda: self.apply_preset("90_max")
        )
        self.btn_preset_90.pack(fill="x", pady=3)

        self.btn_preset_wifi = ctk.CTkButton(
            self.preset_frame,
            text="📶 Wi-Fi Баланс (H.265 / 60 FPS)",
            height=28,
            fg_color="#2A2F3D",
            hover_color="#3B4254",
            command=lambda: self.apply_preset("wifi_opt")
        )
        self.btn_preset_wifi.pack(fill="x", pady=3)

        # Version & Credits at bottom
        self.footer_label = ctk.CTkLabel(
            self.sidebar,
            text="scrcpy v5.0.1 | Quest 3S Edition\nNvidia NVDEC Ultra-Low Latency",
            font=ctk.CTkFont(size=10),
            text_color="#555555"
        )
        self.footer_label.grid(row=9, column=0, padx=15, pady=10)

        # ------------------ RIGHT MAIN AREA (TABVIEW) ------------------
        self.tabview = ctk.CTkTabview(self, corner_radius=10)
        self.tabview.grid(row=0, column=1, padx=15, pady=15, sticky="nsew")

        self.tab_video = self.tabview.add("🎥 Настройки видео")
        self.tab_audio = self.tabview.add("⚙ Окно и Звук")
        self.tab_help = self.tabview.add("📖 Инструкция к шлему")
        self.tab_logs = self.tabview.add("📝 Логи")

        self._setup_video_tab()
        self._setup_window_audio_tab()
        self._setup_help_tab()
        self._setup_logs_tab()

    def _setup_video_tab(self):
        tab = self.tab_video
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=1)

        # Eye Mode / Crop
        lbl_eye = ctk.CTkLabel(tab, text="Режим обзора (Глаз / Кадрирование):", font=ctk.CTkFont(weight="bold"))
        lbl_eye.grid(row=0, column=0, columnspan=2, sticky="w", padx=15, pady=(10, 5))

        self.var_eye_mode = ctk.StringVar(value="Правый глаз (16:9 — Стримы / YouTube)")
        self.combo_eye_mode = ctk.CTkComboBox(
            tab,
            values=[
                "Правый глаз (16:9 — Стримы / YouTube)",
                "Правый глаз (1:1 — Полный угол обзора)",
                "Левый глаз (16:9 — Стримы / YouTube)",
                "Левый глаз (1:1 — Полный угол обзора)",
                "Оба глаза (Стерео / Без обрезки)",
                "Пользовательский crop"
            ],
            variable=self.var_eye_mode,
            command=self._on_eye_mode_changed,
            height=34
        )
        self.combo_eye_mode.grid(row=1, column=0, columnspan=2, sticky="ew", padx=15, pady=(0, 10))

        # Custom Crop Field
        self.var_custom_crop = ctk.StringVar(value="1600:900:2017:510")
        self.entry_custom_crop = ctk.CTkEntry(
            tab,
            textvariable=self.var_custom_crop,
            placeholder_text="Формат: ширина:высота:смещение_x:смещение_y",
            state="disabled",
            height=32
        )
        self.entry_custom_crop.grid(row=2, column=0, columnspan=2, sticky="ew", padx=15, pady=(0, 15))

        # FPS Selection
        lbl_fps = ctk.CTkLabel(tab, text="Частота кадров (FPS):", font=ctk.CTkFont(weight="bold"))
        lbl_fps.grid(row=3, column=0, sticky="w", padx=15, pady=(5, 5))

        self.var_fps = ctk.StringVar(value="60 FPS (Стандарт)")
        self.combo_fps = ctk.CTkComboBox(
            tab,
            values=["60 FPS (Стандарт)", "72 FPS (Нативно Quest)", "90 FPS (Максимальная плавность)", "120 FPS (Экстремальный)"],
            variable=self.var_fps,
            height=34
        )
        self.combo_fps.grid(row=4, column=0, sticky="ew", padx=15, pady=(0, 15))

        # Bitrate Selection
        lbl_bitrate = ctk.CTkLabel(tab, text="Битрейт видеопотока:", font=ctk.CTkFont(weight="bold"))
        lbl_bitrate.grid(row=3, column=1, sticky="w", padx=15, pady=(5, 5))

        self.var_bitrate = ctk.StringVar(value="30 Мбит/с (Оптимально)")
        self.combo_bitrate = ctk.CTkComboBox(
            tab,
            values=["15 Мбит/с (Wi-Fi эконом)", "30 Мбит/с (Оптимально)", "45 Мбит/с (Высокое качество)", "60 Мбит/с (Ультра USB 3)"],
            variable=self.var_bitrate,
            height=34
        )
        self.combo_bitrate.grid(row=4, column=1, sticky="ew", padx=15, pady=(0, 15))

        # Codec Selection
        lbl_codec = ctk.CTkLabel(tab, text="Видеокодек (Аппаратный энкодер):", font=ctk.CTkFont(weight="bold"))
        lbl_codec.grid(row=5, column=0, sticky="w", padx=15, pady=(5, 5))

        self.var_codec = ctk.StringVar(value="H.265 / HEVC (Рекомендуется)")
        self.combo_codec = ctk.CTkComboBox(
            tab,
            values=["H.265 / HEVC (Рекомендуется)", "H.264 (Совместимость)", "AV1 (Quest 3S + RTX 4050)"],
            variable=self.var_codec,
            height=34
        )
        self.combo_codec.grid(row=6, column=0, sticky="ew", padx=15, pady=(0, 15))

        # Max Size (Resolution)
        lbl_res = ctk.CTkLabel(tab, text="Максимальное разрешение:", font=ctk.CTkFont(weight="bold"))
        lbl_res.grid(row=5, column=1, sticky="w", padx=15, pady=(5, 5))

        self.var_res = ctk.StringVar(value="1080p (1920)")
        self.combo_res = ctk.CTkComboBox(
            tab,
            values=["1080p (1920)", "1440p (2560)", "Без ограничений (Native)"],
            variable=self.var_res,
            height=34
        )
        self.combo_res.grid(row=6, column=1, sticky="ew", padx=15, pady=(0, 15))

        # Quest optimization toggle
        self.var_quest_opt = ctk.BooleanVar(value=True)
        self.chk_quest_opt = ctk.CTkCheckBox(
            tab,
            text="Применять оптимизации захвата Quest 3S перед запуском (ADB setprop)",
            variable=self.var_quest_opt
        )
        self.chk_quest_opt.grid(row=7, column=0, columnspan=2, sticky="w", padx=15, pady=(15, 10))

    def _setup_window_audio_tab(self):
        tab = self.tab_audio
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=1)

        # Audio section
        lbl_audio = ctk.CTkLabel(tab, text="Настройки звука:", font=ctk.CTkFont(weight="bold"))
        lbl_audio.grid(row=0, column=0, sticky="w", padx=15, pady=(10, 5))

        self.var_audio = ctk.BooleanVar(value=True)
        self.chk_audio = ctk.CTkCheckBox(
            tab,
            text="Передавать звук со шлема на ноутбук (AAC / Opus)",
            variable=self.var_audio
        )
        self.chk_audio.grid(row=1, column=0, columnspan=2, sticky="w", padx=15, pady=5)

        # Window controls
        lbl_win = ctk.CTkLabel(tab, text="Настройки окна трансляции:", font=ctk.CTkFont(weight="bold"))
        lbl_win.grid(row=2, column=0, sticky="w", padx=15, pady=(15, 5))

        self.var_fullscreen = ctk.BooleanVar(value=False)
        self.chk_fullscreen = ctk.CTkCheckBox(
            tab,
            text="Полноэкранный режим при старте (F11 для выхода)",
            variable=self.var_fullscreen
        )
        self.chk_fullscreen.grid(row=3, column=0, sticky="w", padx=15, pady=5)

        self.var_always_on_top = ctk.BooleanVar(value=False)
        self.chk_always_on_top = ctk.CTkCheckBox(
            tab,
            text="Окно поверх других приложений",
            variable=self.var_always_on_top
        )
        self.chk_always_on_top.grid(row=3, column=1, sticky="w", padx=15, pady=5)

        self.var_stay_awake = ctk.BooleanVar(value=True)
        self.chk_stay_awake = ctk.CTkCheckBox(
            tab,
            text="Предотвращать засыпание шлема при бездействии",
            variable=self.var_stay_awake
        )
        self.chk_stay_awake.grid(row=4, column=0, sticky="w", padx=15, pady=5)

        self.var_borderless = ctk.BooleanVar(value=False)
        self.chk_borderless = ctk.CTkCheckBox(
            tab,
            text="Без рамки окна (Borderless)",
            variable=self.var_borderless
        )
        self.chk_borderless.grid(row=4, column=1, sticky="w", padx=15, pady=5)

        # Recording Section
        lbl_rec = ctk.CTkLabel(tab, text="Запись геймплея:", font=ctk.CTkFont(weight="bold"))
        lbl_rec.grid(row=5, column=0, sticky="w", padx=15, pady=(15, 5))

        self.var_record = ctk.BooleanVar(value=False)
        self.chk_record = ctk.CTkCheckBox(
            tab,
            text="Автоматически записывать видео в файл MP4",
            variable=self.var_record
        )
        self.chk_record.grid(row=6, column=0, sticky="w", padx=15, pady=5)

        btn_open_recordings = ctk.CTkButton(
            tab,
            text="📁 Открыть папку с записями",
            width=180,
            command=self._open_recordings_folder
        )
        btn_open_recordings.grid(row=6, column=1, sticky="w", padx=15, pady=5)

    def _setup_help_tab(self):
        tab = self.tab_help
        scroll = ctk.CTkScrollableFrame(tab)
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        guide_text = """
### 🚀 Инструкция: Как подготовить Meta Quest 3S

#### 1. Включение Режима разработчика (нужно сделать 1 раз)
1. Установите на телефон приложение **Meta Horizon** и войдите в свой аккаунт Meta.
2. Перейдите на сайт **developer.oculus.com**, залогиньтесь под тем же аккаунтом и создайте любую тестовую "Организацию" (название любое, например `MyVRHome`). Это бесплатно и занимает 1 минуту.
3. В приложении Meta Horizon на телефоне:
   - Откройте `Меню` → `Устройства` (или выберите шлем Quest 3S).
   - Нажмите `Настройки гарнитуры` → `Режим разработчика`.
   - Переведите тумблер в положение **ВКЛ**.
   - Перезагрузите шлем.

#### 2. Первое подключение по кабелю
1. Включите шлем и подключите его к ноутбуку кабелем Type-C.
2. Наденьте шлем! Вы увидите всплывающее окно: **"Разрешить отладку по USB?"**
3. Обязательно поставьте галочку **"Всегда разрешать с этого компьютера"** и нажмите **"Разрешить"**.
4. После этого в программе статус шлема станет зеленым: **"● Meta Quest 3S подключен"**.

#### 3. Беспроводной режим (Wi-Fi)
1. Подключите шлем кабелем.
2. Убедитесь, что шлем и ноутбук находятся в одной сети Wi-Fi (рекомендуется 5 ГГц).
3. Нажмите синюю кнопку **"📶 Включить Wi-Fi режим"**.
4. Программа автоматически подключится к шлему по сети.
5. Отсоедините кабель — видеопоток будет идти по воздуху!

#### 🎮 Полезные горячие клавиши во время трансляции:
- **F11** или **Alt + F** — Развернуть на весь экран / выйти из полноэкранного режима.
- **Alt + W** — Подогнать окно без черных рамок.
- **Alt + O** — Выключить дисплей шлема (картинка на мониторе останется, а батарея шлема будет садиться в 3 раза медленнее!).
- **Alt + Shift + O** — Включить дисплей шлема обратно.
- **Alt + I** — Включить/выключить счетчик FPS в консоли.
        """
        lbl = ctk.CTkLabel(scroll, text=guide_text.strip(), justify="left", font=ctk.CTkFont(size=13))
        lbl.pack(anchor="w", padx=10, pady=10)

    def _setup_logs_tab(self):
        tab = self.tab_logs
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        self.txt_logs = ctk.CTkTextbox(tab, font=ctk.CTkFont(family="Consolas", size=12))
        self.txt_logs.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        btn_clear = ctk.CTkButton(tab, text="Очистить лог", width=120, command=lambda: self.txt_logs.delete("1.0", "end"))
        btn_clear.grid(row=1, column=0, sticky="e", padx=10, pady=(0, 10))

    def log(self, text: str):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        msg = f"[{timestamp}] {text}\n"
        self.txt_logs.insert("end", msg)
        self.txt_logs.see("end")

    def _on_eye_mode_changed(self, choice):
        if choice == "Пользовательский crop":
            self.entry_custom_crop.configure(state="normal")
        else:
            self.entry_custom_crop.configure(state="disabled")

    def apply_preset(self, preset_name: str):
        if preset_name == "60_stream":
            self.var_eye_mode.set("Правый глаз (16:9 — Стримы / YouTube)")
            self.var_fps.set("60 FPS (Стандарт)")
            self.var_bitrate.set("30 Мбит/с (Оптимально)")
            self.var_codec.set("H.265 / HEVC (Рекомендуется)")
            self.var_res.set("1080p (1920)")
            self.log("Применён пресет: Плавный летсплей (60 FPS, 16:9, H.265, 30M)")
        elif preset_name == "90_max":
            self.var_eye_mode.set("Правый глаз (16:9 — Стримы / YouTube)")
            self.var_fps.set("90 FPS (Максимальная плавность)")
            self.var_bitrate.set("45 Мбит/с (Высокое качество)")
            self.var_codec.set("H.265 / HEVC (Рекомендуется)")
            self.var_res.set("1440p (2560)")
            self.log("Применён пресет: Максимум VR (90 FPS, 1440p, 45M)")
        elif preset_name == "wifi_opt":
            self.var_eye_mode.set("Правый глаз (16:9 — Стримы / YouTube)")
            self.var_fps.set("60 FPS (Стандарт)")
            self.var_bitrate.set("20 Мбит/с (Wi-Fi эконом)")
            self.var_codec.set("H.265 / HEVC (Рекомендуется)")
            self.var_res.set("1080p (1920)")
            self.log("Применён пресет: Wi-Fi Баланс (60 FPS, H.265, 20M)")
        self._on_eye_mode_changed(self.var_eye_mode.get())

    def _open_recordings_folder(self):
        if sys.platform == "win32":
            os.startfile(RECORDINGS_DIR)

    def _start_status_loop(self):
        def loop():
            while self.is_monitoring:
                self.refresh_device_status(silent=True)
                time.sleep(3)

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    def refresh_device_status(self, silent=False):
        devices = self.adb.get_devices()
        if not devices:
            self.current_device = None
            self.status_indicator.configure(text="● Шлем не подключен", text_color="#E74C3C")
            self.device_details_label.configure(
                text="Подключите Quest 3S кабелем USB-C\nили проверьте Wi-Fi"
            )
            if not silent:
                self.log("Устройства не найдены. Убедитесь, что шлем включен и подключен.")
            return

        dev = devices[0]
        self.current_device = dev
        status = dev["status"]
        serial = dev["serial"]
        is_wireless = dev["is_wireless"]

        if status == "unauthorized":
            self.status_indicator.configure(text="● Требуется авторизация!", text_color="#F39C12")
            self.device_details_label.configure(
                text="Наденьте шлем и нажмите 'Разрешить отладку'\nГалочка: 'Всегда разрешать'"
            )
            if not silent:
                self.log("Шлем найден, но ожидает подтверждения отладки внутри VR!")
        elif status == "device":
            conn_type = "Wi-Fi Беспроводной" if is_wireless else "Кабель USB"
            battery = self.adb.get_battery(serial)
            self.status_indicator.configure(text="● Meta Quest 3S готов к трансляции", text_color="#2ECC71")
            self.device_details_label.configure(
                text=f"Связь: {conn_type} ({serial})\nБатарея: {battery}"
            )
            if not silent:
                self.log(f"Шлем подключен: {serial} | Тип: {conn_type} | Батарея: {battery}")

    def switch_to_wifi(self):
        if not self.current_device or self.current_device["status"] != "device":
            self.log("Ошибка: Сначала подключите шлем кабелем и разрешите отладку!")
            return

        serial = self.current_device["serial"]
        if self.current_device["is_wireless"]:
            self.log("Шлем уже подключен по Wi-Fi!")
            return

        self.log("Определение IP адреса шлема...")
        ip = self.adb.get_device_ip(serial)
        if not ip:
            self.log("Не удалось определить IP шлема. Убедитесь, что шлем подключен к Wi-Fi!")
            return

        self.log(f"IP шлема найден: {ip}. Включение порта 5555...")
        ok_tcp, out_tcp = self.adb.enable_tcpip(5555, serial)
        if not ok_tcp:
            self.log(f"Ошибка tcpip: {out_tcp}")
            return

        time.sleep(1)
        self.log(f"Подключение к {ip}:5555...")
        ok_conn, out_conn = self.adb.connect_wireless(ip, 5555)
        if ok_conn:
            self.log(f"🎉 Успешно подключено по Wi-Fi к {ip}!")
            self.log("Теперь можно отсоединить кабель USB Type-C!")
            self.refresh_device_status()
        else:
            self.log(f"Не удалось подключиться: {out_conn}")

    def _get_crop_argument(self) -> str | None:
        mode = self.var_eye_mode.get()
        if "Правый глаз (16:9" in mode:
            # Quest 3S right eye centered 16:9 crop
            return "1600:900:2017:510"
        elif "Правый глаз (1:1" in mode:
            # Full native right eye
            return "1832:1920:1832:0"
        elif "Левый глаз (16:9" in mode:
            # Quest 3S left eye centered 16:9 crop
            return "1600:900:185:510"
        elif "Левый глаз (1:1" in mode:
            # Full native left eye
            return "1832:1920:0:0"
        elif "Пользовательский" in mode:
            val = self.var_custom_crop.get().strip()
            return val if val else None
        else:
            # Both eyes stereo
            return None

    def start_stream(self):
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            self.log("Трансляция уже запущена!")
            return

        if not self.current_device or self.current_device["status"] != "device":
            self.log("Шлем не подключен или не авторизован! Подключите шлем перед запуском.")
            return

        serial = self.current_device["serial"]

        # Parse settings
        fps_map = {
            "60 FPS (Стандарт)": 60,
            "72 FPS (Нативно Quest)": 72,
            "90 FPS (Максимальная плавность)": 90,
            "120 FPS (Экстремальный)": 120
        }
        fps_val = fps_map.get(self.var_fps.get(), 60)

        bitrate_map = {
            "15 Мбит/с (Wi-Fi эконом)": "15M",
            "30 Мбит/с (Оптимально)": "30M",
            "45 Мбит/с (Высокое качество)": "45M",
            "60 Мбит/с (Ультра USB 3)": "60M"
        }
        bitrate_val = bitrate_map.get(self.var_bitrate.get(), "30M")

        codec_map = {
            "H.265 / HEVC (Рекомендуется)": "h265",
            "H.264 (Совместимость)": "h264",
            "AV1 (Quest 3S + RTX 4050)": "av1"
        }
        codec_val = codec_map.get(self.var_codec.get(), "h265")

        res_map = {
            "1080p (1920)": 1920,
            "1440p (2560)": 2560,
            "Без ограничений (Native)": 0
        }
        res_val = res_map.get(self.var_res.get(), 1920)

        # Apply Quest capture optimizations via ADB
        if self.var_quest_opt.get():
            self.log(f"Применение настроек Quest: FPS={fps_val}, MaxRes={res_val or 1920}, Bitrate={bitrate_val}...")
            bitrate_int = int(bitrate_val.replace("M", "")) * 1000000
            self.adb.apply_quest_optimizations(fps=fps_val, width=res_val or 1920, bitrate=bitrate_int, serial=serial)

        # Build scrcpy arguments
        cmd = [str(SCRCPY_PATH)]
        if serial:
            cmd.extend(["-s", serial])

        # FPS & Bitrate
        cmd.append(f"--max-fps={fps_val}")
        cmd.append(f"--video-bit-rate={bitrate_val}")
        cmd.append(f"--video-codec={codec_val}")

        # Resolution
        if res_val > 0:
            cmd.append(f"--max-size={res_val}")

        # Crop
        crop = self._get_crop_argument()
        if crop:
            cmd.append(f"--crop={crop}")

        # Audio
        if self.var_audio.get():
            cmd.append("--audio-source=playback")
            cmd.append("--audio-dup")
        else:
            cmd.append("--no-audio")

        # Window flags
        if self.var_fullscreen.get():
            cmd.append("--fullscreen")
        if self.var_always_on_top.get():
            cmd.append("--always-on-top")
        if self.var_stay_awake.get():
            cmd.append("--stay-awake")
        if self.var_borderless.get():
            cmd.append("--window-borderless")

        cmd.append("--window-title=Meta Quest 3S (60 FPS Stream)")
        cmd.append("--print-fps")

        # Recording
        if self.var_record.get():
            rec_name = f"quest_stream_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            rec_path = RECORDINGS_DIR / rec_name
            cmd.append(f"--record={rec_path}")
            self.log(f"Включена запись в файл: {rec_name}")

        self.log(f"Запуск трансляции с параметрами: FPS={fps_val}, Битрейт={bitrate_val}, Кодек={codec_val}...")

        try:
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.scrcpy_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags
            )

            self.btn_start.configure(state="disabled", fg_color="#555555")
            self.btn_stop.configure(state="normal", fg_color="#C0392B")

            # Thread to read scrcpy output
            def read_output():
                for line in self.scrcpy_proc.stdout:
                    line = line.strip()
                    if line:
                        self.log(f"[scrcpy] {line}")
                self.scrcpy_proc.wait()
                self.log(f"Трансляция завершена (код: {self.scrcpy_proc.returncode})")
                self.btn_start.configure(state="normal", fg_color="#27AE60")
                self.btn_stop.configure(state="disabled", fg_color="#555555")

            threading.Thread(target=read_output, daemon=True).start()

        except Exception as e:
            self.log(f"Ошибка запуска scrcpy: {e}")

    def stop_stream(self):
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            self.log("Остановка трансляции...")
            self.scrcpy_proc.terminate()
            try:
                self.scrcpy_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.scrcpy_proc.kill()
            self.btn_start.configure(state="normal", fg_color="#27AE60")
            self.btn_stop.configure(state="disabled", fg_color="#555555")
            self.log("Трансляция остановлена.")

    def destroy(self):
        self.is_monitoring = False
        self.stop_stream()
        super().destroy()


if __name__ == "__main__":
    app = QuestMirrorApp()
    app.mainloop()
