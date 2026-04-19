# SenseVoiceSmall 統合実装計画

> 作成: 2026-04-18  
> ブランチ: `experiment/sensevoice-small`  
> 目的: STT を kotoba-whisper から SenseVoiceSmall (ONNX + DirectML) に置き換え、処理時間の短縮を検証する

---

## 背景・動機

実測データ（2026-04-18 計測）:

| 録音時間 | STT 時間 | LLM 時間 | STT 比率 |
|---------|---------|---------|---------|
| 5.1 秒 | 5.68 秒 | 2.85 秒 | 67% |
| 6.1 秒 | 5.65 秒 | 2.76 秒 | 67% |
| 16.8 秒 | 5.81 秒 | 3.41 秒 | 63% |

- STT が全体の 65% を占め、ボトルネックであることが確認された
- STT 時間が音声長に依存しない（Whisper が常に 30 秒窓を処理するため）
- SenseVoiceSmall は非自己回帰（NAR）アーキテクチャで、デコードが O(1) steps

**目標**: STT を 5.7 秒 → 2 秒台に短縮し、総処理時間を 8.5 秒 → 5 秒台にする

---

## 技術選定

### モデル

| 選択肢 | 入手先 | 形式 | 採用理由 |
|--------|--------|------|---------|
| **SenseVoiceSmall ONNX** | `lovemefan/SenseVoice-onnx` (HuggingFace) | ONNX FP16 | DirectML で AMD GPU を使える唯一の現実的な選択肢 |
| FunASR (PyTorch) | `FunAudioLLM/SenseVoiceSmall` | PyTorch | Windows で ROCm が不要だが CPU のみ → 遅い |

**採用**: ONNX + DirectML

> 注意: `lovemefan/SenseVoice-onnx` はコミュニティ製の非公式変換品。
> DirectML で全オペレーターが GPU に乗らない可能性がある（一部 CPU フォールバック）。

### 推論バックエンド

`onnxruntime-directml` パッケージ。既に `.venv` にインストール済み（`onnxruntime` が確認できる）。

---

## 実装方針

### 基本方針

- `WhisperTranscriber` クラスは**一切変更しない**（既存機能を壊さない）
- 新クラス `SenseVoiceTranscriber` を **`step2_transcriber.py` と同じインターフェース**で実装
  - `transcribe(wav_path: Path) -> str` を公開 API とする
  - `shutdown()` メソッドも実装（現状は no-op でよい）
- `airtype_config.json` にスイッチを追加してどちらを使うか切り替える
- 実験が失敗しても `config.json` を1行変えれば元に戻る

---

## 実装ステップ

### Step 1: 事前調査・モデルダウンロード

```powershell
# モデルの入手
# HuggingFace から ONNX モデルをダウンロード
# lovemefan/SenseVoice-onnx の model.onnx (FP16) を取得
# 保存先: AirType の親フォルダ / sensevoice-onnx/model.onnx
```

ダウンロードするファイル（`.onnx` 単体では動作しない）:

```
sensevoice-onnx/
├── model.onnx            ← 推論グラフ本体
├── tokens.json           ← トークン ID → 文字 の対応辞書（必須）
├── vocab.txt             ← 存在する場合はこちらも取得
└── config.yaml           ← 前処理パラメータが記載されている場合あり
```

> `lovemefan/SenseVoice-onnx` の `example/` ディレクトリや `README` を確認し、
> 辞書ファイル一式を必ずセットでダウンロードする。

ダウンロードしたモデルの入力・出力テンソル名と型を確認する:

```python
import onnxruntime as ort
sess = ort.InferenceSession("model.onnx")
for inp in sess.get_inputs():
    print(inp.name, inp.shape, inp.type)
for out in sess.get_outputs():
    print(out.name, out.shape, out.type)
```

**確認すべき重要な点:**
- 入力が raw waveform か FBank 特徴量か（前処理戦略が変わる）
- 出力がトークン ID か直接テキストか
- ONNX グラフ内に前処理（FBank/CMVN/LFR）が含まれているか

この情報が判明してから Step 2 以降の実装に入る。

### Step 2: 音声前処理の方針決定

SenseVoice（FunASR 系）の前処理には標準的な FBank に加え、以下が含まれる:

- **CMVN（ケプストラム平均分散正規化）**: 統計ファイルとの照合が必要
- **LFR（Low Frame Rate）**: 複数フレームを結合するサブサンプリング処理

これらを numpy/scipy で FunASR 実装と完全一致させることは非常に困難（わずかなズレで文字化けが発生する）。以下の優先順位で方針を選ぶ:

| 優先度 | 条件 | 方針 |
|--------|------|------|
| 1 | ONNX グラフ内に前処理が含まれる | 前処理不要。raw waveform を直接入力するだけ |
| 2 | ONNX グラフに前処理がない | **ハイブリッド構成**（下記）を採用 |

**ハイブリッド構成（前処理のみ FunASR を使用）:**

```
WAV → [FunASR の frontend モジュール（CPU）] → FBank/CMVN/LFR 特徴量
                                                  ↓
                              [ONNX Runtime + DirectML（AMD GPU）] → トークン ID
                                                  ↓
                              [vocab デコード] → テキスト
```

`pip install funasr` は PyTorch を含む重い依存だが、**推論は行わず前処理だけ**を担う。
GPU は ONNX Runtime（DirectML）だけが使うため AMD RX6600 でも GPU 高速化が効く。

### Step 3: `step2_sensevoice.py` の実装

```python
# 新規ファイル: AirType/step2_sensevoice.py

class SenseVoiceTranscriber:
    def __init__(
        self,
        model_dir: Path | None = None,   # sensevoice-onnx/ フォルダ
        language: str = "ja",
    ):
        # InferenceSession は __init__ で一度だけ作成し保持する
        # （DirectML の初期化に数秒かかるため、transcribe() 内で毎回作ると遅くなる）
        # providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
        self._session = ort.InferenceSession(
            str(model_dir / "model.onnx"),
            providers=["DmlExecutionProvider", "CPUExecutionProvider"],
        )
        # トークン辞書もここで読み込む
        self._vocab = self._load_vocab(model_dir / "tokens.json")
        ...

    def transcribe(self, wav_path: Path, **kwargs) -> str:
        # 1. WAV 読み込み（wave モジュール）
        # 2. 前処理（Step 2 の方針に従う）
        # 3. ONNX 推論（self._session.run()）
        # 4. CTC greedy decode + vocab でテキスト化
        # 5. 後処理（ノイズトークン除去）
        ...

    def shutdown(self):
        pass  # サーバープロセスなし
```

**トークンデコード**:
出力テンソルがトークン ID の場合、`tokens.json` を辞書として CTC greedy decode を行う。
出力が直接テキストの場合（ONNX 変換実装依存）はデコード不要。Step 1 の確認結果次第。

### Step 4: `airtype_config.json` にスイッチ追加

```json
{
  "transcriber": {
    "backend": "sensevoice",           // "whisper" | "sensevoice"
    "sensevoice_dir": "",              // 空文字 = 親フォルダ/sensevoice-onnx
    "language": "ja"
  }
}
```

### Step 5: `main.py` での切り替え実装

```python
# config の transcriber.backend に応じてインスタンスを切り替える
if cfg.get("transcriber", {}).get("backend") == "sensevoice":
    from step2_sensevoice import SenseVoiceTranscriber
    transcriber = SenseVoiceTranscriber(...)
else:
    transcriber = WhisperTranscriber(...)
```

`api_server.py` も同様に対応する。

### Step 6: ベンチマーク測定

`step2_transcriber.py` と `step2_sensevoice.py` に既存のタイムスタンプ計測コードが入っているので、同一 WAV ファイルで両者を比較する:

```bash
python AirType/step2_transcriber.py test.wav kotoba-q5
python -c "
from AirType.step2_sensevoice import SenseVoiceTranscriber
from pathlib import Path
t = SenseVoiceTranscriber()
print(t.transcribe(Path('test.wav')))
"
```

---

## ファイル変更一覧

| ファイル | 変更種別 | 内容 |
|---------|---------|------|
| `AirType/step2_sensevoice.py` | **新規作成** | SenseVoiceTranscriber クラス |
| `AirType/main.py` | 修正 | backend スイッチで初期化を切り替え |
| `AirType/api_server.py` | 修正 | 同上 |
| `airtype_config.json` | 修正 | `transcriber.backend` フィールド追加 |
| `SPEC.md` | 修正 | SenseVoice バックエンドの記述追加 |

---

## ブランチ戦略

```
main
 └── feat/video-transcribe    ← 現在のブランチ（タイムスタンプ計測が含まれる）
      └── experiment/sensevoice-small   ← このブランチ（ここで作業する）
```

`feat/video-transcribe` からブランチを切る理由:
- タイムスタンプ計測コード（`所要時間` ログ）が実験に必要なため

実験成功時のマージ先: `main`（`feat/video-transcribe` は別途マージ）

---

## リスクと対策

| リスク | 可能性 | 対策 |
|--------|--------|------|
| DirectML で一部オペレーターが CPU フォールバック | 中 | CPU フォールバック時の速度を確認。壊滅的に遅ければ諦める |
| コミュニティ製 ONNX の品質問題（精度劣化） | 低〜中 | 同一音声で kotoba-whisper と精度比較。閾値以下なら採用しない |
| FBank/CMVN/LFR 前処理のズレによる文字化け | 高 | ONNX グラフに前処理が含まれない場合、FunASR frontend をハイブリッド利用する（自前実装しない） |
| モデルの入出力仕様が不明 | 高（事前調査必要） | Step 1 で必ず確認してから実装に進む |

---

## 判断基準

| 指標 | 合格ライン | 備考 |
|------|-----------|------|
| STT 処理時間 | < 3.0 秒（現状 5.7 秒の半分以下） | 2倍速達成の最低ライン |
| 日本語精度（文字誤り率） | kotoba-q5 と同等以上 | 主観評価 + 誤認識数の比較 |
| VRAM 使用量 | llama-server と合計 8GB 以内 | オーバー時は llama-server を軽量モデルに変更 |

いずれかが基準を満たさない場合は `experiment/sensevoice-small` ブランチを保留し、`main` への統合はしない。
