"""
AirType v2 - Step 1: WebRTC VAD による自動発話検出・録音

設計:
- sounddevice で 16kHz/16bit/mono を常時監視
- webrtcvad で 30ms フレームごとに声/無音を判定
- 発話が N フレーム連続 → 録音開始
- 無音が M フレーム連続 → 録音停止 → WAV 保存 → コールバック呼び出し
- ホットキーは「監視モードのON/OFF」に役割変更（録音の開始/停止は自動）
"""

import collections
import tempfile
import threading
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
import webrtcvad


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
SAMPLE_RATE = 16000          # webrtcvad の要件 (8000 / 16000 / 32000 / 48000)
FRAME_MS = 30                # フレーム長: 10 / 20 / 30 ms のいずれか
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)  # 480 samples

VAD_AGGRESSIVENESS = 2       # 0(緩) 〜 3(厳)

SPEECH_START_FRAMES = 3      # 録音開始に必要な連続発話フレーム数 (= 90ms)
SILENCE_END_FRAMES = 33      # 録音終了に必要な連続無音フレーム数 (= 990ms ≈ 1秒)
PRE_BUFFER_FRAMES = 10       # 発話開始前のプリバッファ (= 300ms、頭切れを防ぐ)
MIN_SPEECH_FRAMES = 10       # 最低発話長: これ未満はノイズとしてスキップ (= 300ms)


# ─────────────────────────────────────
# VADRecorder クラス
# ─────────────────────────────────────
class VADRecorder:
    """
    WebRTC VAD を使い、発話区間を自動検出して WAV 保存するクラス。

    使い方:
        def on_ready(wav_path: Path):
            text = transcriber.transcribe(wav_path)

        recorder = VADRecorder(on_audio_ready=on_ready)
        recorder.start_listening()   # 監視開始
        ...
        recorder.stop_listening()    # 監視停止

    Parameters
    ----------
    on_audio_ready : Callable[[Path], None]
        発話区間が確定するたびに呼ばれるコールバック。
        WAV ファイルのパスを受け取る。呼び出し元で削除すること。
    aggressiveness : int
        VAD の感度 (0=緩い, 3=厳しい)。デフォルト 2。
    """

    def __init__(
        self,
        on_audio_ready: Callable[[Path], None],
        aggressiveness: int = VAD_AGGRESSIVENESS,
    ):
        self._vad = webrtcvad.Vad(aggressiveness)
        self._on_audio_ready = on_audio_ready
        self._running = False
        self._thread: Optional[threading.Thread] = None

        print(
            f"[VAD] 設定: aggressiveness={aggressiveness}, "
            f"開始={SPEECH_START_FRAMES}フレーム({SPEECH_START_FRAMES * FRAME_MS}ms), "
            f"終了={SILENCE_END_FRAMES}フレーム({SILENCE_END_FRAMES * FRAME_MS}ms)"
        )

    def start_listening(self):
        """マイク監視ループをバックグラウンドスレッドで開始する。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("[VAD] 監視開始 → 話しかけてください")

    def stop_listening(self):
        """マイク監視ループを停止する。"""
        self._running = False
        print("[VAD] 監視停止")

    # ── 内部ループ ──────────────────────────────────────────
    def _loop(self):
        """sounddevice ストリームを読みながら VAD で発話を検出するメインループ。"""
        pre_buffer: collections.deque = collections.deque(maxlen=PRE_BUFFER_FRAMES)
        recording: list[np.ndarray] = []
        speech_run = 0    # 連続発話フレーム数
        silence_run = 0   # 連続無音フレーム数
        in_speech = False

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
        ) as stream:
            while self._running:
                frame, _ = stream.read(FRAME_SAMPLES)
                is_speech = self._vad.is_speech(frame.tobytes(), SAMPLE_RATE)

                if not in_speech:
                    # ── 待機中: プリバッファを維持しつつ発話開始を待つ ──
                    pre_buffer.append(frame.copy())
                    speech_run = speech_run + 1 if is_speech else 0

                    if speech_run >= SPEECH_START_FRAMES:
                        in_speech = True
                        silence_run = 0
                        recording = list(pre_buffer)
                        print("[VAD] 発話開始 ▶")

                else:
                    # ── 録音中: フレームを蓄積しつつ無音終了を待つ ──
                    recording.append(frame.copy())
                    silence_run = silence_run + 1 if not is_speech else 0

                    if silence_run >= SILENCE_END_FRAMES:
                        in_speech = False
                        speech_run = 0
                        duration_s = len(recording) * FRAME_MS / 1000
                        print(f"[VAD] 発話終了 ■ ({duration_s:.1f}秒)")

                        if len(recording) >= MIN_SPEECH_FRAMES:
                            wav_path = self._save_wav(recording)
                            threading.Thread(
                                target=self._on_audio_ready,
                                args=(wav_path,),
                                daemon=True,
                            ).start()
                        else:
                            print("[VAD] 発話が短すぎるためスキップ")

                        recording = []
                        silence_run = 0

    @staticmethod
    def _save_wav(frames: list[np.ndarray]) -> Path:
        """フレームリストを 16kHz/16bit/mono WAV として一時ファイルに保存する。"""
        audio = np.concatenate(frames, axis=0)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)       # 16-bit
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return Path(tmp.name)
