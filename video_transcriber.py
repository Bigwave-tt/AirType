"""
AirType - 動画ファイルからの音声抽出ユーティリティ

ffmpeg で動画から音声を 16kHz モノラル WAV に変換する。
変換後の WAV は WhisperTranscriber.transcribe() に直接渡せる形式。
"""

import shutil
import subprocess
from pathlib import Path


def find_ffmpeg() -> str | None:
    """ffmpeg 実行ファイルのパスを返す。見つからなければ None。

    探索順:
    1. システム PATH
    2. アプリフォルダ直下の ffmpeg/ffmpeg.exe
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = Path(__file__).parent.parent / "ffmpeg" / "ffmpeg.exe"
    if local.exists():
        return str(local)
    return None


def extract_audio(video_path: Path, wav_path: Path, ffmpeg_exe: str = "ffmpeg") -> None:
    """動画ファイルから音声を WAV (16kHz, mono, PCM s16le) で抽出する。

    Parameters
    ----------
    video_path : Path
        入力動画ファイル
    wav_path : Path
        出力 WAV ファイルパス（既存の場合は上書き）
    ffmpeg_exe : str
        ffmpeg 実行ファイルのパス

    Raises
    ------
    RuntimeError
        ffmpeg が 0 以外の終了コードを返した場合
    """
    cmd = [
        ffmpeg_exe,
        "-y",                    # 上書き確認なし
        "-i", str(video_path),
        "-vn",                   # 映像ストリームを除外
        "-acodec", "pcm_s16le",  # 16bit PCM
        "-ar", "16000",          # 16kHz（whisper.cpp 推奨サンプルレート）
        "-ac", "1",              # モノラル
        str(wav_path),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg が失敗しました (code={result.returncode}):\n"
            f"{result.stderr[-2000:]}"
        )
