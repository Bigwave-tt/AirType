"""
AirType - Step 2: whisper.cpp (Vulkan GPU) による音声→テキスト変換

設計:
- whisper-cli.exe を subprocess で呼び出す
- AMD RX 6600 の Vulkan バックエンドを使用 (ggml-vulkan.dll)
- モデル: ggml-large-v3.bin (whisper.cpp フォルダ内)
- transcribe() は WAV ファイルパスを受け取りテキスト文字列を返す

フォルダ構成:
  AirType/
    AirType/              ← このファイルがある場所
    whisper.cpp-windows-vulkan/
      whisper-cli.exe
      ggml-large-v3.bin
      ggml-vulkan.dll
      ...
"""

import re
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
WHISPER_MODEL = WHISPER_DIR / "ggml-large-v3-turbo-q8_0.bin"

DEFAULT_LANGUAGE = "ja"

# whisper.cpp の --prompt に渡す初期文脈テキスト
# 技術用語を事前提示することで同音異義語の誤認識を軽減する
# 例: 「軌道」→「起動」、「記録」→「録音」 等
INITIAL_PROMPT = (
    "AirTypeは音声入力ツールです。"
    "起動、録音、文字起こし、整形、ペースト、GPU、Vulkan、モデル、"
    "プログラム、コード、変換、認識、処理、設定、実行、停止、"
    "ファイル、フォルダ、パス、ダウンロード、インストール。"
)

# タイムスタンプ付き出力行のパターン: [00:00:00.000 --> 00:00:02.860]  テキスト
_TIMESTAMP_RE = re.compile(r"^\[[\d:.]+ --> [\d:.]+\]\s*(.*)")

# whisper.cpp が出力するゴミトークンのパターン (短い音声や --prompt 時に発生)
# 例: ≪≫、(無音)、[音楽]、[拍手] 等
_NOISE_RE = re.compile(
    r"[≪≫《》【】\[\(（]"      # 括弧類で始まるトークン
    r"|^\s*[\(\[（【][^\)\]）】]*[\)\]）】]\s*$"  # 行全体が括弧で囲まれている
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
    use_kotoba : bool
        後方互換のために残しているが現在は無効 (whisper.cpp を使用)。
    """

    def __init__(
        self,
        language: Optional[str] = DEFAULT_LANGUAGE,
        device: str = "auto",
        use_kotoba: bool = False,
    ):
        self.language = language

        if not WHISPER_CLI.exists():
            raise FileNotFoundError(
                f"whisper-cli.exe が見つかりません: {WHISPER_CLI}\n"
                f"whisper.cpp-windows-vulkan フォルダを AirType フォルダと同じ場所に置いてください。"
            )
        if not WHISPER_MODEL.exists():
            raise FileNotFoundError(
                f"モデルファイルが見つかりません: {WHISPER_MODEL}\n"
                f"ggml-large-v3.bin を {WHISPER_DIR} に置いてください。"
            )

        print(f"[Transcriber] whisper-cli.exe: {WHISPER_CLI}")
        print(f"[Transcriber] モデル: {WHISPER_MODEL.name}")
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

        cmd = [
            str(WHISPER_CLI),
            "-m", str(WHISPER_MODEL),
            "-f", str(wav_path),
            "-l", self.language or "auto",
            "--prompt", INITIAL_PROMPT,  # 技術用語の同音異義語誤認識を軽減
            "--no-prints",   # 進捗ログを抑制
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"whisper-cli.exe が失敗しました (code={result.returncode}):\n{result.stderr}"
            )

        full_text = self._parse_output(result.stdout)
        print(f"[Transcriber] 文字起こし結果:\n  → {full_text!r}")
        return full_text

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
def _test_with_existing_file(wav_path: str):
    transcriber = WhisperTranscriber()
    text = transcriber.transcribe(Path(wav_path))
    print(f"\n結果: {text}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        _test_with_existing_file(sys.argv[1])
    else:
        print("使い方: python step2_transcriber.py <wav_file>")
