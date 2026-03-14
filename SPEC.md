# AirType 仕様書

> 最終更新: 2026-03-12
> ブランチ: `claude/add-completed-code-A2gKy`

---

## 概要

AirType は Windows 向けのプッシュトゥトーク式音声入力ツールです。
「無変換」キーを押し続けて話すと、音声が自動的にテキストに変換されてアクティブウィンドウに貼り付けられます。
外部サービスへの依存なく、完全ローカルで動作します。

---

## システム要件

| 項目 | 要件 |
|------|------|
| OS | Windows 10/11 (64bit) |
| Python | 3.10 以上 |
| GPU | Vulkan 対応 GPU (AMD RX 6600 等) |
| VRAM | 8 GB 推奨 |
| RAM | 16 GB 以上推奨 |

### 外部バイナリ（Python パッケージ外）

```
AirType の親フォルダ/
├── whisper.cpp-windows-vulkan/
│   ├── whisper-server.exe    ← STT サーバーモード（推奨）
│   ├── whisper-cli.exe       ← STT CLI モード（フォールバック）
│   ├── ggml-kotoba-whisper-v2.0-q5_0.bin   ← デフォルトモデル (~538 MB)
│   ├── ggml-kotoba-whisper-v2.0.bin         ← 高精度モデル (~1.52 GB)
│   └── ggml-vulkan.dll
└── llama.cpp-windows-vulkan/
    ├── llama-server.exe      ← LLM サーバーモード（推奨）
    ├── llama-cli.exe         ← LLM CLI モード（フォールバック）
    └── Qwen3.5-2B-Q5_K_M.gguf   ← デフォルトモデル (~1.7 GB)
```

### Python 依存パッケージ

```
pynput       >=1.7.6    グローバルホットキー・キーボード制御
sounddevice  >=0.4.6    マイク録音 (PortAudio ラッパー)
numpy        >=1.24.0   録音バッファ処理
pyperclip    >=1.8.2    クリップボード操作
pystray      >=0.19.0   システムトレイアイコン
Pillow       >=10.0.0   トレイアイコン画像生成
```

---

## 動作モード

### シングルPCモード（デフォルト）

GPU を搭載した1台のPCで録音・STT・LLM・ペーストをすべて実行します。

```
python main.py
# または
AirType_launcher.vbs をダブルクリック
```

### ネットワークモード（2台構成）

GPU 非搭載のクライアントPCから GPU 搭載のホストPCに音声データを送信し、
STT・LLM処理をホストPC側で行います。

| 役割 | PC | 起動スクリプト | 必要なもの |
|------|----|---------------|-----------|
| **ホスト（サーバー）** | t-tak（RX 6600搭載） | `api_server.py` | whisper.cpp / llama.cpp / GPU |
| **クライアント** | mahan | `client.py` | マイク・Python のみ |

**ホストPC（t-tak）での起動:**
```
python api_server.py
```

**クライアントPC（mahan）での起動:**
```
python client.py
# または
client_launcher.vbs をダブルクリック
```

**設定（`airtype_config.json`）:**
```json
"network": {
    "server_url": "http://YOUR_SERVER_IP:8000/dictate"
}
```
> `server_url` はホストPC（t-tak）のIPアドレスを指定します。

---

## フォルダ構成

```
AirType/
├── main.py               メインスクリプト（シングルPCモード）
├── api_server.py         APIサーバー（ネットワークモード・ホストPC用）
├── client.py             軽量クライアント（ネットワークモード・クライアントPC用）
├── step1_recorder.py     音声録音
├── step2_transcriber.py  音声→テキスト変換 (STT)
├── step3_refiner.py      テキスト整形 (LLM)
├── step4_paster.py       クリップボード + ペースト
├── step5_gui.py          GUI (トレイ・設定・履歴)
├── config.py             設定読み込みユーティリティ
├── airtype_config.json   設定ファイル
├── AirType_launcher.vbs  シングルPCモード用ランチャー
├── client_launcher.vbs   クライアントPC用ランチャー
├── requirements.txt      Python 依存パッケージ
└── SPEC.md               本仕様書
```

---

## アーキテクチャ

### パイプライン全体

```
[無変換 押下]
    │
    ▼
[Step 1] Recorder
  マイク録音 (sounddevice, 16kHz mono int16)
    │ WAV ファイル (tempfile)
    ▼
[Step 2] WhisperTranscriber
  音声→テキスト (whisper.cpp Vulkan)
    │ 生テキスト (句読点なし、フィラーあり)
    ▼
[Step 3] LlamaRefiner
  フィラー除去 + 句読点追加 (Qwen3.5 LLM)
    │ 整形テキスト
    ▼
[Step 4] Paster
  クリップボードコピー + Ctrl+V 送信
    │
    ▼
[アクティブウィンドウ]
```

### スレッドモデル

```
メインスレッド   : tkinter OSD イベントループ
PttHook スレッド : WH_KEYBOARD_LL フック + Windows メッセージポンプ
Worker スレッド  : WAV Queue 消費 → STT → 整形 → ペースト
録音             : sounddevice 非同期コールバック (追加スレッド不要)
TrayIcon スレッド: pystray イベントループ
LlamaServer スレッド : llama-server.exe バックグラウンド起動・ヘルスチェック
WhisperServer スレッド: whisper-server.exe バックグラウンド起動・接続確認
```

### 状態遷移

```
IDLE ──[無変換 押下]──→ RECORDING
                           │ 録音 + OSD 表示
       ←──[無変換 離放]──┘
IDLE ←── WAV を Queue に投入
  │
  └── Worker: STT → 整形 → ペースト → 履歴追加
```

---

## 各コンポーネント詳細

### Step 1: Recorder (`step1_recorder.py`)

- sounddevice の非同期ストリームでマイク入力を int16 バッファに蓄積
- `start()` / `stop()` で録音区間を制御
- 停止時に WAV ファイル (16kHz mono int16) を tempfile に保存して返す
- サンプルレート: **16 kHz**（Whisper の要件）

### Step 2: WhisperTranscriber (`step2_transcriber.py`)

**モード選択（自動）:**

| 優先度 | 条件 | 動作 |
|--------|------|------|
| 1 | whisper-server.exe が存在する | **サーバーモード**（常駐・高速） |
| 2 | whisper-cli.exe のみ存在する | **CLI モード**（毎回起動・低速） |

**サーバーモード:**
- ポート: `18766`
- 起動: アプリ起動時にバックグラウンドスレッドで起動
- 起動順序: llama-server の準備完了後に起動（`startup_gate` による VRAM 競合回避）
- 準備確認: TCP ポート接続ポーリング（最大 60 秒）
- API: `POST http://127.0.0.1:18766/inference` (multipart/form-data)
- レスポンス: `verbose_json`（セグメント単位でノイズフィルタを適用）

**ノイズフィルタ (`_NOISE_RE`):**
- `≪≫《》` を含む行（幻覚トークン）
- 括弧で囲まれた行: `(無音)` `[音楽]` `（拍手）` 等
- 句読点・記号のみの行
- 典型的な幻覚フレーズ: 「ご視聴ありがとうございました」等

**選択可能モデル:**

| キー | ファイル | サイズ | 特徴 |
|------|----------|--------|------|
| `kotoba-q5`（デフォルト） | ggml-kotoba-whisper-v2.0-q5_0.bin | ~538 MB | 日本語特化・速度精度バランス |
| `kotoba-full` | ggml-kotoba-whisper-v2.0.bin | ~1.52 GB | 最高精度 |
| `large-v3` | ggml-large-v3.bin | ~3 GB | 汎用・低速 |
| `accurate` | ggml-large-v3-q5_0.bin | ~1.1 GB | 汎用・量子化 |
| `turbo` | ggml-large-v3-turbo-q5_0.bin | ~850 MB | 汎用・高速 |

**初期プロンプト:** `"日本語の音声入力です。"`（同音異義語誤認識の軽減）

### Step 3: LlamaRefiner (`step3_refiner.py`)

**モード選択（自動）:**

| 優先度 | 条件 | 動作 |
|--------|------|------|
| 1 | llama-server.exe が存在する | **サーバーモード**（常駐・高速） |
| 2 | llama-cli.exe のみ存在する | **CLI モード**（毎回起動・低速） |
| 3 | いずれも存在しない | **RuleBasedRefiner**（フィラー正規表現のみ） |

**サーバーモード:**
- ポート: `18765`
- 起動: アプリ起動時にバックグラウンドスレッドで起動
- 準備確認: `GET /health` ポーリング（最大 90 秒）
- API: `POST http://localhost:18765/completion`

**推論パラメータ:**

| パラメータ | 値 | 目的 |
|-----------|-----|------|
| temperature | 0.1 | 決定論的出力（誤変更リスク低減） |
| top_p | 0.8 | - |
| top_k | 20 | - |
| repeat_penalty | 1.15 | 繰り返しループ防止 |
| repeat_last_n | 64 | 繰り返しチェック対象トークン数 |
| n_ctx | 512 | コンテキスト長（KV キャッシュ削減） |
| n_predict | 256 | 最大生成トークン数 |
| ngl | 99 | 全レイヤーを Vulkan GPU にオフロード |

**プロンプト設計（ChatML + Few-shot）:**
- システムプロンプト: フィラー削除・句読点追加のみ許可。単語変更・文体変換・文の再構成を明示禁止
- Few-shot: 1件（最小変更のみ行う例）
- Qwen3 思考モード抑制: アシスタントターンを `<think>\n\n</think>\n` でプレフィル

**忠実度チェック (`_is_faithful`):**
- 句読点・空白除去後の `SequenceMatcher.ratio()` で類似度を計算
- ratio < 0.70 → 「過剰編集」と判定してルールベースにフォールバック
- 目的: 文の書き換え・削除を検出（フィラー削除は ratio ≈ 0.8〜1.0 なので通過）

**フォールバック階層:**
```
LLM (サーバー/CLI)
    ↓ 過剰編集検出 または エラー
RuleBasedRefiner (正規表現フィラー除去のみ)
```

**選択可能モデル:**

| キー | ファイル | サイズ | 特徴 |
|------|----------|--------|------|
| `qwen3.5-2b`（デフォルト） | Qwen3.5-2B-Q5_K_M.gguf | ~1.7 GB | 速度重視 |
| `qwen3.5-4b` | qwen3.5-4b-instruct-q5_k_m.gguf | ~2.7 GB | 高精度 |

### Step 4: Paster (`step4_paster.py`)

- pyperclip でクリップボードにテキストをコピー
- コピー後にクリップボード内容を検証（不一致時はリトライ、最大 2 回）
- Windows IME を一時無効化してペースト中の変換候補干渉を防ぐ
- Ctrl+V（pynput）でアクティブウィンドウにペースト
- ペースト後に IME を元の状態に復元

### Step 5: GUI (`step5_gui.py`)

- **TrayIcon**: pystray によるシステムトレイアイコン
  - 録音中は赤アイコン、アイドル時はグレーアイコン
  - 右クリックメニュー: 設定・履歴・終了
- **SettingsWindow**: tkinter によるモデル選択ダイアログ
  - Whisper モデルの変更（変更時は旧サーバーを終了して新サーバーを起動）
- **HistoryWindow**: 認識テキスト履歴（最大 200 件）
  - コピー・全消去機能付き
- **OSD**: 枠なし・半透明・常に最前面・クリック透過の録音中インジケーター

---

## PTT（プッシュトゥトーク）実装

- `WH_KEYBOARD_LL`（Windows 低レベルキーボードフック）で `VK_NONCONVERT`（無変換キー、`0x1D`）を監視
- 対象キーを **IME に渡す前に握りつぶす**（管理者権限不要）
- キーリピート対策: 押下中フラグで 2 回目以降を無視
- フック専用スレッドで Windows メッセージポンプを維持

---

## VRAM 使用状況（AMD RX 6600, 8 GB）

| コンポーネント | 専用 VRAM | 共有 GPU メモリ |
|----------------|-----------|----------------|
| llama-server (Qwen3.5-2B) | ~3.5 GB | ~3.0 GB (PCIe) |
| whisper-server (kotoba-q5) | ~0.3 GB | - |
| **合計** | **~3.8 GB** | **~3.0 GB** |

> 備考: llama-server 起動後に whisper-server を起動することで VRAM 競合を回避しているが、
> 両サーバー合計が 8 GB を超えるため llama-server の一部が共有メモリ（PCIe 経由、低速）に溢れる。
> GPU Compute は正常に動作している（Compute 0 グラフで確認済み）。

**起動順序（`main.py`）:**
```
LlamaRefiner 起動
    ↓ _server_ready イベントがゲートとして機能
WhisperTranscriber 起動（llama-server 準備完了後）
```

---

## 起動方法

```
# コンソールあり（開発・デバッグ用）
python main.py

# コンソールなし（通常使用）
AirType_launcher.vbs をダブルクリック
  → .venv\Scripts\pythonw.exe または venv\Scripts\pythonw.exe を使用
  → ログ出力: airtype.log（スクリプトと同ディレクトリ）
```

---

## 終了方法

- トレイアイコン右クリック → 「終了」
- または Ctrl+C（コンソール起動時、2 秒以内に 2 回）

シャットダウン処理:
1. トレイアイコン停止
2. PTT フック解除
3. Worker に Poison Pill 送信・完了待機（最大 10 秒）
4. whisper-server.exe 終了
5. llama-server.exe 終了
6. tkinter ループ終了

---

## 既知の制限事項

| 項目 | 内容 |
|------|------|
| Whisper 認識精度 | 小声・環境音で誤認識が増加。文境界なしで連続発話すると LLM が誤解釈する場合あり |
| LLM 微細変更 | ratio ≥ 0.70 の微細な単語置換（`は→も` 等）は忠実度チェックをすり抜けることがある |
| VRAM 共有 | 両サーバー同時動作で llama-server の一部が PCIe 経由の共有メモリに溢れる |
| モデル変更 | 設定ウィンドウからの Whisper モデル変更のみ対応。Qwen モデルの変更には再起動が必要 |
| Windows 専用 | `WH_KEYBOARD_LL`・IME 制御・`CREATE_NO_WINDOW` 等の Windows API を多用 |
