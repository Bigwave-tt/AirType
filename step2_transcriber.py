"""
AirType - Step 2: faster-whisper による音声→テキスト変換

設計:
- WhisperTranscriber クラスで faster-whisper をラップ
- モデルは初回起動時にダウンロード・キャッシュされる (~/.cache/huggingface)
- GPU (CUDA) があれば自動利用、なければ CPU にフォールバック
- transcribe() は WAV ファイルパスを受け取りテキスト文字列を返す
- 単体テスト: このファイルを直接実行すると Step 1 の Recorder と連携して動作確認できる
"""

from pathlib import Path
from typing import Optional

from faster_whisper import WhisperModel


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
DEFAULT_MODEL_SIZE = "small"   # tiny / base / small / medium / large-v3
DEFAULT_LANGUAGE = "ja"        # None にすると自動検出 (精度は下がる)
COMPUTE_TYPE_GPU = "float16"   # GPU 使用時の量子化
COMPUTE_TYPE_CPU = "int8"      # CPU 使用時の量子化 (速度優先)


# ─────────────────────────────────────
# WhisperTranscriber クラス
# ─────────────────────────────────────
class WhisperTranscriber:
    """
    faster-whisper をラップして WAV → テキスト変換を行う。

    Parameters
    ----------
    model_size : str
        使用するモデルサイズ ("tiny", "base", "small", "medium", "large-v3")
        日本語の実用精度には "small" 以上を推奨。
    language : str | None
        文字起こし言語コード ("ja", "en", 等)。None で自動検出。
    device : str
        "cuda" / "cpu" / "auto"。auto は CUDA 優先でフォールバック。
    """

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL_SIZE,
        language: Optional[str] = DEFAULT_LANGUAGE,
        device: str = "auto",
    ):
        self.language = language

        actual_device, compute_type = self._resolve_device(device)
        print(f"[Transcriber] モデル読み込み中: {model_size}  device={actual_device}  compute={compute_type}")

        self.model = WhisperModel(
            model_size,
            device=actual_device,
            compute_type=compute_type,
        )
        print(f"[Transcriber] モデル準備完了")

    @staticmethod
    def _resolve_device(device: str) -> tuple[str, str]:
        """
        device="auto" のとき CUDA 利用可能なら GPU、なければ CPU を選択する。
        """
        if device == "auto":
            try:
                import torch
                if torch.cuda.is_available():
                    return "cuda", COMPUTE_TYPE_GPU
            except ImportError:
                pass
            return "cpu", COMPUTE_TYPE_CPU
        compute_type = COMPUTE_TYPE_GPU if device == "cuda" else COMPUTE_TYPE_CPU
        return device, compute_type

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
            結合された文字起こしテキスト (セグメント間をスペースで連結)
        """
        if not wav_path.exists():
            raise FileNotFoundError(f"WAV ファイルが見つかりません: {wav_path}")

        print(f"[Transcriber] 文字起こし開始: {wav_path.name}")

        segments, info = self.model.transcribe(
            str(wav_path),
            language=self.language,
            beam_size=5,
            temperature=0,                      # 決定的出力 (ハルシネーション抑制)
            condition_on_previous_text=False,   # 前セグメントの影響を排除
            vad_filter=True,           # 無音部分をスキップして精度向上
            vad_parameters={
                "min_silence_duration_ms": 500,  # 500ms 以上の無音をカット
            },
        )

        # セグメントはジェネレータなので list 化してから結合
        segment_texts = [seg.text.strip() for seg in segments]
        full_text = " ".join(segment_texts)

        print(f"[Transcriber] 検出言語: {info.language} (確率: {info.language_probability:.2f})")
        print(f"[Transcriber] 文字起こし結果:\n  → {full_text!r}")

        return full_text


# ─────────────────────────────────────
# 単体テスト用エントリポイント
# ─────────────────────────────────────
def _test_with_recorder():
    """
    Step 1 の Recorder と組み合わせて動作確認する。
    Ctrl+Shift+Space で録音 → 停止 → 文字起こし を試せる。
    """
    # Step 1 のモジュールをインポート
    from step1_recorder import HotkeyController, Recorder

    transcriber = WhisperTranscriber()

    def on_recorded(wav_path: Path):
        try:
            text = transcriber.transcribe(wav_path)
            print(f"\n{'='*40}")
            print(f"文字起こし完了: {text}")
            print(f"{'='*40}\n")
        finally:
            # 確認後にファイルを削除
            wav_path.unlink(missing_ok=True)
            print(f"[cleanup] 一時ファイルを削除: {wav_path}")

    recorder = Recorder()
    controller = HotkeyController(recorder=recorder, on_recorded=on_recorded)

    try:
        controller.start_listening()
    except KeyboardInterrupt:
        print("\n[test] 終了します")


def _test_with_existing_file(wav_path: str):
    """既存の WAV ファイルで文字起こしをテストする"""
    transcriber = WhisperTranscriber()
    text = transcriber.transcribe(Path(wav_path))
    print(f"\n結果: {text}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        # 引数に WAV ファイルパスが指定された場合はそのファイルをテスト
        _test_with_existing_file(sys.argv[1])
    else:
        # 引数なしの場合はリアルタイム録音テスト
        _test_with_recorder()
