"""
AirType - Step 2: whisper.cpp (Vulkan GPU) による音声→テキスト変換

設計:
- whisper-cli.exe を subprocess で呼び出す
- AMD RX 6600 の Vulkan バックエンドを使用 (ggml-vulkan.dll)
- デフォルトモデル: kotoba-whisper-v2.0-q5_0 (日本語特化・高速・高精度)
- transcribe() は WAV ファイルパスを受け取りテキスト文字列を返す

フォルダ構成:
  AirType/
    AirType/              ← このファイルがある場所
    whisper.cpp-windows-vulkan/
      whisper-cli.exe
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

import re
import struct
import subprocess
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
# このファイルから見た whisper.cpp フォルダの相対パス
_HERE = Path(__file__).parent
WHISPER_DIR = _HERE.parent / "whisper.cpp-windows-vulkan"
WHISPER_CLI = WHISPER_DIR / "whisper-cli.exe"

# 選択可能なモデル
# kotoba-whisper-v2.0: デコーダー層を 32→2 に蒸留した日本語特化モデル
#   - 本家 large-v3 と同等の日本語精度
#   - モデルが軽いためタイムアウトリスクが物理的に消滅
MODELS = {
    "kotoba-q5":   WHISPER_DIR / "ggml-kotoba-whisper-v2.0-q5_0.bin",  # 推奨: 速度・精度バランス (~538MB)
    "kotoba-full": WHISPER_DIR / "ggml-kotoba-whisper-v2.0.bin",        # 最高精度: 量子化なし (~1.52GB)
    "large-v3":    WHISPER_DIR / "ggml-large-v3.bin",                   # 汎用: 量子化なし (最も遅い)
    "accurate":    WHISPER_DIR / "ggml-large-v3-q5_0.bin",              # 汎用: 量子化あり (遅い)
    "turbo":       WHISPER_DIR / "ggml-large-v3-turbo-q5_0.bin",        # 汎用: 高速 (やや低精度)
}
DEFAULT_MODEL = "kotoba-q5"

# モデル別タイムアウト (秒)
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

# タイムスタンプ付き出力行のパターン: [00:00:00.000 --> 00:00:02.860]  テキスト
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
    whisper-cli.exe (Vulkan GPU) をラップして WAV → テキスト変換を行う。

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
    ):
        self.language = language

        if model not in MODELS:
            raise ValueError(f"model は {list(MODELS)} のいずれかを指定してください: {model!r}")
        self.model_key = model
        self.model_path = MODELS[model]
        self.timeout = MODEL_TIMEOUTS[model]

        if not WHISPER_CLI.exists():
            raise FileNotFoundError(
                f"whisper-cli.exe が見つかりません: {WHISPER_CLI}\n"
                f"whisper.cpp-windows-vulkan フォルダを AirType フォルダと同じ場所に置いてください。"
            )
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

        print(f"[Transcriber] whisper-cli.exe: {WHISPER_CLI}")
        print(f"[Transcriber] モデル: {self.model_path.name}")
        print(f"[Transcriber] タイムアウト: {self.timeout}秒")
        print(f"[Transcriber] バックエンド: Vulkan (AMD RX 6600)")
        print("[Transcriber] 準備完了")

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

        cmd = [
            str(WHISPER_CLI),
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

        full_text = self._parse_output(result.stdout)
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
    def _parse_output(output: str) -> str:
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


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        model_arg = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL
        _test_with_existing_file(sys.argv[1], model=model_arg)
    else:
        print("使い方: python step2_transcriber.py <wav_file> [model]")
        print(f"  model: {list(MODELS)} (デフォルト: {DEFAULT_MODEL})")
