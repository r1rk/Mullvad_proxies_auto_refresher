import os
import sys
import json
import time
import random
import argparse
import tempfile
import sys
import ctypes
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from typing import List, Optional, Callable, Dict, Tuple


# ネットワーク通信用
import requests

# PyQt6のインポートを試みる（CLI環境での動作を保証するため）
try:
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QLineEdit, QPushButton, QCheckBox, 
        QTextEdit, QProgressBar, QSpinBox, QDoubleSpinBox, QMessageBox, QGroupBox, QGridLayout
    )
    from PyQt6.QtCore import QThread, pyqtSignal, QTimer
    from PyQt6.QtGui import QFont
    HAS_PYQT6 = True
except ImportError:
    HAS_PYQT6 = False

# ---------------------------------------------------------
# 1. 共通定数・データクラス
# ---------------------------------------------------------
CONFIG_FILE = "config_mp.json"
MULLVAD_API_URL = "https://api.mullvad.net/www/relays/wireguard/"

@dataclass
class AppConfig:
    """アプリケーションの設定を保持するデータクラス"""
    output_base_name: str = "mullvad_proxies"   # 保存するベースファイル名（拡張子なし）
    output_socks5h: bool = True                 # socks5h:// 形式を出力するか
    output_socks5: bool = True                  # socks5:// 形式を出力するか
    output_raw: bool = True                     # スキームなし (hostname:port) 形式を出力するか
    countries: str = ""                         # カンマ区切りの国コード (例: "jp,us")
    target_url: str = "https://icanhazip.com/"  # ヘルスチェック用URL
    interval_minutes: int = 0                   # 定期実行間隔（分）。0は単発実行
    max_workers: int = 64                       # 同時検証スレッド数（デフォルト: 64）
    timeout_seconds: float = 2.0                # 検証タイムアウト秒数

class ConfigManager:
    """設定ファイルの読み込みと保存を担当するクラス"""
    @staticmethod
    def load() -> AppConfig:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    valid_keys = AppConfig.__annotations__.keys()
                    filtered_data = {k: v for k, v in data.items() if k in valid_keys}
                    return AppConfig(**filtered_data)
            except Exception as e:
                print(f"[警告] config.json の読み込みに失敗しました。デフォルト値を使用します。({e})")
        return AppConfig()

    @staticmethod
    def save(config: AppConfig):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(asdict(config), f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[エラー] 設定の保存に失敗しました: {e}")

# ---------------------------------------------------------
# 2. コアエンジン (抽出・変換・検証・保存)
# ---------------------------------------------------------
class ProxyPipeline:
    """API取得から検証・保存までの一連の処理を行うパイプライン"""

    def __init__(self, config: AppConfig, log_callback: Optional[Callable[[str], None]] = None):
        self.config = config
        self.log_callback = log_callback
        self._cancel_requested = False

    def cancel(self):
        """処理の中断をリクエストする"""
        self._cancel_requested = True

    def run(self, progress_callback: Optional[Callable[[int, int, str], None]] = None) -> List[str]:
        """パイプライン実行メイン"""
        self._cancel_requested = False
        _log = self.log_callback or print

        _log("Mullvad APIからサーバーリストを取得中...")
        raw_proxies = self._fetch_relays()

        if not raw_proxies:
            _log("有効なサーバーが見つかりませんでした。")
            return []

        if self._cancel_requested:
            return []

        # 国フィルタリング
        if self.config.countries:
            target_countries = [c.strip().lower() for c in self.config.countries.split(",") if c.strip()]
            filtered_proxies = [
                p for p in raw_proxies 
                if any(f"-{c}-" in p.lower() or f"-{c}" in p.lower() for c in target_countries)
            ]
            _log(f"国フィルタ適用後: {len(filtered_proxies)} / {len(raw_proxies)} 件")
            proxies_to_check = filtered_proxies
        else:
            proxies_to_check = raw_proxies

        total = len(proxies_to_check)
        _log(f"プロキシの検証を開始します (同時スレッド数: {self.config.max_workers}, タイムアウト: {self.config.timeout_seconds}s)...")

        valid_proxies = []
        completed = 0
        error_stats: Dict[str, int] = {}

        # ThreadPoolExecutorによる並列実行
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            future_to_proxy = {executor.submit(self._validate_proxy, p): p for p in proxies_to_check}
            
            for future in as_completed(future_to_proxy):
                if self._cancel_requested:
                    _log("処理がキャンセルされました。")
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                proxy_addr = future_to_proxy[future]
                completed += 1
                try:
                    exit_ip, err_reason = future.result()
                    if exit_ip:
                        valid_proxies.append(proxy_addr)
                    else:
                        err_key = err_reason or "Unknown"
                        error_stats[err_key] = error_stats.get(err_key, 0) + 1
                except Exception as e:
                    error_stats["Exception"] = error_stats.get("Exception", 0) + 1

                if progress_callback:
                    progress_callback(completed, total, proxy_addr)

        if not self._cancel_requested:
            _log("-" * 60)
            err_summary = ", ".join([f"{k}: {v}" for k, v in error_stats.items()]) if error_stats else "全件成功"
            _log(f"検証完了: {len(valid_proxies)}/{total} 件が有効でした。 [不合格内訳 -> {err_summary}]")
            self._save_to_file(valid_proxies)
            _log(f"結果を {self.config.output_base_name}_*.txt に保存しました。")

        return valid_proxies

    def _fetch_relays(self) -> List[str]:
        """APIからリレー一覧を取得し、SOCKS5アドレスのリストを生成"""
        try:
            resp = requests.get(MULLVAD_API_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"[エラー] API取得失敗: {e}")
            else:
                print(f"[エラー] API取得失敗: {e}")
            return []

        proxy_list = []
        for relay in data:
            if not relay.get("active"):
                continue

            hostname = relay.get("hostname", "")
            if hostname:
                socks5_domain = f"{hostname.replace('wg-', 'wg-socks5-')}.relays.mullvad.net"
                proxy_list.append(f"{socks5_domain}:1080")

        return proxy_list

    def _validate_proxy(self, proxy_addr: str) -> Tuple[Optional[str], Optional[str]]:
        """単一のプロキシの疎通を確認し、(出口IP, エラー理由) を返す。
        ソケット枯渇対策としてセッションを明示的に閉じ、DNS集中負荷緩和のためのジッターを入れる。
        """
        # DNS集中負荷を和らげるため、0〜50msのランダムな揺らぎ（Jitter）を入れる
        time.sleep(random.uniform(0.0, 0.05))

        proxy_url = f"socks5h://{proxy_addr}"
        proxies = {
            "http": proxy_url,
            "https": proxy_url
        }

        try:
            with requests.Session() as session:
                session.proxies = proxies
                res = session.get(
                    self.config.target_url,
                    timeout=self.config.timeout_seconds
                )
                if res.status_code == 200:
                    return res.text.strip(), None
                else:
                    return None, f"HTTP_{res.status_code}"
        except requests.exceptions.ConnectTimeout:
            return None, "ConnectTimeout"
        except requests.exceptions.ReadTimeout:
            return None, "ReadTimeout"
        except requests.exceptions.ProxyError:
            return None, "Proxy/DNS_Error"
        except requests.exceptions.ConnectionError:
            return None, "ConnectionError"
        except Exception as e:
            return None, f"Error_{type(e).__name__}"

    def _save_to_file(self, valid_proxies: List[str]):
        """アトミック書き込みで設定された全形式のファイルへ一括保存"""
        if not valid_proxies:
            return

        base_dir = os.path.dirname(os.path.abspath(self.config.output_base_name))
        if not base_dir or not os.path.exists(base_dir):
            base_dir = "."
        base_name = os.path.basename(self.config.output_base_name)

        def write_file(suffix: str, scheme: str):
            filename = f"{base_name}_{suffix}.txt" if suffix else f"{base_name}.txt"
            filepath = os.path.join(base_dir, filename)
            
            scheme_proxies = [f"{scheme}{p}" for p in valid_proxies]
            
            fd, temp_path = tempfile.mkstemp(dir=base_dir, text=True)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write("\n".join(scheme_proxies) + "\n")
            
            os.replace(temp_path, filepath)

        if self.config.output_socks5h:
            write_file("socks5h", "socks5h://")
        if self.config.output_socks5:
            write_file("socks5", "socks5://")
        if self.config.output_raw:
            write_file("raw", "")

# ---------------------------------------------------------
# 3. CLI インターフェース
# ---------------------------------------------------------
def run_cli_interactive(config: AppConfig):
    """対話型のCLIモード"""
    pipeline = ProxyPipeline(config)
    print("=== Mullvad Proxy Manager (CLI) ===")
    pipeline.run()

    while True:
        print("\n--- 次の操作を選択してください ---")
        print("[1] 指定した間隔（分）で定期実行を開始する")
        print("[2] 今すぐ再実行（手動更新）")
        print("[3] 終了する")
        
        choice = input("選択 (1-3): ").strip()
        
        if choice == '1':
            interval_str = input("何分ごとに実行しますか？ (例: 15): ").strip()
            try:
                interval_min = int(interval_str)
                if interval_min <= 0:
                    print("1分以上を指定してください。")
                    continue
                config.interval_minutes = interval_min
                ConfigManager.save(config)
                run_cli_daemon(config)
                break
            except ValueError:
                print("数値を入力してください。")
        elif choice == '2':
            pipeline.run()
        elif choice == '3':
            print("終了します。")
            break
        else:
            print("無効な入力です。")

def run_cli_daemon(config: AppConfig):
    """CLIの定期実行モード"""
    pipeline = ProxyPipeline(config)
    print(f"=== 定期実行モード開始 (間隔: {config.interval_minutes}分) ===")
    print("終了するには Ctrl+C を押してください。")
    
    try:
        while True:
            pipeline.run()
            wait_seconds = config.interval_minutes * 60
            print(f"\n[待機中] 次回の実行は {config.interval_minutes} 分後です...")
            
            for i in range(wait_seconds, 0, -1):
                mins, secs = divmod(i, 60)
                sys.stdout.write(f"\r次回まで: {mins:02d}:{secs:02d} ")
                sys.stdout.flush()
                time.sleep(1)
            print("\n")
            
    except KeyboardInterrupt:
        print("\n[終了] Ctrl+C が押されました。定期実行を終了します。")
        sys.exit(0)

# ---------------------------------------------------------
# 4. GUI インターフェース (PyQt6)
# ---------------------------------------------------------
if HAS_PYQT6:
    class WorkerThread(QThread):
        progress_sig = pyqtSignal(int, int, str)
        log_sig = pyqtSignal(str)
        finished_sig = pyqtSignal(list)

        def __init__(self, config: AppConfig):
            super().__init__()
            self.config = config
            self.pipeline = ProxyPipeline(config, log_callback=self.log_sig.emit)

        def run(self):
            def p_callback(completed, total, proxy):
                self.progress_sig.emit(completed, total, proxy)
            
            valid_list = self.pipeline.run(progress_callback=p_callback)
            self.finished_sig.emit(valid_list)

        def cancel(self):
            self.pipeline.cancel()

    class MainWindow(QMainWindow):
        """PyQt6 GUIメインウィンドウ"""
        def __init__(self, config: AppConfig):
            super().__init__()
            self.config = config
            self.worker = None
            self.timer = QTimer()
            self.timer.timeout.connect(self.start_pipeline)
            self.is_running = False
            
            self.init_ui()

        def init_ui(self):
            """UIのレイアウト構築"""
            self.setWindowTitle("Mullvad Proxy Manager")
            self.resize(800, 680)

            central_widget = QWidget()
            self.setCentralWidget(central_widget)
            main_layout = QVBoxLayout(central_widget)

            # --- 設定グループ ---
            settings_group = QGroupBox("設定")
            grid = QGridLayout()

            grid.addWidget(QLabel("出力ベース名:"), 0, 0)
            self.out_file_edit = QLineEdit(self.config.output_base_name)
            self.out_file_edit.setToolTip("保存時のファイル名のベース（拡張子なし）。例: mullvad_proxies")
            grid.addWidget(self.out_file_edit, 0, 1)

            grid.addWidget(QLabel("同時出力形式:"), 1, 0)
            format_layout = QHBoxLayout()
            self.chk_socks5h = QCheckBox("socks5h://")
            self.chk_socks5h.setChecked(self.config.output_socks5h)
            self.chk_socks5 = QCheckBox("socks5://")
            self.chk_socks5.setChecked(self.config.output_socks5)
            self.chk_raw = QCheckBox("スキームなし")
            self.chk_raw.setChecked(self.config.output_raw)

            format_layout.addWidget(self.chk_socks5h)
            format_layout.addWidget(self.chk_socks5)
            format_layout.addWidget(self.chk_raw)
            grid.addLayout(format_layout, 1, 1)

            grid.addWidget(QLabel("国フィルタ(カンマ区切り):"), 2, 0)
            self.countries_edit = QLineEdit(self.config.countries)
            self.countries_edit.setPlaceholderText("例: jp, us (空欄で全て)")
            grid.addWidget(self.countries_edit, 2, 1)

            grid.addWidget(QLabel("検証用ターゲットURL:"), 3, 0)
            self.url_edit = QLineEdit(self.config.target_url)
            grid.addWidget(self.url_edit, 3, 1)

            grid.addWidget(QLabel("同時検証スレッド数:"), 4, 0)
            self.spin_max_workers = QSpinBox()
            self.spin_max_workers.setRange(2, 256)
            self.spin_max_workers.setSingleStep(2)
            self.spin_max_workers.setValue(self.config.max_workers)
            self.spin_max_workers.setToolTip("並列で接続確認を行うスレッド数 (2〜256、デフォルト: 64)")
            grid.addWidget(self.spin_max_workers, 4, 1)

            grid.addWidget(QLabel("タイムアウト秒数:"), 5, 0)
            self.spin_timeout = QDoubleSpinBox()
            self.spin_timeout.setRange(0.5, 30.0)
            self.spin_timeout.setSingleStep(0.5)
            self.spin_timeout.setValue(self.config.timeout_seconds)
            self.spin_timeout.setToolTip("1プロキシあたりの応答待ち時間 (秒)")
            grid.addWidget(self.spin_timeout, 5, 1)

            grid.addWidget(QLabel("定期実行間隔 (分):"), 6, 0)
            self.spin_interval = QSpinBox()
            self.spin_interval.setRange(0, 1440)
            self.spin_interval.setValue(self.config.interval_minutes)
            self.spin_interval.setSpecialValueText("0 (単発実行)")
            grid.addWidget(self.spin_interval, 6, 1)

            settings_group.setLayout(grid)
            main_layout.addWidget(settings_group)

            # --- 実行制御ボタン ---
            btn_layout = QHBoxLayout()
            self.btn_run = QPushButton("手動実行")
            self.btn_run.clicked.connect(self.run_once)
            btn_layout.addWidget(self.btn_run)

            self.btn_start_loop = QPushButton("定期実行 開始")
            self.btn_start_loop.clicked.connect(self.start_loop)
            btn_layout.addWidget(self.btn_start_loop)

            self.btn_stop_loop = QPushButton("定期実行 停止")
            self.btn_stop_loop.setEnabled(False)
            self.btn_stop_loop.clicked.connect(self.stop_loop)
            btn_layout.addWidget(self.btn_stop_loop)

            self.btn_cancel = QPushButton("中断")
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.clicked.connect(self.cancel_pipeline)
            btn_layout.addWidget(self.btn_cancel)

            main_layout.addLayout(btn_layout)

            # --- プログレスバー ---
            self.progress_bar = QProgressBar()
            self.progress_bar.setValue(0)
            main_layout.addWidget(self.progress_bar)

            # --- ログ表示エリア ---
            self.log_text = QTextEdit()
            self.log_text.setReadOnly(True)
            self.log_text.setFont(QFont("Consolas", 10))
            main_layout.addWidget(self.log_text)

        def log(self, message: str):
            self.log_text.append(message)

        def apply_ui_to_config(self):
            """GUIの各入力を設定オブジェクトに反映して保存"""
            self.config.output_base_name = self.out_file_edit.text()
            self.config.output_socks5h = self.chk_socks5h.isChecked()
            self.config.output_socks5 = self.chk_socks5.isChecked()
            self.config.output_raw = self.chk_raw.isChecked()
            self.config.countries = self.countries_edit.text()
            self.config.target_url = self.url_edit.text()
            self.config.max_workers = self.spin_max_workers.value()
            self.config.timeout_seconds = self.spin_timeout.value()
            self.config.interval_minutes = self.spin_interval.value()
            ConfigManager.save(self.config)

        def start_pipeline(self):
            """検証処理の実行開始"""
            if self.is_running:
                return
            
            self.apply_ui_to_config()
            self.is_running = True
            self.btn_run.setEnabled(False)
            self.btn_cancel.setEnabled(True)
            self.progress_bar.setValue(0)
            self.log("--- 実行開始 ---")

            self.worker = WorkerThread(self.config)
            self.worker.progress_sig.connect(self.update_progress)
            self.worker.log_sig.connect(self.log)
            self.worker.finished_sig.connect(self.on_pipeline_finished)
            self.worker.start()

        def run_once(self):
            self.start_pipeline()

        def start_loop(self):
            self.apply_ui_to_config()
            interval = self.config.interval_minutes
            if interval <= 0:
                QMessageBox.warning(self, "エラー", "定期実行の間隔を1分以上に設定してください。")
                return
            
            self.timer.start(interval * 60 * 1000)
            self.btn_start_loop.setEnabled(False)
            self.btn_stop_loop.setEnabled(True)
            self.spin_interval.setEnabled(False)
            self.log(f"定期実行を開始しました。({interval}分間隔)")
            self.start_pipeline()

        def stop_loop(self):
            self.timer.stop()
            self.btn_start_loop.setEnabled(True)
            self.btn_stop_loop.setEnabled(False)
            self.spin_interval.setEnabled(True)
            self.log("定期実行を停止しました。")

        def cancel_pipeline(self):
            if self.worker and self.is_running:
                self.worker.cancel()
                self.log("中断シグナルを送信しました。待機中...")
                self.btn_cancel.setEnabled(False)

        def update_progress(self, completed, total, current_proxy):
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(completed)

        def on_pipeline_finished(self, valid_list):
            self.is_running = False
            self.btn_run.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.progress_bar.setValue(self.progress_bar.maximum())
            self.log(f"--- 処理完了 (有効: {len(valid_list)}件) ---")

# ---------------------------------------------------------
# 5. エントリーポイント
# ---------------------------------------------------------
def hide_console():
    """Windows環境でコンソールウィンドウを非表示にする"""
    if sys.platform == "win32":
        # 実行中のコンソールウィンドウのハンドルを取得して非表示(SW_HIDE=0)にする
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)

def main():
    parser = argparse.ArgumentParser(description="Mullvad Proxy Manager")
    parser.add_argument("--gui", action="store_true", help="GUIモードで起動します")
    parser.add_argument("--run", action="store_true", help="CLIで即時1回実行して終了します")
    args = parser.parse_args()

    config = ConfigManager.load()

    # GUIモードで起動された場合は即座に黒い画面を消す
    if args.gui:
        hide_console()
        if not HAS_PYQT6:
            print("エラー: PyQt6がインストールされていないため、GUIを起動できません。")
            sys.exit(1)
        
        app = QApplication(sys.argv)
        window = MainWindow(config)
        window.show()
        sys.exit(app.exec())
    else:
        if args.run:
            pipeline = ProxyPipeline(config)
            pipeline.run()
        else:
            if config.interval_minutes > 0:
                run_cli_daemon(config)
            else:
                run_cli_interactive(config)

if __name__ == "__main__":
    main()
