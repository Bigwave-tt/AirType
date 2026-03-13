"""
AirType - APIサーバー (ホストPC用)

ネットワーク上のクライアントPCから音声データを受け取り、
STT（Whisper）+ テキスト整形（LLM）を実行して結果を返す。

起動方法:
  python api_server.py

エンドポイント:
  POST /dictate
    - multipart/form-data で WAV ファイルを受け取る
    - {"text": "整形済みテキスト", "raw": "認識生テキスト"} を返す

  GET /health
    - サーバー稼働確認用

起動シーケンス:
  1. LlamaRefiner を起動（llama-server.exe の VRAM 確保を優先）
  2. llama-server 準備完了後に WhisperTranscriber を起動
  3. 両者の準備完了後に FastAPI (uvicorn) を起動

ポート: 8000 (変更は HOST / PORT 定数で)
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse

from step2_transcriber import WhisperTranscriber
from step3_refiner import LlamaRefiner


# ─────────────────────────────────────
# 設定
# ─────────────────────────────────────
HOST = "0.0.0.0"   # 全ネットワークインターフェースでリッスン
PORT = 8000


# ─────────────────────────────────────
# グローバルコンポーネント
# ─────────────────────────────────────
_transcriber: WhisperTranscriber | None = None
_refiner: LlamaRefiner | None = None
_ready = False  # 両サーバーの準備完了フラグ


# ─────────────────────────────────────
# FastAPI アプリ
# ─────────────────────────────────────
app = FastAPI(title="AirType API Server", version="1.0.0")


@app.get("/health")
def health():
    """サーバー稼働・準備状態を返す"""
    return {"status": "ok" if _ready else "starting"}


@app.post("/dictate")
async def dictate(file: UploadFile = File(...)):
    """
    WAV ファイルを受け取り、STT + LLM 整形を実行してテキストを返す。

    Parameters
    ----------
    file : UploadFile
        multipart/form-data で送信された WAV ファイル

    Returns
    -------
    JSON
        {"text": "整形済みテキスト", "raw": "認識生テキスト"}
    """
    if not _ready:
        raise HTTPException(status_code=503, detail="サーバー起動中です。しばらく待ってから再送してください。")

    # WAV を一時ファイルに保存
    tmp_path: Path | None = None
    try:
        audio_bytes = await file.read()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="音声データが空です")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = Path(tmp.name)

        print(f"[API] /dictate 受信: {tmp_path.name}  ({len(audio_bytes) / 1024:.1f} KB)")

        # STT
        raw_text = _transcriber.transcribe(tmp_path)
        if not raw_text.strip():
            print("[API] 音声が認識されませんでした")
            return JSONResponse({"text": "", "raw": ""})

        # LLM 整形
        refined_text = _refiner.refine(raw_text)
        if not refined_text.strip():
            refined_text = raw_text

        # 英語変換チェック（main.py と同様）
        if _is_ascii_dominant(refined_text) and not _is_ascii_dominant(raw_text):
            print("[API] LLM が英語に変換しました。生テキストを使用します")
            refined_text = raw_text

        print(f"[API] 完了: {refined_text!r}")
        return JSONResponse({"text": refined_text, "raw": raw_text})

    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] パイプラインエラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


def _is_ascii_dominant(text: str) -> bool:
    if not text:
        return False
    ascii_count = sum(1 for c in text if ord(c) < 128 and c.isalpha())
    total_alpha = sum(1 for c in text if c.isalpha())
    return total_alpha > 0 and (ascii_count / total_alpha) > 0.8


# ─────────────────────────────────────
# 起動シーケンス
# ─────────────────────────────────────
def _init_pipeline():
    """
    LlamaRefiner → WhisperTranscriber の順に起動する。
    llama-server の VRAM 確保を優先させることで VRAM 競合を回避。
    """
    global _transcriber, _refiner, _ready

    print("=" * 50)
    print("  AirType API Server を起動しています...")
    print("=" * 50)

    # Step 1: LlamaRefiner（llama-server が先に VRAM を確保）
    _refiner = LlamaRefiner()
    _whisper_gate = _refiner._server_ready if _refiner._use_server else None

    # Step 2: WhisperTranscriber（llama-server 準備完了後に起動）
    _transcriber = WhisperTranscriber(startup_gate=_whisper_gate)

    # サーバーモードの場合、whisper-server の準備を待つ
    if _transcriber._use_server:
        print("[API] whisper-server の準備完了を待機中...")
        if not _transcriber._server_ready.wait(timeout=90):
            print("[API] WARNING: whisper-server の起動待機タイムアウト。CLIモードで続行します。")

    _ready = True
    print("\n" + "=" * 50)
    print(f"  AirType API Server 起動完了")
    print(f"  エンドポイント: http://{HOST}:{PORT}/dictate")
    print("=" * 50 + "\n")


def main():
    # パイプラインを初期化（uvicorn 起動前に完了させる）
    _init_pipeline()

    # uvicorn でサーブ開始
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
