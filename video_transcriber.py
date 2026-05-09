"""
AirType - 動画ファイルからの音声抽出ユーティリティ

ffmpeg で動画から音声を 16kHz モノラル WAV に変換する。
変換後の WAV は WhisperTranscriber.transcribe() に直接渡せる形式。
"""

import re
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


def get_duration(video_path: Path, ffmpeg_exe: str = "ffmpeg") -> float:
    """動画/音声ファイルの長さ（秒）を返す。取得失敗時は 0.0。"""
    result = subprocess.run(
        [ffmpeg_exe, "-i", str(video_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    # ffmpeg -i は終了コード 1 を返すが stderr に Duration が含まれる
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + mn * 60 + s
    return 0.0


def extract_audio_chunk(
    video_path: Path,
    wav_path: Path,
    start_sec: float,
    duration_sec: float,
    ffmpeg_exe: str = "ffmpeg",
) -> None:
    """動画ファイルの指定区間を WAV (16kHz, mono, PCM s16le) で抽出する。

    Parameters
    ----------
    start_sec : float
        抽出開始位置（秒）
    duration_sec : float
        抽出する長さ（秒）
    """
    cmd = [
        ffmpeg_exe,
        "-y",
        "-ss", str(start_sec),   # 入力シーク（高速）
        "-i", str(video_path),
        "-t", str(duration_sec),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
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
