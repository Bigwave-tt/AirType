"""
AirType - APIサーバー (ホストPC用)

ネットワーク上のクライアントPCから音声データを受け取り、
STT（Whisper）+ テキスト整形（LLM）を実行して結果を返す。

起動方法:
  python api_server.py

設定:
  airtype_config.json の network / whisper / llama セクションを参照。

エンドポイント:
  POST /dictate
    - multipart/form-data で WAV ファイルを受け取る
    - {"text": "整形済みテキスト", "raw": "認識生テキスト"} を返す
    - APIキー設定時は X-API-Key ヘッダーが必要

  GET /health
    - サーバー稼働確認用

起動シーケンス:
  1. LlamaRefiner を起動（llama-server.exe の VRAM 確保を優先）
  2. llama-server 準備完了後に WhisperTranscriber を起動
  3. 両者の準備完了後に FastAPI (uvicorn) を起動
"""

import atexit
import tempfile
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Security, UploadFile
from fastapi.security.api_key import APIKeyHeader

import config as _config
from step2_transcriber import WhisperTranscriber
from step3_refiner import LlamaRefiner


# ─────────────────────────────────────
# 設定読み込み
# ─────────────────────────────────────
_cfg          = _config.load()
_net          = _cfg["network"]
_whisper_cfg  = _cfg["whisper"]
_llama_cfg    = _cfg["llama"]

HOST         = _net["host"]
PORT         = int(_net["port"])
_cfg_api_key = _net["api_key"]   # 空文字 = 認証なし

# バイナリパス解決
_whisper_dir = _config.resolve_dir(_whisper_cfg["dir"], "whisper.cpp-windows-vulkan")
_llama_dir   = _config.resolve_dir(_llama_cfg["dir"],   "llama.cpp-windows-vulkan")

# ポート解決
_whisper_port = _config.resolve_port(int(_whisper_cfg["server_port"]))
_llama_port   = _config.resolve_port(int(_llama_cfg["server_port"]))


# ─────────────────────────────────────
# グローバルコンポーネント
# ─────────────────────────────────────
_transcriber: WhisperTranscriber | None = None
_refiner: LlamaRefiner | None           = None
_ready = False

# 一時ファイルの残骸追跡（プロセス異常終了時に atexit でクリーンアップ）
_active_tmp_files: set[Path] = set()


@atexit.register
def _cleanup_tmp_files():
    for p in list(_active_tmp_files):
        try:
            if p.exists():
                p.unlink()
                print(f"[API] atexit クリーンアップ: {p.name}")
        except Exception:
            pass


# ─────────────────────────────────────
# APIキー認証
# ─────────────────────────────────────
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _verify_api_key(key: str = Security(_api_key_header)):
    """APIキーが設定されている場合にリクエストを検証する。"""
    if _cfg_api_key and key != _cfg_api_key:
        raise HTTPException(status_code=403, detail="無効なAPIキーです")


# ─────────────────────────────────────
# FastAPI アプリ
# ─────────────────────────────────────
app = FastAPI(title="AirType API Server", version="1.1.0")


@app.get("/health")
def health():
    """サーバー稼働・準備状態を返す。"""
    return {"status": "ok" if _ready else "starting"}


@app.post("/dictate", dependencies=[Depends(_verify_api_key)])
async def dictate(file: UploadFile = File(...)):
    """
    WAV ファイルを受け取り、STT + LLM 整形を実行してテキストを返す。

    Parameters
    ----------
    file : UploadFile
        multipart/form-data で送信された WAV ファイル

    Headers
    -------
    X-API-Key : str (optional)
        airtype_config.json の network.api_key が空でない場合に必須

    Returns
    -------
    JSON
        {"text": "整形済みテキスト", "raw": "認識生テキスト"}
    """
    if not _ready:
        raise HTTPException(
            status_code=503,
            detail="サーバー起動中です。しばらく待ってから再送してください。"
        )

    tmp_path: Path | None = None
    try:
        audio_bytes = await file.read()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="音声データが空です")

        # 一時ファイルに保存（atexit 追跡に登録）
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = Path(tmp.name)
        _active_tmp_files.add(tmp_path)

        print(f"[API] /dictate 受信: {tmp_path.name}  ({len(audio_bytes) / 1024:.1f} KB)")

        # STT
        raw_text = _transcriber.transcribe(tmp_path)
        if not raw_text.strip():
            print("[API] 音声が認識されませんでした")
            return {"text": "", "raw": ""}

        # LLM 整形
        refined_text = _refiner.refine(raw_text)
        if not refined_text.strip():
            refined_text = raw_text

        # 英語変換チェック（main.py と同様）
        if _is_ascii_dominant(refined_text) and not _is_ascii_dominant(raw_text):
            print("[API] LLM が英語に変換しました。生テキストを使用します")
            refined_text = raw_text

        print(f"[API] 完了: {refined_text!r}")
        return {"text": refined_text, "raw": raw_text}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] パイプラインエラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # 通常終了時は即座にクリーンアップ
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()
            _active_tmp_files.discard(tmp_path)


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
    print(f"  llama dir  : {_llama_dir}")
    print(f"  whisper dir: {_whisper_dir}")
    print(f"  llama port : {_llama_port}")
    print(f"  whisper port: {_whisper_port}")
    print(f"  API key    : {'設定あり' if _cfg_api_key else '設定なし（認証なし）'}")
    print("=" * 50)

    # Step 1: LlamaRefiner（llama-server が先に VRAM を確保）
    _refiner = LlamaRefiner(
        llama_dir=_llama_dir,
        server_port=_llama_port,
    )
    _whisper_gate = _refiner._server_ready if _refiner._use_server else None

    # Step 2: WhisperTranscriber（llama-server 準備完了後に起動）
    _transcriber = WhisperTranscriber(
        startup_gate=_whisper_gate,
        whisper_dir=_whisper_dir,
        server_port=_whisper_port,
    )

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
    _init_pipeline()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
