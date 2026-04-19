"""
AirType - Step 2 (SenseVoice): SenseVoiceSmall (ONNX + DirectML) による音声→テキスト変換

設計:
- onnxruntime-directml で AMD GPU (DirectML) を使用
- InferenceSession は __init__ で一度だけ作成・保持（DirectML 初期化オーバーヘッド回避）
- transcribe() は WhisperTranscriber と同一インターフェースで差し替え可能

前処理パイプライン:
  WAV (16kHz mono)
    → FBank 特徴量 (80 次元, 25ms 窓, 10ms シフト)
    → CMVN 正規化 (am.mvn の mean/std を使用)
    → ONNX Encoder (sense-voice-encoder.onnx)
        入力 1: FBank 特徴量
        入力 2: 言語埋め込み (embedding.npy から ja を選択)
    → CTC ロジット
    → SentencePiece BPE デコード (chn_jpn_yue_eng_ko_spectok.bpe.model)
    → テキスト

フォルダ構成:
  AirType の親フォルダ/
    sensevoice-onnx/
      sense-voice-encoder.onnx               ← ONNX エンコーダー本体
      chn_jpn_yue_eng_ko_spectok.bpe.model   ← SentencePiece トークナイザー
      am.mvn                                 ← CMVN 統計 (mean/std)
      embedding.npy                          ← 言語埋め込み行列
      (config.yaml)                          ← 前処理パラメータ (任意)
"""

import json
import re
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
import onnxruntime as ort


# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
_HERE = Path(__file__).parent
_DEFAULT_SENSEVOICE_DIR = _HERE.parent / "sensevoice-onnx"

# FBank パラメータ (FunASR デフォルト値)
# config.yaml が存在する場合はそちらの値で上書きする
_FBANK_SAMPLE_RATE  = 16000
_FBANK_NUM_MELS     = 80
_FBANK_FRAME_LENGTH = 0.025   # 25 ms
_FBANK_FRAME_SHIFT  = 0.010   # 10 ms
_FBANK_PREEMPH      = 0.97

# 言語コード → 埋め込み行インデックスのマッピング (FunASR SenseVoice 準拠)
# TODO: 実際の embedding.npy の軸順序を確認して修正する
_LANG_MAP = {
    "zh": 0,   # 中国語
    "en": 1,   # 英語
    "yue": 2,  # 広東語
    "ja": 3,   # 日本語
    "ko": 4,   # 韓国語
    "auto": 0, # 自動判定はひとまず中国語インデックスを仮置き
}


# ─────────────────────────────────────
# SenseVoiceTranscriber クラス
# ─────────────────────────────────────
class SenseVoiceTranscriber:
    """
    SenseVoiceSmall (ONNX) をラップして WAV → テキスト変換を行う。

    Parameters
    ----------
    model_dir : Path | None
        sensevoice-onnx/ フォルダのパス。None の場合はデフォルト相対パスを使用。
    language : str
        文字起こし言語コード ("ja", "en", "zh", "ko", "yue", "auto")。
    startup_gate : threading.Event | None
        このイベントが set されるまで ONNX モデルの読み込みを遅延させる。
        llama-server の VRAM 確保完了を待つために使用。
    """

    model_key = "sensevoice-small"  # SettingsWindow との互換用

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        language: str = "ja",
        startup_gate: Optional[threading.Event] = None,
    ):
        self.language      = language
        self._startup_gate = startup_gate
        self._session: Optional[ort.InferenceSession] = None
        self._sp           = None        # SentencePieceProcessor
        self._cmvn_mean: Optional[np.ndarray] = None
        self._cmvn_std:  Optional[np.ndarray] = None
        self._embeddings: Optional[np.ndarray] = None
        self._ready        = threading.Event()

        _dir = Path(model_dir) if model_dir else _DEFAULT_SENSEVOICE_DIR
        # INT8量子化版を優先、なければFP32版を使用
        _int8 = _dir / "sense-voice-encoder-int8.onnx"
        _fp32 = _dir / "sense-voice-encoder.onnx"
        self._model_path = _int8 if _int8.exists() else _fp32
        self._sp_path    = _dir / "chn_jpn_yue_eng_ko_spectok.bpe.model"
        self._mvn_path   = _dir / "am.mvn"
        self._emb_path   = _dir / "embedding.npy"
        self._cfg_path   = _dir / "config.yaml"

        for path in [self._model_path, self._sp_path, self._mvn_path, self._emb_path]:
            if not path.exists():
                raise FileNotFoundError(
                    f"SenseVoice に必要なファイルが見つかりません: {path}\n"
                    f"sensevoice-onnx/ フォルダに以下の4ファイルを置いてください:\n"
                    f"  sense-voice-encoder.onnx\n"
                    f"  chn_jpn_yue_eng_ko_spectok.bpe.model\n"
                    f"  am.mvn\n"
                    f"  embedding.npy"
                )

        print(f"[SenseVoice] モデルフォルダ: {_dir}")
        print(f"[SenseVoice] 言語: {language}")
        print("[SenseVoice] バックグラウンドでモデルを読み込み中...")

        threading.Thread(
            target=self._load_model,
            daemon=True,
            name="SenseVoiceInit",
        ).start()

    # ── モデル読み込み（バックグラウンド）────────────────────────────────
    def _load_model(self):
        """DirectML セッション・トークナイザー・前処理データを初期化する。"""
        if self._startup_gate is not None:
            print("[SenseVoice] llama-server の VRAM 確保完了を待機中...")
            self._startup_gate.wait(timeout=90)
            print("[SenseVoice] llama-server 準備完了を確認。モデルを読み込みます。")

        try:
            # ── ONNX セッション ────────────────────────────────────────
            providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
            self._session = ort.InferenceSession(
                str(self._model_path),
                providers=providers,
            )
            active  = self._session.get_providers()
            backend = "DirectML (AMD GPU)" if "DmlExecutionProvider" in active else "CPU"
            print(f"[SenseVoice] 推論バックエンド: {backend}")

            # ── テンソル情報ログ（入力名・形状を確認するために出力）──────
            print("[SenseVoice] --- モデル入出力テンソル情報 ---")
            for inp in self._session.get_inputs():
                print(f"  入力: {inp.name!r}  shape={inp.shape}  type={inp.type}")
            for out in self._session.get_outputs():
                print(f"  出力: {out.name!r}  shape={out.shape}  type={out.type}")
            print("[SenseVoice] -----------------------------------")

            # ── SentencePiece トークナイザー ───────────────────────────
            import sentencepiece as spm
            self._sp = spm.SentencePieceProcessor()
            self._sp.Load(str(self._sp_path))
            print(f"[SenseVoice] トークナイザー語彙数: {self._sp.GetPieceSize()}")

            # ── CMVN 統計 (am.mvn) ────────────────────────────────────
            self._cmvn_mean, self._cmvn_std = self._load_mvn(self._mvn_path)
            print(f"[SenseVoice] CMVN: shift shape={self._cmvn_mean.shape}, scale shape={self._cmvn_std.shape}")

            # ── 言語埋め込み ──────────────────────────────────────────
            self._embeddings = np.load(str(self._emb_path))
            print(f"[SenseVoice] 言語埋め込み: shape={self._embeddings.shape}")

            # ── config.yaml があれば FBank パラメータを上書き ──────────
            if self._cfg_path.exists():
                self._load_fbank_config(self._cfg_path)

            self._ready.set()
            print("[SenseVoice] 準備完了（以降の推論は高速になります）")

        except Exception as e:
            print(f"[SenseVoice] モデル読み込み失敗: {e}")
            import traceback
            traceback.print_exc()
            self._session = None

    @staticmethod
    def _load_mvn(mvn_path: Path):
        """
        am.mvn (Kaldi <Nnet> 形式) から AddShift・Rescale の値を読み込む。

        実際の形式:
          <AddShift>  → [ shift_0 shift_1 ... shift_559 ]  560 値
          <Rescale>   → [ scale_0 scale_1 ... scale_559 ]  560 値

        - shift は features に足すシフト量 (= -mean)
        - scale は features に掛けるスケール量 (= 1/std)
        - 両方とも LFR 後の 560 次元特徴量に適用する

        適用式: output = (input + shift) * scale
        """
        import re
        text = mvn_path.read_text(encoding="utf-8")

        float_blocks = []
        for m in re.finditer(r'\[([^\]]+)\]', text):
            nums = m.group(1).strip().split()
            try:
                arr = np.array(nums, dtype=np.float32)
                if len(arr) > 1:   # "[ 0 ]" などの単一値ブロックは除外
                    float_blocks.append(arr)
            except ValueError:
                pass

        if len(float_blocks) < 2:
            raise ValueError(
                f"am.mvn から CMVN 統計を読み込めませんでした。"
                f"見つかったブロック数: {len(float_blocks)}"
            )

        shift = float_blocks[0]   # AddShift 値 (= -mean)
        scale = float_blocks[1]   # Rescale 値  (= 1/std)
        return shift, scale

    def _load_fbank_config(self, cfg_path: Path):
        """config.yaml が存在する場合、FBank パラメータを上書きする。"""
        try:
            import yaml
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            fe = cfg.get("frontend_conf", {})
            global _FBANK_NUM_MELS, _FBANK_FRAME_LENGTH, _FBANK_FRAME_SHIFT
            _FBANK_NUM_MELS     = int(fe.get("n_mels",        _FBANK_NUM_MELS))
            _FBANK_FRAME_LENGTH = float(fe.get("frame_length", _FBANK_FRAME_LENGTH * 1000)) / 1000
            _FBANK_FRAME_SHIFT  = float(fe.get("frame_shift",  _FBANK_FRAME_SHIFT  * 1000)) / 1000
            print(f"[SenseVoice] FBank config: mels={_FBANK_NUM_MELS}, "
                  f"frame_length={_FBANK_FRAME_LENGTH*1000:.0f}ms, "
                  f"frame_shift={_FBANK_FRAME_SHIFT*1000:.0f}ms")
        except Exception as e:
            print(f"[SenseVoice] config.yaml 読み込みスキップ: {e}")

    # ── 公開 API ─────────────────────────────────────────────────────────
    def transcribe(
        self,
        wav_path: Path,
        infer_timeout: Optional[int] = None,
        force_cli: bool = False,
    ) -> str:
        """
        WAV ファイルを文字起こしして結合テキストを返す。

        Parameters
        ----------
        wav_path : Path
            文字起こし対象の WAV ファイルパス
        infer_timeout, force_cli : 未使用（WhisperTranscriber との互換用）
        """
        if not wav_path.exists():
            raise FileNotFoundError(f"WAV ファイルが見つかりません: {wav_path}")

        if not self._ready.wait(timeout=90):
            raise RuntimeError("[SenseVoice] モデルの読み込みがタイムアウトしました。")
        if self._session is None:
            raise RuntimeError("[SenseVoice] モデルの読み込みに失敗しています。ログを確認してください。")

        print(f"[SenseVoice] 文字起こし開始: {wav_path.name}")
        self._check_audio_level(wav_path)

        t0       = time.perf_counter()
        waveform = self._load_wav(wav_path)
        feats    = self._extract_fbank(waveform)          # FBank + CMVN
        text     = self._infer(feats)
        print(f"[SenseVoice] 所要時間: {time.perf_counter() - t0:.2f}秒")
        print(f"[SenseVoice] 文字起こし結果:\n  → {text!r}")
        return text

    def shutdown(self):
        """互換用。SenseVoice はサーバープロセスを持たないため何もしない。"""
        pass

    # ── 内部メソッド: 音声読み込み ────────────────────────────────────────
    def _load_wav(self, wav_path: Path) -> np.ndarray:
        """WAV ファイルを 16kHz mono float32 の numpy 配列として読み込む。"""
        with wave.open(str(wav_path), "rb") as wf:
            n_channels  = wf.getnchannels()
            sample_rate = wf.getframerate()
            n_frames    = wf.getnframes()
            raw         = wf.readframes(n_frames)

        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

        if n_channels == 2:
            audio = audio.reshape(-1, 2).mean(axis=1)

        if sample_rate != _FBANK_SAMPLE_RATE:
            audio = self._resample(audio, sample_rate, _FBANK_SAMPLE_RATE)

        return audio

    @staticmethod
    def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        """numpy 線形補間によるリサンプル（AirType は 16kHz 固定録音のため通常は不要）。"""
        n_out = int(len(audio) * target_sr / orig_sr)
        x_orig = np.linspace(0, len(audio) - 1, len(audio))
        x_new  = np.linspace(0, len(audio) - 1, n_out)
        return np.interp(x_new, x_orig, audio).astype(np.float32)

    # ── 内部メソッド: 前処理 (FBank + CMVN + LFR) ───────────────────────
    def _extract_fbank(self, waveform: np.ndarray) -> np.ndarray:
        """
        16kHz 波形 → FBank (T, 80) → CMVN → LFR (T', 560) の順に処理する。

        FunASR デフォルト設定:
          - プリエンファシス: 0.97、窓関数: Hamming、窓長: 25ms、シフト: 10ms、メル次元: 80
          - LFR: 7 フレームを結合、ストライド 6 → 出力次元 80×7=560
            （モデル入力 shape=[1, T', 560] で確認済み）
        """
        sr          = _FBANK_SAMPLE_RATE
        frame_len   = int(round(_FBANK_FRAME_LENGTH * sr))   # 400
        frame_shift = int(round(_FBANK_FRAME_SHIFT  * sr))   # 160
        n_mels      = _FBANK_NUM_MELS
        n_fft       = 2 ** int(np.ceil(np.log2(frame_len)))  # 512

        # プリエンファシス
        emphasized = np.append(waveform[0], waveform[1:] - _FBANK_PREEMPH * waveform[:-1])

        # フレーミング + Hamming 窓
        n_frames = 1 + (len(emphasized) - frame_len) // frame_shift
        indices  = (
            np.tile(np.arange(frame_len), (n_frames, 1))
            + np.tile(np.arange(n_frames) * frame_shift, (frame_len, 1)).T
        )
        frames = emphasized[indices] * np.hamming(frame_len)

        # パワースペクトル → メルフィルタバンク → 対数
        mag_spec = np.abs(np.fft.rfft(frames, n=n_fft)) ** 2
        mel_fb   = self._mel_filterbank(sr, n_fft, n_mels)
        feats    = np.log(np.maximum(np.dot(mag_spec, mel_fb.T), 1e-10)).astype(np.float32)

        # LFR: 7 フレームを結合してストライド 6 でサブサンプリング → (T', 560)
        feats = self._apply_lfr(feats, lfr_m=7, lfr_n=6)

        # CMVN 正規化（LFR 後の 560 次元に適用）
        # _cmvn_mean = AddShift 値 (= -mean)、_cmvn_std = Rescale 値 (= 1/std)
        # 適用式: output = (input + shift) * scale
        if self._cmvn_mean is not None and self._cmvn_std is not None:
            feats = (feats + self._cmvn_mean) * self._cmvn_std

        return feats  # shape: (T', 560)

    @staticmethod
    def _apply_lfr(feats: np.ndarray, lfr_m: int = 7, lfr_n: int = 6) -> np.ndarray:
        """
        LFR (Low Frame Rate): lfr_m フレームを lfr_n ストライドで結合する。
        (T, 80) → (T', 560)   T' = ceil(T / lfr_n)
        先頭を左パディングして FunASR 実装と一致させる。
        """
        T, D     = feats.shape
        T_lfr    = int(np.ceil(T / lfr_n))
        left_pad = (lfr_m - 1) // 2
        padded   = np.concatenate([np.tile(feats[0:1], (left_pad, 1)), feats], axis=0)

        out = np.zeros((T_lfr, D * lfr_m), dtype=np.float32)
        for i in range(T_lfr):
            start = i * lfr_n
            end   = start + lfr_m
            if end <= padded.shape[0]:
                out[i] = padded[start:end].flatten()
            else:
                # 末尾が足りない場合は最終フレームで右パディング
                chunk = padded[start:]
                pad   = np.tile(padded[-1:], (lfr_m - len(chunk), 1))
                out[i] = np.concatenate([chunk, pad], axis=0).flatten()
        return out

    @staticmethod
    def _mel_filterbank(sr: int, n_fft: int, n_mels: int) -> np.ndarray:
        """メルフィルタバンク行列を生成する。shape: (n_mels, n_fft//2+1)"""
        low_freq_mel = 0
        high_freq_mel = 2595 * np.log10(1 + (sr / 2) / 700)
        mel_points    = np.linspace(low_freq_mel, high_freq_mel, n_mels + 2)
        hz_points     = 700 * (10 ** (mel_points / 2595) - 1)
        bin_points    = np.floor((n_fft + 1) * hz_points / sr).astype(int)

        fbank = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
        for m in range(1, n_mels + 1):
            lo, center, hi = bin_points[m - 1], bin_points[m], bin_points[m + 1]
            for k in range(lo, center):
                fbank[m - 1, k] = (k - lo) / (center - lo + 1e-10)
            for k in range(center, hi):
                fbank[m - 1, k] = (hi - k) / (hi - center + 1e-10)
        return fbank

    # ── 内部メソッド: 推論 ────────────────────────────────────────────────
    def _infer(self, feats: np.ndarray) -> str:
        """
        FBank 特徴量と言語埋め込みを ONNX エンコーダーに入力してテキストを返す。

        ログで確認済みのテンソル仕様:
          入力 1: 'speech'         shape=[1, T', 560]  float32  ← FBank+CMVN+LFR済み
          入力 2: 'speech_lengths' shape=[1]            int64
          出力  : 'encoder_out'    shape=[1, T', 25055] float32  ← CTC ロジット
        """
        speech_batch = feats[np.newaxis, :, :]                        # (1, T', 560)
        speech_len   = np.array([feats.shape[0]], dtype=np.int64)     # (1,)

        inputs = {
            "speech":         speech_batch,
            "speech_lengths": speech_len,
        }

        outputs = self._session.run(None, inputs)
        logits  = outputs[0]  # shape: (1, T', 25055)

        return self._decode(logits)

    def _decode(self, logits: np.ndarray) -> str:
        """CTC greedy decode → SentencePiece デコード → テキスト。"""
        if logits.ndim == 3:
            logits = logits[0]   # (T', V)

        # greedy: 各フレームで最大確率のトークン ID を選択
        token_ids = logits.argmax(axis=-1).tolist()

        # CTC デコード: ブランク除去 + 連続重複を1つに畳む
        # TODO: SentencePiece モデルの blank_id を確認して修正する（通常は 0）
        blank_id = 0
        prev     = None
        kept     = []
        for tid in token_ids:
            if tid == blank_id or tid == prev:
                prev = tid
                continue
            kept.append(tid)
            prev = tid

        if not kept:
            return ""

        text = self._sp.Decode(kept)
        # SenseVoice が付与する特殊トークンを除去（言語・感情・イベント・ITN タグ）
        text = re.sub(r'<\|[^|]+\|>', '', text)
        return text.strip()

    # ── 内部メソッド: 音量チェック ───────────────────────────────────────
    @staticmethod
    def _check_audio_level(wav_path: Path) -> None:
        """WAV ファイルの音量を確認してデバッグ情報を出力する。"""
        try:
            data    = wav_path.read_bytes()
            samples = struct.unpack_from(f"<{(len(data) - 44) // 2}h", data, 44)
            if samples:
                peak = max(abs(s) for s in samples)
                rms  = (sum(s * s for s in samples) / len(samples)) ** 0.5
                print(f"[SenseVoice] 音量チェック: peak={peak}, rms={rms:.1f} (無音判定: peak<100)")
                if peak < 100:
                    print("[SenseVoice] WARNING: 録音がほぼ無音です。マイク設定を確認してください。")
        except Exception:
            pass


# ─────────────────────────────────────
# 単体テスト用エントリポイント
# ─────────────────────────────────────
def _test_with_existing_file(wav_path: str):
    transcriber = SenseVoiceTranscriber()
    transcriber._ready.wait(timeout=90)
    text = transcriber.transcribe(Path(wav_path))
    print(f"\n結果: {text}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        _test_with_existing_file(sys.argv[1])
    else:
        print("使い方: python step2_sensevoice.py <wav_file>")
