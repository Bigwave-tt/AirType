"""
AirType - Step 2: Kotoba-Whisper (faster-whisper) による音声→テキスト変換

設計:
- WhisperTranscriber クラスで faster-whisper をラップ
- モデル: kotoba-tech/kotoba-whisper-v2.0-faster (日本語特化・高精度)
  - kotoba-tech 公式 CTranslate2 変換済みモデル
  - 初回起動時に HuggingFace Hub から自動ダウンロード (~/.cache/huggingface)
  - CTranslate2 形式で変換済みのため faster-whisper で直接利用可能
- AMD GPU (ROCm) / NVIDIA GPU (CUDA) / CPU を自動選択
- transcribe() は WAV ファイルパスを受け取りテキスト文字列を返す
- 単体テスト: このファイルを直接実行すると Step 1 の Recorder と連携して動作確認できる

AMD RX 6600 (ROCm) セットアップ:
  Linux:   pip install ctranslate2  # ROCm ビルド済みホイールを使用
  Windows: ROCm 5.7+ が必要。pip install torch --index-url https://download.pytorch.org/whl/rocm5.7
"""

from pathlib import Path
from typing import Optional

from faster_whisper import WhisperModel


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
# Kotoba-Whisper v2.0 (kotoba-tech 公式 CTranslate2変換済み・faster-whisper対応)
# large-v3 ベースの日本語特化モデル。標準 large-v3 より高精度。
KOTOBA_MODEL_ID = "kotoba-tech/kotoba-whisper-v2.0-faster"

# フォールバック: HuggingFace にアクセスできない場合は standard Whisper を使用
FALLBACK_MODEL_SIZE = "large-v3"

DEFAULT_LANGUAGE = "ja"        # None にすると自動検出 (精度は下がる)
COMPUTE_TYPE_GPU = "float16"   # GPU 使用時の量子化
COMPUTE_TYPE_CPU = "int8"      # CPU 使用時の量子化 (速度優先)


# ─────────────────────────────────────
# WhisperTranscriber クラス
# ─────────────────────────────────────
class WhisperTranscriber:
    """
    Kotoba-Whisper (faster-whisper) をラップして WAV → テキスト変換を行う。

    Parameters
    ----------
    language : str | None
        文字起こし言語コード ("ja", "en", 等)。None で自動検出。
    device : str
        "cuda" / "cpu" / "auto"。
        auto は CUDA (NVIDIA) → ROCm (AMD) → CPU の順でフォールバック。
    use_kotoba : bool
        True で Kotoba-Whisper v2.2-faster (日本語特化) を使用。
        False で standard faster-whisper (large-v3) にフォールバック。
    """

    def __init__(
        self,
        language: Optional[str] = DEFAULT_LANGUAGE,
        device: str = "auto",
        use_kotoba: bool = False,
    ):
        self.language = language

        actual_device, compute_type = self._resolve_device(device)
        model_id = KOTOBA_MODEL_ID if use_kotoba else FALLBACK_MODEL_SIZE

        print(f"[Transcriber] モデル読み込み中: {model_id}")
        print(f"[Transcriber] device={actual_device}  compute={compute_type}")

        if use_kotoba:
            self.model = self._load_with_fallback(model_id, actual_device, compute_type)
        else:
            self.model = WhisperModel(model_id, device=actual_device, compute_type=compute_type)
        print("[Transcriber] モデル準備完了")

    @staticmethod
    def _load_with_fallback(model_id: str, device: str, compute_type: str) -> WhisperModel:
        """Kotoba-Whisper のロードを試み、失敗時は large-v3 にフォールバックする。"""
        try:
            return WhisperModel(model_id, device=device, compute_type=compute_type)
        except Exception as e:
            print(f"[Transcriber] Kotoba-Whisper ロード失敗: {e}")
            print(f"[Transcriber] large-v3 にフォールバックします")
            return WhisperModel(FALLBACK_MODEL_SIZE, device=device, compute_type=compute_type)

    @staticmethod
    def _resolve_device(device: str) -> tuple[str, str]:
        """
        device="auto" のとき CUDA/ROCm → CPU の順で選択する。
        AMD ROCm は torch の CUDA インターフェース経由で検出できる。
        """
        if device != "auto":
            compute_type = COMPUTE_TYPE_GPU if device == "cuda" else COMPUTE_TYPE_CPU
            return device, compute_type

        try:
            import torch
            if torch.cuda.is_available():
                # NVIDIA CUDA または AMD ROCm (HIP) が利用可能
                gpu_name = torch.cuda.get_device_name(0)
                print(f"[Transcriber] GPU 検出: {gpu_name}")
                return "cuda", COMPUTE_TYPE_GPU
        except ImportError:
            pass

        return "cpu", COMPUTE_TYPE_CPU

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

        segments, info = self.model.transcribe(
            str(wav_path),
            language=self.language,
            beam_size=5,
            temperature=0,                      # 決定的出力 (ハルシネーション抑制)
            condition_on_previous_text=False,   # 前セグメントの影響を排除
            vad_filter=True,                    # 無音部分をスキップして精度向上
            vad_parameters={
                "min_silence_duration_ms": 500,
            },
        )

        segment_texts = [seg.text.strip() for seg in segments]
        full_text = " ".join(segment_texts)

        print(f"[Transcriber] 検出言語: {info.language} (確率: {info.language_probability:.2f})")
        print(f"[Transcriber] 文字起こし結果:\n  → {full_text!r}")

        return full_text


# ─────────────────────────────────────
# 単体テスト用エントリポイント
# ─────────────────────────────────────
def _test_with_recorder():
    from step1_recorder import HotkeyController, Recorder

    transcriber = WhisperTranscriber()

    def on_recorded(wav_path: Path):
        try:
            text = transcriber.transcribe(wav_path)
            print(f"\n{'='*40}")
            print(f"文字起こし完了: {text}")
            print(f"{'='*40}\n")
        finally:
            wav_path.unlink(missing_ok=True)
            print(f"[cleanup] 一時ファイルを削除: {wav_path}")

    recorder = Recorder()
    controller = HotkeyController(recorder=recorder, on_recorded=on_recorded)

    try:
        controller.start_listening()
    except KeyboardInterrupt:
        print("\n[test] 終了します")


def _test_with_existing_file(wav_path: str):
    transcriber = WhisperTranscriber()
    text = transcriber.transcribe(Path(wav_path))
    print(f"\n結果: {text}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        _test_with_existing_file(sys.argv[1])
    else:
        _test_with_recorder()
