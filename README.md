# AirType

Windows 向けプッシュトゥトーク（PTT）音声入力ツール。
「無変換」キーを押しながら話すと、音声が自動的にテキストに変換されてアクティブウィンドウに貼り付けられます。

**完全ローカル動作** — クラウドへの送信なし。すべての処理が手元のPCで完結します。

---

## 特徴

- **プッシュトゥトーク** — 「無変換」キーを押している間だけ録音。意図しない認識が起きない
- **AMD Vulkan 対応** — NVIDIA 不要。AMD GPU（RX 6600 等）で GPU 加速が動く数少ないツール
- **日本語特化 STT** — kotoba-whisper（日本語ファインチューン済み whisper）で高精度認識
- **LLM によるテキスト整形** — フィラー除去・句読点付与をローカル LLM が行う（オプション）
- **常駐サーバー方式** — 初回起動後はモデルが VRAM に常駐し、毎回起動コストなし
- **OSD 表示** — 録音中に半透明インジケーターをオーバーレイ表示
- **2台構成対応** — GPU 非搭載 PC からネットワーク越しに GPU PC へ音声を送信して処理

---

## 必要環境

| 項目 | 要件 |
|------|------|
| OS | Windows 10 / 11 (64bit) |
| Python | 3.10 以上 |
| GPU | Vulkan 対応 GPU（AMD RX 6600 等） |
| VRAM | 8 GB 推奨（LLM 整形使用時） |
| RAM | 16 GB 以上推奨 |

---

## セットアップ

AirType 本体のほかに、**whisper.cpp**（音声認識）と **llama.cpp**（テキスト整形）のバイナリ・モデルが必要です。以下の手順で揃えてください。

### 手順 1 — フォルダ構成を作る

AirType リポジトリの **親フォルダ** に、次の並びでフォルダを配置します。

```
どこか作業フォルダ/
├── AirType/                         ← このリポジトリ（git clone 先）
├── whisper.cpp-windows-vulkan/      ← 手順 2 で作成
└── llama.cpp-windows-vulkan/        ← 手順 3 で作成（LLM 整形を使う場合）
```

### 手順 2 — whisper.cpp を用意する

#### 2-1. バイナリを取得

1. [whisper.cpp Releases](https://github.com/ggerganov/whisper.cpp/releases) を開く
2. 最新リリースの Assets から **`whisper-...-windows-vulkan.zip`** をダウンロード
3. 展開して中身を `whisper.cpp-windows-vulkan/` フォルダに置く
   - `whisper-server.exe` と `whisper-cli.exe` が含まれていれば OK

#### 2-2. 音声認識モデルを取得

HuggingFace から GGUF 形式のモデルをダウンロードして `whisper.cpp-windows-vulkan/` に置きます。

| モデル | ファイル名 | サイズ | 推奨 |
|--------|-----------|--------|------|
| kotoba-whisper v2.0 Q5（日本語特化） | `ggml-kotoba-whisper-v2.0-q5_0.bin` | 約 540 MB | ★ デフォルト |
| kotoba-whisper v2.0 フル精度 | `ggml-kotoba-whisper-v2.0.bin` | 約 1.5 GB | 最高精度 |

ダウンロード先：[ggml-org/kotoba-whisper-v2.0-gguf（Hugging Face）](https://huggingface.co/ggml-org/kotoba-whisper-v2.0-gguf/tree/main)

> ページ上部の「Files and versions」タブを開き、該当ファイルの右側にある ↓ アイコンからダウンロードできます。

**この時点でのフォルダ構成:**
```
whisper.cpp-windows-vulkan/
├── whisper-server.exe
├── whisper-cli.exe
├── ggml-kotoba-whisper-v2.0-q5_0.bin   ← ここに置く
└── ggml-vulkan.dll（等）
```

### 手順 3 — llama.cpp を用意する（LLM 整形を使う場合）

LLM によるフィラー除去・句読点付与を使わない場合はスキップできます。

#### 3-1. バイナリを取得

1. [llama.cpp Releases](https://github.com/ggerganov/llama.cpp/releases) を開く
2. 最新リリースの Assets から **`llama-...-bin-win-vulkan-x64.zip`** をダウンロード
3. 展開して中身を `llama.cpp-windows-vulkan/` フォルダに置く
   - `llama-server.exe` と `llama-cli.exe` が含まれていれば OK

#### 3-2. LLM モデルを取得

| モデル | ファイル名 | サイズ | 推奨 |
|--------|-----------|--------|------|
| Qwen3.5-2B Q5_K_M | `Qwen3.5-2B-Q5_K_M.gguf` | 約 1.7 GB | ★ デフォルト（速度重視） |
| Qwen3.5-4B Q5_K_M | `Qwen3.5-4B-Q5_K_M.gguf` | 約 2.7 GB | 精度重視 |

ダウンロード先：[bartowski/Qwen3.5-2B-Instruct-GGUF（Hugging Face）](https://huggingface.co/bartowski/Qwen3.5-2B-Instruct-GGUF/tree/main)

**この時点でのフォルダ構成:**
```
llama.cpp-windows-vulkan/
├── llama-server.exe
├── llama-cli.exe
├── Qwen3.5-2B-Q5_K_M.gguf              ← ここに置く
└── （各種 .dll）
```

### 手順 4 — Python パッケージをインストール

```bash
pip install -r requirements.txt
```

### 手順 5 — 起動

```bash
python main.py
```

または `AirType_launcher.vbs` をダブルクリック（コンソールなし・バックグラウンド起動）。

初回起動時は llama-server → whisper-server の順でモデルを VRAM に読み込みます。  
GPU・モデルサイズによりますが、**起動完了まで 30〜90 秒**かかります。トレイアイコンが表示されたら準備完了です。

---

## 使い方

1. 起動するとタスクバーのシステムトレイにアイコンが表示される
2. テキストを入力したいアプリ（メモ帳・ブラウザ等）をアクティブにする
3. **「無変換」キーを押しながら** 話す（画面中央に赤い録音インジケーターが表示される）
4. 話し終えたら **キーを離す**
5. 数秒後に認識結果がカーソル位置にペーストされる

### トレイアイコンの操作

右クリックメニューから操作できます：

| メニュー項目 | 内容 |
|------------|------|
| 設定 | STT バックエンド・LLM モデルの切り替え、LLM 整形の ON/OFF |
| 履歴 | 認識テキストの一覧表示・コピー |
| 終了 | アプリを終了（サーバープロセスも停止） |

---

## 設定ファイル

`airtype_config.json` で動作をカスタマイズできます。よく使う項目：

```json
{
  "transcriber": {
    "backend": "sensevoice",   // "whisper" または "sensevoice"
    "language": "ja"
  },
  "audio_duck": {
    "mode": "duck"             // 録音中のシステム音量制御: "duck" / "mute" / "none"
  }
}
```

個人設定は `airtype_config.local.json` に書くとリポジトリに含まれません（`.gitignore` 済み）。

---

## ネットワークモード（2台構成）

GPU 非搭載の PC（ノート PC 等）から、GPU 搭載 PC に音声を転送して処理する構成です。

**ホスト PC（GPU 搭載）で起動:**
```bash
python api_server.py
```

**クライアント PC で起動:**
```bash
python client.py
# または client_launcher.vbs をダブルクリック
```

`airtype_config.json` の `network.server_url` をホスト PC の IP アドレスに変更してください：
```json
"server_url": "http://192.168.x.x:8000/dictate"
```

---

## STT バックエンドの選択

| バックエンド | 設定値 | 特徴 |
|------------|--------|------|
| whisper.cpp | `"whisper"` | Vulkan GPU 加速、多言語対応 |
| SenseVoice Small (ONNX) | `"sensevoice"` | 高速・軽量、日本語特化、DirectML 加速 |

SenseVoice を使う場合は `sensevoice-onnx/` フォルダを親ディレクトリに配置してください。

---

## 既知の制限

| 項目 | 内容 |
|------|------|
| **Windows 専用** | WH_KEYBOARD_LL・IME 制御・CREATE_NO_WINDOW 等の Windows API を多用。WSL 不可 |
| **Vulkan GPU 必須**（LLM 使用時） | GPU 非搭載 PC では llama-server が動作しない。ネットワークモードで回避可 |
| **起動時間** | 初回の llama-server + whisper-server 起動に 30〜90 秒かかる |
| **VRAM 消費** | Qwen3.5-2B + kotoba-whisper-q5 の同時起動で約 3.8 GB（RX 6600 実測） |
| **認識精度** | 小声・環境音で誤認識増加。文境界なしの連続発話で LLM が誤解釈することがある |
| **モデル変更** | Whisper モデルは設定 UI から変更可。LLM モデルの変更はアプリ再起動が必要 |

---

## ライセンス

[MIT License](LICENSE)
