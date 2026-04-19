# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 起動・実行

```bash
# 開発・デバッグ用（コンソールあり）
python AirType/main.py

# 各ステップの単体テスト
python AirType/step2_transcriber.py <wav_file> [model_key]
python AirType/step3_refiner.py [model_key]
python AirType/step4_paster.py
```

通常使用は `AirType_launcher.vbs`（pythonw.exe 経由）。ログは `AirType/airtype.log` に出力される。

## 外部バイナリ（Python 外）

リポジトリには含まれない。`AirType/` の親ディレクトリに配置する：

```
whisper.cpp-windows-vulkan/   ← STT バックエンド
llama.cpp-windows-vulkan/     ← LLM バックエンド
```

パスは `airtype_config.json` の `whisper.dir` / `llama.dir` で上書き可能。空文字でデフォルト相対パスを使用。

## アーキテクチャ

### パイプライン

```
PTTキー(無変換) → Recorder → WAV Queue → WhisperTranscriber → LlamaRefiner → Paster
```

`main.py` の Worker スレッドがキューを消費してパイプラインを順次実行する。状態は `IDLE / RECORDING / PROCESSING` の3値で管理し、PROCESSING 中の再トリガーは無視する。

### 各ステップの責務

| ファイル | クラス | 入出力 |
|---------|--------|--------|
| `step1_recorder.py` | `Recorder` | マイク → 16kHz mono WAV (tempfile) |
| `step2_transcriber.py` | `WhisperTranscriber` | WAV → 生テキスト（whisper.cpp Vulkan） |
| `step3_refiner.py` | `LlamaRefiner` / `RuleBasedRefiner` | 生テキスト → 整形テキスト（llama.cpp Vulkan） |
| `step4_paster.py` | `Paster` | テキスト → クリップボード + Ctrl+V |
| `step5_gui.py` | `TrayIcon` / `SettingsWindow` / `HistoryWindow` / `OSD` | GUI 全般 |

### サーバーモード（STT・LLM 共通パターン）

両ステップとも `*.exe` をバックグラウンドスレッドで起動し、HTTP API で推論する常駐サーバー方式を優先する。EXE が存在しない場合は CLI サブプロセスモードにフォールバック。

- **起動順序が重要**: llama-server が VRAM を確保してから whisper-server を起動する（`startup_gate` イベント）。RX 6600 の VRAM 8GB に対して両サーバー合計が上限に近いため。
- ポート: llama-server=`18765`、whisper-server=`18766`（config で変更可）

### 設定

`airtype_config.json` ですべての実行時パラメータを管理。`config.py` の `load_config()` が読み込み、各ステップのコンストラクタに渡す。

### ネットワークモード

`api_server.py`（FastAPI）+ `client.py` の2台構成。`client.py` は録音のみ行い WAV を HTTP POST でホストに送信。`api_server.py` は STT・LLM を実行してテキストを返す。`main.py` と同じ step2/step3 を再利用する。

## 重要な実装上の制約

- **Windows 専用**: `WH_KEYBOARD_LL`・IME 制御・`CREATE_NO_WINDOW` を多用。WSL では動作しない。
- **忠実度チェック** (`_is_faithful`): LLM の過剰編集を `SequenceMatcher.ratio() < 0.85` で検出してルールベースにフォールバック。テキストを大幅に変える変更は意図的に拒否される。
- **ノイズフィルタ** (`_NOISE_RE`): whisper.cpp が出力する幻覚トークン（`≪≫`、`(無音)` 等）をセグメント単位で除去。
- **ASCII 支配チェック**: 英字の 80% 超が ASCII の場合、LLM が日本語→英語に変換したと判断して生テキストを使用する。
