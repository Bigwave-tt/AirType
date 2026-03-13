"""
AirType - Step 2: whisper.cpp (Vulkan GPU) による音声→テキスト変換

設計:
- whisper-server.exe が存在すればサーバーモード（推奨）
  - アプリ起動時にサーバーをバックグラウンドで一度だけ起動
  - 2回目以降の推論は HTTP API 経由で高速完了
- whisper-server.exe がなければ whisper-cli.exe をサブプロセス呼び出し（フォールバック）
- AMD RX 6600 の Vulkan バックエンドを使用 (ggml-vulkan.dll)
- デフォルトモデル: kotoba-whisper-v2.0-q5_0 (日本語特化・高速・高精度)
- transcribe() は WAV ファイルパスを受け取りテキスト文字列を返す

フォルダ構成:
  AirType/
    AirType/              ← このファイルがある場所
    whisper.cpp-windows-vulkan/
      whisper-server.exe           ← 推奨 (サーバーモード用)
      whisper-cli.exe              ← フォールバック (CLIモード用)
      ggml-kotoba-whisper-v2.0-q5_0.bin   ← 推奨 (速度・精度バランス・約538MB)
      ggml-kotoba-whisper-v2.0.bin         ← 量子化なし完全版 (最高精度・約1.52GB)
      ggml-large-v3.bin                    ← 汎用 (--model large-v3)
      ggml-vulkan.dll
      ...

Kotoba-Whisper GGML ダウンロード (PowerShell):
  # q5_0 バランス型 (推奨・約538MB) ※ 精度は完全版とほぼ同等
  curl.exe -L -o ggml-kotoba-whisper-v2.0-q5_0.bin `
    "https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-ggml/resolve/main/ggml-kotoba-whisper-v2.0-q5_0.bin"

  # 量子化なし完全版 (約1.52GB)
  curl.exe -L -o ggml-kotoba-whisper-v2.0.bin `
    "https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-ggml/resolve/main/ggml-kotoba-whisper-v2.0.bin"
"""

import json
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
# このファイルから見た whisper.cpp フォルダのデフォルト相対パス
_HERE = Path(__file__).parent
_DEFAULT_WHISPER_DIR = _HERE.parent / "whisper.cpp-windows-vulkan"

# モデルのファイル名（ whisper dir からの相対）
_MODEL_FILES = {
    "kotoba-q5":   "ggml-kotoba-whisper-v2.0-q5_0.bin",   # 推奨: 速度・精度バランス (~538MB)
    "kotoba-full": "ggml-kotoba-whisper-v2.0.bin",          # 最高精度: 量子化なし (~1.52GB)
    "large-v3":    "ggml-large-v3.bin",                     # 汎用: 量子化なし (最も遅い)
    "accurate":    "ggml-large-v3-q5_0.bin",                # 汎用: 量子化あり (遅い)
    "turbo":       "ggml-large-v3-turbo-q5_0.bin",          # 汎用: 高速 (やや低精度)
}

# 後方互換性のためモジュールレベルでも公開（デフォルトパスを使用）
WHISPER_DIR    = _DEFAULT_WHISPER_DIR
WHISPER_SERVER = WHISPER_DIR / "whisper-server.exe"
WHISPER_CLI    = WHISPER_DIR / "whisper-cli.exe"
MODELS = {k: WHISPER_DIR / v for k, v in _MODEL_FILES.items()}

DEFAULT_MODEL = "kotoba-q5"

# モデル別タイムアウト (秒) - CLIモード用 (モデルロード込み)
# kotoba は軽量なので 30 秒で十分; large 系は 120 秒
MODEL_TIMEOUTS = {
    "kotoba-q5":   30,
    "kotoba-full": 45,
    "large-v3":    120,
    "accurate":    120,
    "turbo":       60,
}

DEFAULT_LANGUAGE = "ja"

# whisper.cpp の --prompt に渡す初期文脈テキスト
# 技術用語を事前提示することで同音異義語の誤認識を軽減する
# 例: 「軌道」→「起動」、「記録」→「録音」 等
INITIAL_PROMPT = "日本語の音声入力です。"

# サーバーモード設定
_DEFAULT_WHISPER_SERVER_PORT = 18766  # デフォルトポート (config で上書き可)
_STARTUP_TIMEOUT             = 60     # サーバー起動・モデルロード完了までの待機上限 (秒)
_INFER_TIMEOUT               = 30     # サーバーモードの推論タイムアウト (秒)

# タイムスタンプ付き出力行のパターン: [00:00:00.000 --> 00:00:02.860]  テキスト (CLIモード用)
_TIMESTAMP_RE = re.compile(r"^\[[\d:.]+ --> [\d:.]+\]\s*(.*)")

# whisper.cpp が出力するゴミトークンのパターン (短い音声や無音入力時に発生)
# 例: ≪≫、《》、【】、(無音)、[音楽]、[拍手]、字幕:、句読点のみ 等
_NOISE_RE = re.compile(
    # 1. 特殊括弧 ≪≫《》 を含む行 (whisper の典型的ゴミトークン)
    r"[≪≫《》]"
    # 2. 行全体が各種括弧で囲まれている: (無音)、[音楽]、（拍手）、【字幕】 等
    r"|^\s*[\(\[（【〔〈『「][^\)\]）】〕〉』」\n]*[\)\]）】〕〉』」]\s*$"
    # 3. 「字幕:」「字幕：」で始まる行
    r"|^字幕[：:]"
    # 4. 句読点・記号のみで構成された行
    r"|^\s*[。、・…！？!?\s　]+\s*$"
    # 5. 日本語 whisper でよく発生する幻覚フレーズ
    r"|^ご視聴ありがとうございました[。！]?$"
    r"|^チャンネル登録をお願いします[。！]?$"
    r"|^字幕[はを].*提供しています[。]?$"
)


# ─────────────────────────────────────
# WhisperTranscriber クラス
# ─────────────────────────────────────
class WhisperTranscriber:
    """
    whisper-server.exe または whisper-cli.exe (Vulkan GPU) をラップして WAV → テキスト変換を行う。

    whisper-server.exe が存在すればサーバーモード（高速）、
    whisper-cli.exe のみならば CLI サブプロセスモード（互換）で動作する。

    Parameters
    ----------
    language : str | None
        文字起こし言語コード ("ja", "en", 等)。None で自動検出。
    model : str
        使用するモデルキー。MODELS のいずれか。デフォルトは "kotoba-q5"。
    """

    def __init__(
        self,
        language: Optional[str] = DEFAULT_LANGUAGE,
        model: str = DEFAULT_MODEL,
        device: str = "auto",
        startup_gate: Optional[threading.Event] = None,
        whisper_dir: Optional[Path] = None,
        server_port: Optional[int] = None,
    ):
        """
        Parameters
        ----------
        whisper_dir : Path | None
            whisper.cpp フォルダのパス。None の場合はデフォルト相対パスを使用。
        server_port : int | None
            whisper-server が使用するポート。None の場合はデフォルト値を使用。0 で空きポート自動割り当て。
        """
        self.language = language
        self._startup_gate = startup_gate  # このイベントが set されるまでサーバー起動を遅延
        self._server_proc  = None
        self._server_ready = threading.Event()
        self._use_server   = False

        # ── パス解決 ──────────────────────────────────────────────────
        _dir = Path(whisper_dir) if whisper_dir else _DEFAULT_WHISPER_DIR
        self._whisper_server = _dir / "whisper-server.exe"
        self._whisper_cli    = _dir / "whisper-cli.exe"

        # ── ポート解決 ────────────────────────────────────────────────
        _port_cfg = server_port if server_port is not None else _DEFAULT_WHISPER_SERVER_PORT
        if _port_cfg == 0:
            import socket as _sock
            with _sock.socket() as s:
                s.bind(("", 0))
                _port_cfg = s.getsockname()[1]
            print(f"[Transcriber] 空きポートを自動割り当て: {_port_cfg}")
        self._server_port = _port_cfg

        if model not in _MODEL_FILES:
            raise ValueError(f"model は {list(_MODEL_FILES)} のいずれかを指定してください: {model!r}")
        self.model_key  = model
        self.model_path = _dir / _MODEL_FILES[model]
        self.timeout    = MODEL_TIMEOUTS[model]

        if not self.model_path.exists():
            _kotoba_hints = {
                "kotoba-q5":   (
                    "ggml-kotoba-whisper-v2.0-q5_0.bin を whisper.cpp-windows-vulkan フォルダに置いてください。\n"
                    "ダウンロード: curl.exe -L -o ggml-kotoba-whisper-v2.0-q5_0.bin "
                    '"https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-ggml/resolve/main/ggml-kotoba-whisper-v2.0-q5_0.bin"'
                ),
                "kotoba-full": (
                    "ggml-kotoba-whisper-v2.0.bin を whisper.cpp-windows-vulkan フォルダに置いてください。\n"
                    "ダウンロード: curl.exe -L -o ggml-kotoba-whisper-v2.0.bin "
                    '"https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-ggml/resolve/main/ggml-kotoba-whisper-v2.0.bin"'
                ),
            }
            hint = (
                _kotoba_hints.get(model, f"対応するモデルファイルを {WHISPER_DIR} に置いてください。")
                if model.startswith("kotoba")
                else f"対応するモデルファイルを {WHISPER_DIR} に置いてください。"
            )
            raise FileNotFoundError(
                f"モデルファイルが見つかりません: {self.model_path}\n{hint}"
            )

        # ── サーバーモード優先 ────────────────────────────────────────
        if self._whisper_server.exists():
            self._use_server = True
            print(f"[Transcriber] whisper-server.exe: {self._whisper_server}")
            print(f"[Transcriber] モデル: {self.model_path.name}")
            print(f"[Transcriber] バックグラウンドでサーバーを起動中... (ポート {self._server_port})")
            threading.Thread(
                target=self._start_server,
                daemon=True,
                name="WhisperServer",
            ).start()
            return

        # ── CLI サブプロセスモード（フォールバック）──────────────────
        if not self._whisper_cli.exists():
            raise FileNotFoundError(
                f"whisper-server.exe も whisper-cli.exe も見つかりません: {_dir}\n"
                f"whisper.cpp-windows-vulkan フォルダを AirType フォルダと同じ場所に置いてください。"
            )

        print(f"[Transcriber] whisper-cli.exe: {self._whisper_cli}")
        print(f"[Transcriber] モデル: {self.model_path.name}")
        print(f"[Transcriber] タイムアウト: {self.timeout}秒")
        print(f"[Transcriber] バックエンド: Vulkan (AMD RX 6600)")
        print("[Transcriber] 準備完了")

    # ── サーバー起動（バックグラウンドスレッドで実行）────────────────
    def _start_server(self):
        """whisper-server.exe をバックグラウンド起動し、ポート接続確認で準備完了を待つ。"""
        # llama-server が先に VRAM を確保するまで待機する
        # （同時起動すると whisper-server が先に VRAM を取り llama-server が shared メモリに溢れる）
        if self._startup_gate is not None:
            print("[Transcriber] llama-server の VRAM 確保完了を待機中...")
            self._startup_gate.wait(timeout=_STARTUP_TIMEOUT)
            print("[Transcriber] llama-server 準備完了を確認。whisper-server を起動します。")

        cmd = [
            str(self._whisper_server),
            "-m",     str(self.model_path),
            "--port", str(self._server_port),
            "--host", "127.0.0.1",
        ]
        try:
            self._server_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            print(f"[Transcriber] サーバー起動失敗: {e}")
            self._use_server = False
            return

        deadline = time.time() + _STARTUP_TIMEOUT
        while time.time() < deadline:
            # プロセスが早期終了していたら失敗
            if self._server_proc.poll() is not None:
                print("[Transcriber] whisper-server が予期せず終了しました")
                self._use_server = False
                return
            try:
                with socket.create_connection(("127.0.0.1", self._server_port), timeout=1):
                    print("[Transcriber] whisper-server 準備完了（以降の推論は高速になります）")
                    self._server_ready.set()
                    return
            except (ConnectionRefusedError, OSError):
                pass
            time.sleep(1.0)

        print(f"[Transcriber] whisper-server が {_STARTUP_TIMEOUT}秒 以内に起動しませんでした")
        self._server_proc.terminate()
        self._server_proc = None
        self._use_server  = False

    # ── 公開 API ─────────────────────────────────────────────────────
    def transcribe(self, wav_path: Path) -> str:
        """
        WAV ファイルを文字起こしして結合テキストを返す。

        Parameters
        ----------
        wav_path : Path
            文字起こし対象の WAV ファイルパス

        Returns
        -------
        str
            結合された文字起こしテキスト
        """
        if not wav_path.exists():
            raise FileNotFoundError(f"WAV ファイルが見つかりません: {wav_path}")

        print(f"[Transcriber] 文字起こし開始: {wav_path.name}")
        self._check_audio_level(wav_path)

        if self._use_server:
            return self._transcribe_server(wav_path)
        return self._transcribe_cli(wav_path)

    def shutdown(self):
        """サーバープロセスを終了する（アプリ終了時に呼ぶ）。"""
        if self._server_proc and self._server_proc.poll() is None:
            print("[Transcriber] whisper-server 終了中...")
            self._server_proc.terminate()
            try:
                self._server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._server_proc.kill()
            print("[Transcriber] whisper-server 終了しました")
        self._server_proc = None

    # ── 内部メソッド: サーバーモード ─────────────────────────────────
    def _transcribe_server(self, wav_path: Path) -> str:
        """whisper-server HTTP API 経由で文字起こしを行う。"""
        # サーバーが準備完了するまで待機
        if not self._server_ready.wait(timeout=_STARTUP_TIMEOUT):
            print("[Transcriber] サーバー準備待機タイムアウト。CLIモードにフォールバック")
            return self._transcribe_cli(wav_path)

        print(f"[Transcriber] 文字起こし開始 (サーバー): {wav_path.name}")
        try:
            response = self._send_multipart(wav_path)
            full_text = self._parse_server_response(response)
        except Exception as e:
            print(f"[Transcriber] サーバーエラー ({type(e).__name__}): {e}。CLIにフォールバック")
            return self._transcribe_cli(wav_path)

        if not full_text:
            print("[Transcriber] WARNING: テキストが取得できませんでした")

        print(f"[Transcriber] 文字起こし結果:\n  → {full_text!r}")
        return full_text

    def _send_multipart(self, wav_path: Path) -> dict:
        """WAV ファイルをマルチパート POST で whisper-server に送信し JSON を返す。"""
        boundary = "----AirTypeWhisperBoundary"
        audio_bytes = wav_path.read_bytes()
        filename = wav_path.name

        # multipart/form-data ボディを手動構築
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: audio/wav\r\n"
            f"\r\n"
        ).encode("utf-8") + audio_bytes + (
            f"\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="language"\r\n'
            f"\r\n"
            f"{self.language or 'auto'}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="prompt"\r\n'
            f"\r\n"
            f"{INITIAL_PROMPT}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="response_format"\r\n'
            f"\r\n"
            f"verbose_json\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        req = urllib.request.Request(
            f"http://127.0.0.1:{self._server_port}/inference",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_INFER_TIMEOUT) as resp:
            return json.loads(resp.read())

    @staticmethod
    def _parse_server_response(response: dict) -> str:
        """
        whisper-server の verbose_json レスポンスからテキストを抽出する。

        segments がある場合はセグメント単位でノイズフィルタを適用。
        segments がない場合は text フィールドを使用。
        """
        segments = response.get("segments", [])
        if segments:
            texts = []
            for seg in segments:
                text = seg.get("text", "").strip()
                if text and not _NOISE_RE.search(text):
                    texts.append(text)
            return "".join(texts)

        # segments がない場合は text フィールド全体を使用
        return response.get("text", "").strip()

    # ── 内部メソッド: CLI サブプロセスモード ─────────────────────────
    def _transcribe_cli(self, wav_path: Path) -> str:
        """whisper-cli.exe サブプロセス経由で文字起こしを行う。"""
        if not self._whisper_cli.exists():
            raise FileNotFoundError(
                f"whisper-cli.exe が見つかりません: {self._whisper_cli}"
            )

        print(f"[Transcriber] 文字起こし開始 (CLI): {wav_path.name}")
        cmd = [
            str(self._whisper_cli),
            "-m", str(self.model_path),
            "-f", str(wav_path),
            "-l", self.language or "auto",
            "--prompt", INITIAL_PROMPT,  # 技術用語の同音異義語誤認識を軽減
            # NOTE: Vulkan GPU は whisper.cpp のビルド時に組み込み済みのため
            # -ngl (GPU レイヤー数) の指定は不要。指定するとこのビルドでは無音終了する。
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,  # コマンドプロンプトが点滅しないよう非表示
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"whisper-cli.exe が失敗しました (code={result.returncode}):\n{result.stderr}"
            )

        full_text = self._parse_cli_output(result.stdout)
        if not full_text:
            print("[Transcriber] WARNING: テキストが取得できませんでした")
            print(f"[Transcriber] DEBUG returncode: {result.returncode}")
            print("[Transcriber] DEBUG stdout (last 30 lines):")
            for line in result.stdout.splitlines()[-30:]:
                print(f"  | {line}")
            if result.stderr:
                print("[Transcriber] DEBUG stderr (last 20 lines):")
                for line in result.stderr.splitlines()[-20:]:
                    print(f"  ! {line}")
        print(f"[Transcriber] 文字起こし結果:\n  → {full_text!r}")
        return full_text

    @staticmethod
    def _check_audio_level(wav_path: Path) -> None:
        """WAVファイルの音量を確認してデバッグ情報を出力する。"""
        try:
            data = wav_path.read_bytes()
            # WAV データ部分 (44バイトヘッダー以降) を16bitサンプルとして読む
            samples = struct.unpack_from(f"<{(len(data) - 44) // 2}h", data, 44)
            if samples:
                peak = max(abs(s) for s in samples)
                rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                print(f"[Transcriber] 音量チェック: peak={peak}, rms={rms:.1f} (無音判定: peak<100)")
                if peak < 100:
                    print("[Transcriber] WARNING: 録音がほぼ無音です。マイク設定を確認してください。")
        except Exception:
            pass  # 音量チェック失敗は無視

    @staticmethod
    def _parse_cli_output(output: str) -> str:
        """
        whisper-cli.exe の出力からテキスト部分を抽出して結合する。

        出力例:
          [00:00:00.000 --> 00:00:02.860]  音声入力のテストです
          [00:00:02.860 --> 00:00:05.780]  文字起こしができるかどうかをやってみましょう
        """
        texts = []
        for line in output.splitlines():
            m = _TIMESTAMP_RE.match(line.strip())
            if m:
                text = m.group(1).strip()
                if text and not _NOISE_RE.search(text):
                    texts.append(text)

        return "".join(texts)


# ─────────────────────────────────────
# 単体テスト用エントリポイント
# ─────────────────────────────────────
def _test_with_existing_file(wav_path: str, model: str = DEFAULT_MODEL):
    transcriber = WhisperTranscriber(model=model)
    text = transcriber.transcribe(Path(wav_path))
    print(f"\n結果: {text}")
    transcriber.shutdown()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        model_arg = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL
        _test_with_existing_file(sys.argv[1], model=model_arg)
    else:
        print("使い方: python step2_transcriber.py <wav_file> [model]")
        print(f"  model: {list(MODELS)} (デフォルト: {DEFAULT_MODEL})")
