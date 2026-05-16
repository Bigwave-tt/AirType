# AirType

Windows 向けプッシュトゥトーク（PTT）音声入力ツール。
「無変換」キーを押しながら話すと、音声が自動的にテキストに変換されてアクティブウィンドウに貼り付けられます。

**完全ローカル動作** — クラウドへの送信なし。すべての処理が手元のPCで完結します。

---

## 特徴

- **プッシュトゥトーク** — 「無変換」キーを押している間だけ録音。誤認識を防ぐシンプルな操作
- **高精度日本語STT** — [whisper.cpp](https://github.com/ggerganov/whisper.cpp)（Vulkan GPU加速）で文字起こし
- **LLMによるテキスト整形** — [llama.cpp](https://github.com/ggerganov/llama.cpp)がフィラー除去・句読点付与を行う（オプション）
- **常駐サーバー方式** — 初回起動後はモデルがVRAMに常駐し、低レイテンシで応答
- **OSD表示** — 録音中に半透明のインジケーターをオーバーレイ表示
- **システムトレイ常駐** — 右クリックメニューから設定・履歴・終了を操作
- **ネットワークモード** — GPU非搭載PCからGPU搭載PCに音声を転送して処理（2台構成）

---

## 必要環境

| 項目 | 要件 |
|------|------|
| OS | Windows 10/11 (64bit) |
| Python | 3.10 以上 |
| GPU | Vulkan 対応 GPU（AMD RX 6600 等） |
| VRAM | 8 GB 推奨 |
| RAM | 16 GB 以上推奨 |

### 外部バイナリ

AirType リポジトリの **親フォルダ** に配置してください。

```
親フォルダ/
├── AirType/                        ← このリポジトリ
├── whisper.cpp-windows-vulkan/
│   ├── whisper-server.exe
│   ├── whisper-cli.exe
│   ├── ggml-kotoba-whisper-v2.0-q5_0.bin   (~538 MB, デフォルト)
│   └── ggml-vulkan.dll
└── llama.cpp-windows-vulkan/
    ├── llama-server.exe
    ├── llama-cli.exe
    └── Qwen3.5-2B-Q5_K_M.gguf              (~1.7 GB, デフォルト)
```

- **whisper.cpp** — [whisper.cpp リリース](https://github.com/ggerganov/whisper.cpp/releases) から `vulkan` ビルドを取得
- **kotoba-whisper モデル** — [ggml-kotoba-whisper-v2.0](https://huggingface.co/ggml-org/kotoba-whisper-v2.0-gguf) (Hugging Face)
- **llama.cpp** — [llama.cpp リリース](https://github.com/ggerganov/llama.cpp/releases) から `vulkan` ビルドを取得
- **Qwen3.5-2B モデル** — [Qwen3.5-2B GGUF](https://huggingface.co/bartowski/Qwen3.5-2B-Instruct-GGUF) (Hugging Face)

---

## セットアップ

### 1. Python 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

`requirements.txt` の主な依存パッケージ:

| パッケージ | 用途 |
|-----------|------|
| pynput | グローバルホットキー・キーボード制御 |
| sounddevice | マイク録音 |
| numpy | 録音バッファ処理 |
| pyperclip | クリップボード操作 |
| pystray | システムトレイアイコン |
| Pillow | アイコン画像生成 |

### 2. 設定ファイルの確認

`airtype_config.json` を開き、必要に応じて変更します。

```json
{
  "network": {
    "server_url": "http://YOUR_SERVER_IP:8000/dictate"
  },
  "transcriber": {
    "backend": "sensevoice",
    "language": "ja"
  }
}
```

ネットワークモードを使わない場合、`network` セクションの変更は不要です。

### 3. 起動

```bash
# コンソールあり（開発・デバッグ用）
python main.py
```

通常使用は `AirType_launcher.vbs` をダブルクリック（コンソールなし、バックグラウンド起動）。

---

## 使い方

1. `main.py` または `AirType_launcher.vbs` を起動するとシステムトレイにアイコンが表示される
2. **「無変換」キーを押し続けながら** 話す（OSDインジケーターが赤く表示される）
3. キーを **離す** と音声認識・テキスト整形が実行される
4. 認識結果がアクティブウィンドウにペーストされる

### トレイアイコンの操作

右クリックメニューから以下を操作できます：

- **設定** — STTバックエンドやLLMモデルの変更
- **履歴** — 認識テキストの履歴閲覧・コピー
- **終了** — アプリケーションを終了

---

## ネットワークモード（2台構成）

GPU非搭載のPCから、GPU搭載のPCにマイク音声を送信して処理する構成です。

**ホストPC（GPU搭載）で起動:**
```bash
python api_server.py
```

**クライアントPC で起動:**
```bash
python client.py
# または
client_launcher.vbs をダブルクリック
```

**`airtype_config.json` の設定:**
```json
"network": {
    "server_url": "http://YOUR_SERVER_IP:8000/dictate",
    "host":       "0.0.0.0",
    "port":       8000,
    "api_key":    ""
}
```

`YOUR_SERVER_IP` をホストPCのIPアドレスに変更してください。

---

## STTバックエンドの選択

`airtype_config.json` の `transcriber.backend` で切り替えます。

| バックエンド | 値 | 説明 |
|------------|-----|------|
| whisper.cpp（デフォルト） | `"whisper"` | Vulkan GPU加速、英語・多言語対応 |
| SenseVoice Small（ONNX） | `"sensevoice"` | 高速、日本語特化、DirectML加速 |

SenseVoice を使う場合は `sensevoice-onnx` フォルダを親ディレクトリに配置してください。

---

## フォルダ構成

```
AirType/
├── main.py               メインスクリプト（シングルPCモード）
├── api_server.py         APIサーバー（ネットワークモード・ホストPC用）
├── client.py             軽量クライアント（ネットワークモード・クライアントPC用）
├── client_gui.py         クライアント用GUIコンポーネント
├── step1_recorder.py     音声録音
├── step2_transcriber.py  音声→テキスト変換（STT）
├── step2_sensevoice.py   SenseVoice STTバックエンド
├── step3_refiner.py      テキスト整形（LLM）
├── step4_paster.py       クリップボード＋ペースト
├── step5_gui.py          GUI（トレイ・設定・履歴）
├── config.py             設定読み込みユーティリティ
├── airtype_config.json   設定ファイル
├── requirements.txt      Python依存パッケージ
├── AirType_launcher.vbs  シングルPCモード用ランチャー
├── client_launcher.vbs   クライアントPC用ランチャー
└── SPEC.md               詳細仕様書
```

---

## 既知の制限

| 項目 | 内容 |
|------|------|
| **Windows専用** | `WH_KEYBOARD_LL`・IME制御・`CREATE_NO_WINDOW` 等のWindows APIを多用。WSLでは動作しない |
| **Vulkan GPU必須**（LLM使用時）| llama.cpp / whisper.cpp のVulkanビルドを使用。GPU非搭載PCではLLM整形が利用不可 |
| **認識精度** | 小声・環境音で誤認識が増加。文境界なしで連続発話するとLLMが誤解釈する場合あり |
| **VRAM消費** | 両サーバー同時動作で約3.8GB（RX 6600, Qwen3.5-2B + kotoba-whisper-q5の場合） |
| **モデル変更** | 設定ウィンドウからはWhisperモデルのみ変更可。LLMモデルの変更は再起動が必要 |
| **LLM微細変更** | 類似度チェックをすり抜ける軽微な単語置換（`は→も` 等）が発生することがある |

---

## ライセンス

[MIT License](LICENSE)
