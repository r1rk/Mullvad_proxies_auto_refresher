# mullvad_proxy_auto_refresher
Mullvadトンネル内から使えるProxyリストを公式に提供されているAPIから自動で取得し、txt形式のリストを更新するPythonスクリプトです。 / The Python script distributed in this repository automatically retrieves a list of proxies that can be used within the Mullvad tunnel from the officially provided API and updates the list in .txt format.

導入、使い方の記事は[こちら](https://note.com/kisaragi_ririka/n/n5516669d0415/)

## 🚀 起動・使用方法

本ツールは GUI（画面操作） と CLI（コマンドライン操作） の両方に対応しています。

---

### 1. GUI モード（画面で操作する）
* **実行方法**: `Mullvad Proxy Auto Refresher.exe` をダブルクリック
* コンソール画面を出さずに、設定変更や手動・定期実行を視覚的に操作できます。

---

### 2. CLI モード（コマンドラインで操作する）

コマンドプロンプトや PowerShell から引数を渡して実行します。

#### 💡 コマンドライン引数一覧

| 引数 | 説明 |
| :--- | :--- |
| *(なし)* | **GUI モード** で起動します（ダブルクリック時のデフォルト）。 |
| `--cli` | **対話型 CLI モード** で起動します。 |
| `--run` | 設定ファイルに従い **1回だけ即時実行** して終了します（タスクスケジューラ等向け）。 |
| `-h`, `--help` | ヘルプメッセージを表示します。 |

---

### 🎮 対話型 CLI モードの操作方法 (`--cli`)

コマンドプロンプトで以下を実行すると対話メニューが起動します。

```cmd
"Mullvad Proxy Auto Refresher.exe" --cli

```

起動すると自動的に1回目のプロキシ取得・検証が実行され、完了後に以下のメニューが表示されます。

```text
=== Mullvad Proxy Manager (CLI) ===
[エラー/成功ログが表示されます]

--- 次の操作を選択してください ---
[1] 指定した間隔（分）で定期実行を開始する
[2] 今すぐ再実行（手動更新）
[3] 終了する
選択 (1-3): 

```

#### 各メニューの選択肢：

* **`1` を入力（定期実行モード）**
* 次に `何分ごとに実行しますか？ (例: 15):` と聞かれます。
* `1` 以上の数値を入力すると、指定した分単位のカウントダウンタイマーが始まり、自動で定期実行を繰り返します。
* 定期実行を止めたい場合は `Ctrl + C` を押すと安全に終了します。


* **`2` を入力（手動更新）**
* 即座にプロキシリストを再取得し、検証と保存を行います。完了後に再度メニューへ戻ります。


* **`3` を入力（終了）**
* CLIモードを終了します。


---

### ⚙️ 設定ファイル (`config_mp.json`) について

CLIモード（特に `--run` 実行時）での動作設定は、exeと同じフォルダに生成される `config_mp.json` を直接編集することでも変更可能です。

```json
{
    "output_base_name": "mullvad_proxies",
    "output_socks5h": true,
    "output_socks5": true,
    "output_raw": true,
    "countries": "jp,us",
    "target_url": "[https://icanhazip.com/](https://icanhazip.com/)",
    "interval_minutes": 0,
    "max_workers": 64,
    "timeout_seconds": 2.0
}

```

* **`countries`**: 取得対象の国コードをカンマ区切りで指定（例: `"jp,us"`）。空欄で全地域。
* **`interval_minutes`**: 0 の場合は単発実行。1 以上に設定して `--cli` で起動すると自動的に定期実行モードになります。
* **`max_workers`**: 同時検証スレッド数（2〜256）。

---

### 📁 出力ファイル

検証に成功した有効なプロキシは、設定に応じた形式で保存されます。

* `mullvad_proxies_socks5h.txt` (`socks5h://ホスト:1080`)
* `mullvad_proxies_socks5.txt` (`socks5://ホスト:1080`)
* `mullvad_proxies_raw.txt` (`ホスト:1080`)

```
