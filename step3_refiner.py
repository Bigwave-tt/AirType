"""
AirType - Step 3: テキスト整形

LlamaRefiner   : llama.cpp (Vulkan GPU) で LLM を使った整形 [優先]
                 対応モデル: Qwen 3.5 (2B/4B) / Gemma 4 (E2B/E4B)
RuleBasedRefiner: 正規表現によるフィラー除去 [フォールバック / 単体利用も可]

フォールバック戦略:
  llama-server.exe / llama-cli.exe / モデルファイルが存在しない場合、またはタイムアウト・
  エラーが発生した場合は、自動的に RuleBasedRefiner の結果を返す。
  パイプライン全体を止めることはない。

モード選択（自動）:
  1. llama-server.exe が存在する場合 → サーバーモード（推奨）
     - アプリ起動時にサーバーをバックグラウンドで一度だけ起動
     - 2回目以降の推論は ~1〜2 秒で完了
  2. llama-cli.exe のみの場合 → CLI サブプロセスモード
     - 毎回モデルをロードするため 15〜20 秒かかる（タイムアウト緩め）

フォルダ構成:
  AirType/
    AirType/              ← このファイルがある場所
    llama.cpp-windows-vulkan/
      llama-server.exe          ← 推奨 (サーバーモード用)
      llama-cli.exe             ← フォールバック (CLIモード用)
      Qwen3.5-2B-Q5_K_M.gguf             ← Qwen 3.5 推奨 (速度重視・約2GB)
      qwen3.5-4b-instruct-q5_k_m.gguf   ← Qwen 3.5 高精度 (約4GB)
      google_gemma-4-E2B-it-Q8_0.gguf    ← Gemma 4 推奨 (速度重視・約2GB)
      google_gemma-4-E4B-it-Q8_0.gguf    ← Gemma 4 高精度 (約4GB)
"""

import difflib
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
_HERE             = Path(__file__).parent
_DEFAULT_LLAMA_DIR = _HERE.parent / "llama.cpp-windows-vulkan"

# モデルのファイル名（llama dir からの相対）
_MODEL_FILES_REFINER = {
    "qwen3.5-2b": "Qwen3.5-2B-Q5_K_M.gguf",
    "qwen3.5-4b": "qwen3.5-4b-instruct-q5_k_m.gguf",
    "gemma4-e2b": "google_gemma-4-E2B-it-Q8_0.gguf",
    "gemma4-e4b": "google_gemma-4-E4B-it-Q8_0.gguf",
}

# 後方互換性のためモジュールレベルでも公開（デフォルトパスを使用）
LLAMA_DIR    = _DEFAULT_LLAMA_DIR
LLAMA_CLI    = LLAMA_DIR / "llama-cli.exe"
LLAMA_SERVER = LLAMA_DIR / "llama-server.exe"
REFINER_MODELS = {k: LLAMA_DIR / v for k, v in _MODEL_FILES_REFINER.items()}

DEFAULT_REFINER_MODEL = "gemma4-e4b"

# サーバーモード設定
_DEFAULT_SERVER_PORT = 18765   # デフォルトポート (config で上書き可)
_STARTUP_TIMEOUT     = 90      # サーバー起動・モデルロード完了までの待機上限 (秒)
_INFER_TIMEOUT       = 15      # サーバーモードの推論タイムアウト (秒)

# CLIモードのタイムアウト（モデルロード込みのため余裕を持たせる）
MODEL_TIMEOUTS = {
    "qwen3.5-2b": 45,
    "qwen3.5-4b": 60,
    "gemma4-e2b": 45,
    "gemma4-e4b": 60,
}

# 非思考モード誘導サンプリングパラメータ (Qwen 3 系共通)
_TEMP          = "0.1"    # より決定論的に（文変更リスクを下げる）
_TOP_P         = "0.8"
_TOP_K         = "20"
_REPEAT_PENALTY = "1.15"  # 繰り返しループ防止（1.0=無効, 1.1〜1.3 が一般的）
_REPEAT_LAST_N  = "64"    # 繰り返しチェック対象のトークン数
_N_GPU         = "99"     # Vulkan GPU へのオフロード層数
_N_CTX         = "512"    # コンテキスト長（リファイナー用途は ~430トークン以内で十分）
_N_PRED        = "256"    # 最大生成トークン数

_SYSTEM_PROMPT = (
    "あなたは音声認識テキストの最小限の校正アシスタントです。"
    "以下のルールを厳守してください。\n"
    "【許可】口語フィラー（『えーっと』『えー』『あのー』『まあ』『こう』『ね』など）の削除。\n"
    "【許可】文末や読点が欠けている箇所への句読点（、。）の追加。\n"
    "【禁止】単語の変更・言い換え・削除・追加。\n"
    "【禁止】文の並び替え・要約・結合・分割。\n"
    "【禁止】文体の変更（『遅れてます』→『遅れています』のような語尾の正規化を含む）。\n"
    "校正後のテキストのみを出力してください。説明や挨拶は不要です。"
)

_FEW_SHOT_INPUT = (
    "プロジェクトの進捗についてなんですがまあ今週中には設計が完了する予定です"
    "来週からは実装フェーズに入りたいと思っていますこうリソースが少し足りないと感じています"
)
_FEW_SHOT_OUTPUT = (
    "プロジェクトの進捗についてなんですが、今週中には設計が完了する予定です。"
    "来週からは実装フェーズに入りたいと思っています。リソースが少し足りないと感じています。"
)


def _build_chatml(raw_text: str) -> str:
    """ChatML 形式のプロンプト文字列を生成する（Qwen 3.5 用・Few-shot 1件入り）。

    アシスタントターンを <think>\\n\\n</think>\\n で始めることで
    Qwen3 の思考モードをスキップさせる（空の think ブロックプレフィル）。
    """
    return (
        f"<|im_start|>system\n{_SYSTEM_PROMPT}<|im_end|>\n"
        f"<|im_start|>user\n{_FEW_SHOT_INPUT}<|im_end|>\n"
        f"<|im_start|>assistant\n<think>\n\n</think>\n{_FEW_SHOT_OUTPUT}<|im_end|>\n"
        f"<|im_start|>user\n{raw_text}<|im_end|>\n"
        f"<|im_start|>assistant\n<think>\n\n</think>\n"
    )


def _build_gemma4(raw_text: str) -> str:
    """Gemma 4 のプロンプト形式を生成する（Few-shot 1件入り）。

    <|think|> トークンをシステムプロンプトに含めないことで思考モードを無効化。
    """
    return (
        f"<|turn>system\n{_SYSTEM_PROMPT}<turn|>\n"
        f"<|turn>user\n{_FEW_SHOT_INPUT}<turn|>\n"
        f"<|turn>model\n{_FEW_SHOT_OUTPUT}<turn|>\n"
        f"<|turn>user\n{raw_text}<turn|>\n"
        f"<|turn>model\n"
    )


def _build_prompt(model_key: str, raw_text: str) -> str:
    """モデルキーに応じたプロンプト形式を返す。"""
    if model_key.startswith("gemma4"):
        return _build_gemma4(raw_text)
    return _build_chatml(raw_text)


def _is_faithful(raw: str, refined: str, threshold: float = 0.75) -> bool:
    """
    LLM の出力が入力テキストに対して忠実かどうかを判定する。

    句読点・空白を除いたテキストで SequenceMatcher の類似度を計算し、
    threshold を下回ったら「過剰編集」と判断して False を返す。

    フィラー削除や句読点追加程度の変更は許容する（ratio ≈ 0.90〜1.0）。
    単語の置換・意味変更は拒否する（ratio < 0.85 が多い）。
    """
    def _strip(text: str) -> str:
        return re.sub(r"[、。・\s　]", "", text)

    raw_s     = _strip(raw)
    refined_s = _strip(refined)

    if not raw_s:
        return True  # 元テキストが空なら判定不要

    ratio = difflib.SequenceMatcher(None, raw_s, refined_s).ratio()
    if ratio < threshold:
        print(f"[Refiner] 忠実度チェック: ratio={ratio:.2f} < {threshold} → 過剰編集と判定")
    return ratio >= threshold


def _parse_llm_output(output: str, model_key: str = "") -> str:
    """
    LLM の出力からアシスタントの返答だけを抽出する。

    model_key に応じてフォーマットを判別する。
    - Qwen (ChatML): <|im_start|>assistant\n 以降 / <|im_end|> で切り捨て / <think> 除去
    - Gemma 4:       <|turn>model\n 以降 / <turn|> で切り捨て
    """
    if model_key.startswith("gemma4"):
        marker = "<|turn>model\n"
        if marker in output:
            output = output.rsplit(marker, 1)[1]
        output = output.split("<turn|>")[0]
    else:
        # Qwen / ChatML 形式
        marker = "<|im_start|>assistant\n"
        if marker in output:
            output = output.rsplit(marker, 1)[1]
        output = output.split("<|im_end|>")[0]
        # 閉じタグあり・なし両方の <think> ブロックを除去
        output = re.sub(r"<think>.*?</think>", "", output, flags=re.DOTALL)
        if "<think>" in output:
            output = output.split("<think>")[0]

    return output.strip()


# ─────────────────────────────────────
# LlamaRefiner クラス (LLM 整形・優先)
# ─────────────────────────────────────
class LlamaRefiner:
    """
    llama.cpp (Vulkan GPU) で LLM を呼び出してテキストを整形する。

    対応モデル: Qwen 3.5 (qwen3.5-2b / qwen3.5-4b)
                Gemma 4   (gemma4-e2b / gemma4-e4b)

    llama-server.exe が存在すればサーバーモード（高速）、
    llama-cli.exe のみならば CLI サブプロセスモード（互換）で動作する。
    いずれも利用不可の場合は RuleBasedRefiner で代替する。

    Parameters
    ----------
    model : str
        使用するモデルキー。REFINER_MODELS のいずれか。
    """

    def __init__(
        self,
        model: str = DEFAULT_REFINER_MODEL,
        llama_dir=None,
        server_port=None,
    ):
        """
        Parameters
        ----------
        llama_dir : Path | str | None
            llama.cpp フォルダのパス。None の場合はデフォルト相対パスを使用。
        server_port : int | None
            llama-server が使用するポート。None の場合はデフォルト値を使用。0 で空きポート自動割り当て。
        """
        self._fallback     = RuleBasedRefiner()
        self._available    = False
        self._server_proc  = None
        self._server_ready = threading.Event()
        self._use_server   = False
        self.model_key     = model

        # ── パス解決 ──────────────────────────────────────────────────
        from pathlib import Path as _Path
        _dir = _Path(llama_dir) if llama_dir else _DEFAULT_LLAMA_DIR
        self._llama_server = _dir / "llama-server.exe"
        self._llama_cli    = _dir / "llama-cli.exe"

        # ── ポート解決 ────────────────────────────────────────────────
        _port_cfg = server_port if server_port is not None else _DEFAULT_SERVER_PORT
        if _port_cfg == 0:
            import socket as _sock
            with _sock.socket() as s:
                s.bind(("", 0))
                _port_cfg = s.getsockname()[1]
            print(f"[Refiner] 空きポートを自動割り当て: {_port_cfg}")
        self._server_port = _port_cfg

        if model not in _MODEL_FILES_REFINER:
            model_path = None
        else:
            model_path = _dir / _MODEL_FILES_REFINER[model]

        if model_path is None:
            print(f"[Refiner] 不明なモデルキー: {model!r}")
            print("[Refiner] → ルールベース整形で動作します")
            return

        if not model_path.exists():
            print(f"[Refiner] モデルファイルが見つかりません: {model_path.name}")
            print("[Refiner] → ルールベース整形で動作します")
            return

        self._model_path = model_path

        # ── サーバーモード優先 ────────────────────────────────────────
        if self._llama_server.exists():
            self._use_server = True
            self._available  = True
            print(f"[Refiner] LLM整形: {model_path.name} (サーバーモード, ポート {self._server_port})")
            print(f"[Refiner] バックグラウンドでサーバーを起動中...")
            threading.Thread(
                target=self._start_server,
                daemon=True,
                name="LlamaServer",
            ).start()
            return

        # ── CLI サブプロセスモード（フォールバック）──────────────────
        if not self._llama_cli.exists():
            print(f"[Refiner] llama-server.exe も llama-cli.exe も見つかりません")
            print("[Refiner] → ルールベース整形で動作します")
            return

        self._timeout    = MODEL_TIMEOUTS.get(model, 45)
        self._available  = True
        self._use_server = False
        print(f"[Refiner] LLM整形: {model_path.name} (CLIモード, タイムアウト: {self._timeout}秒)")

    # ── サーバー起動（バックグラウンドスレッドで実行）────────────────
    def _start_server(self):
        """llama-server.exe をバックグラウンド起動し、ヘルスチェックで準備完了を待つ。"""
        cmd = [
            str(self._llama_server),
            "-m",          str(self._model_path),
            "-ngl",        _N_GPU,
            "--port",      str(self._server_port),
            "--ctx-size",  _N_CTX,
            "--log-disable",
        ]
        try:
            self._server_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            print(f"[Refiner] サーバー起動失敗: {e}")
            self._use_server = False
            return

        health_url = f"http://localhost:{self._server_port}/health"
        deadline   = time.time() + _STARTUP_TIMEOUT

        while time.time() < deadline:
            # プロセスが早期終了していたら失敗
            if self._server_proc.poll() is not None:
                print("[Refiner] llama-server が予期せず終了しました")
                self._use_server = False
                return
            try:
                with urllib.request.urlopen(health_url, timeout=2) as resp:
                    status = json.loads(resp.read()).get("status")
                    if status == "ok":
                        print("[Refiner] llama-server 準備完了（以降の推論は高速になります）")
                        self._server_ready.set()
                        return
            except Exception:
                pass
            time.sleep(1.0)

        print(f"[Refiner] llama-server が {_STARTUP_TIMEOUT}秒 以内に起動しませんでした")
        self._server_proc.terminate()
        self._server_proc = None
        self._use_server  = False

    # ── 公開 API ─────────────────────────────────────────────────────
    def refine(self, raw_text: str) -> str:
        """音声認識テキストを整形して返す。失敗時はルールベース結果を返す。"""
        if not raw_text.strip():
            return ""

        if not self._available:
            return self._fallback.refine(raw_text)

        try:
            if self._use_server:
                refined = self._llm_refine_server(raw_text)
            else:
                refined = self._llm_refine_cli(raw_text)
        except Exception as e:
            print(f"[Refiner] エラー ({type(e).__name__}): {e}。ルールベースにフォールバックします")
            return self._fallback.refine(raw_text)

        # 過剰編集チェック: 元テキストとの類似度が低い場合はルールベースに差し替え
        if not _is_faithful(raw_text, refined):
            print("[Refiner] 過剰編集を検出。ルールベース結果を使用します")
            return self._fallback.refine(raw_text)

        return refined

    def shutdown(self):
        """サーバープロセスを終了する（アプリ終了時に呼ぶ）。"""
        if self._server_proc and self._server_proc.poll() is None:
            print("[Refiner] llama-server 終了中...")
            self._server_proc.terminate()
            try:
                self._server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._server_proc.kill()
            print("[Refiner] llama-server 終了しました")
        self._server_proc = None

    # ── 内部メソッド: サーバーモード ─────────────────────────────────
    def _llm_refine_server(self, raw_text: str) -> str:
        # サーバーが準備完了するまで待機（Workerスレッドでブロック、UIは影響なし）
        if not self._server_ready.wait(timeout=_STARTUP_TIMEOUT):
            print("[Refiner] サーバー準備待機タイムアウト。ルールベースにフォールバック")
            return self._fallback.refine(raw_text)

        prompt    = _build_prompt(self.model_key, raw_text)
        stop_tok  = ["<turn|>"] if self.model_key.startswith("gemma4") else ["<|im_end|>"]
        payload = json.dumps({
            "prompt":         prompt,
            "n_predict":      int(_N_PRED),
            "temperature":    float(_TEMP),
            "top_p":          float(_TOP_P),
            "top_k":          int(_TOP_K),
            "repeat_penalty": float(_REPEAT_PENALTY),
            "repeat_last_n":  int(_REPEAT_LAST_N),
            "stop":           stop_tok,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"http://localhost:{self._server_port}/completion",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        print(f"[Refiner] LLM整形開始 (サーバー): {raw_text!r}")
        with urllib.request.urlopen(req, timeout=_INFER_TIMEOUT) as resp:
            content = json.loads(resp.read()).get("content", "")

        refined = _parse_llm_output(content, self.model_key)
        if not refined:
            print("[Refiner] LLM出力が空でした。ルールベースにフォールバックします")
            return self._fallback.refine(raw_text)

        print(f"[Refiner] 整形前:\n  → {raw_text!r}")
        print(f"[Refiner] 整形後:\n  → {refined!r}")
        return refined

    # ── 内部メソッド: CLI サブプロセスモード ─────────────────────────
    def _llm_refine_cli(self, raw_text: str) -> str:
        prompt = _build_prompt(self.model_key, raw_text)
        cmd = [
            str(self._llama_cli),
            "-m",                str(self._model_path),
            "-p",                prompt,
            "-n",                _N_PRED,
            "-c",                _N_CTX,
            "--temp",            _TEMP,
            "--top-p",           _TOP_P,
            "--top-k",           _TOP_K,
            "--repeat-penalty",  _REPEAT_PENALTY,
            "--repeat-last-n",   _REPEAT_LAST_N,
            "-ngl",              _N_GPU,
            "--log-disable",
        ]
        if self.model_key.startswith("gemma4"):
            cmd += ["--stop", "<turn|>"]

        print(f"[Refiner] LLM整形開始 (CLI): {raw_text!r}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"llama-cli.exe が失敗しました (code={result.returncode}):\n"
                f"{result.stderr[:500]}"
            )

        refined = _parse_llm_output(result.stdout, self.model_key)
        if not refined:
            print("[Refiner] LLM出力が空でした。ルールベースにフォールバックします")
            return self._fallback.refine(raw_text)

        print(f"[Refiner] 整形前:\n  → {raw_text!r}")
        print(f"[Refiner] 整形後:\n  → {refined!r}")
        return refined


# ─────────────────────────────────────
# RuleBasedRefiner クラス (フォールバック)
# ─────────────────────────────────────
# フィラーパターン定義 (順序重要: 長いパターンを先に)
_FILLER_PATTERNS = [
    r'えーっと[、,，\s]*', r'えーと[、,，\s]*', r'えっと[、,，\s]*', r'えー[、,，\s]+',
    r'あのー[、,，\s]*',   r'あのう[、,，\s]*', r'あの[、,，\s]+',
    r'うーんと[、,，\s]*', r'うーん[、,，\s]*',
    r'まーあ[、,，\s]*',   r'まあー[、,，\s]*', r'まあ[、,，\s]+',
    r'なんかー[、,，\s]*', r'なんか[、,，\s]+',
    r'そのー[、,，\s]*',
    r'ほらー[、,，\s]*',   r'ほら[、,，\s]+',
    r'ねー[、,，\s]+',
    r'やっぱりー[、,，\s]*', r'やっぱー[、,，\s]*',
]
_COMPILED_PATTERNS = [re.compile(p) for p in _FILLER_PATTERNS]


class RuleBasedRefiner:
    """
    正規表現ルールによりフィラー・言い淀みを除去する。
    LlamaRefiner のフォールバックとして、または単体でも使用可能。
    """

    def refine(self, raw_text: str) -> str:
        if not raw_text.strip():
            return ""
        text = raw_text
        for pattern in _COMPILED_PATTERNS:
            text = pattern.sub("", text)
        text = re.sub(r"[\s\u3000]+", " ", text).strip()
        print(f"[Refiner] ルールベース: {raw_text!r} → {text!r}")
        return text


# ─────────────────────────────────────
# 単体テスト用エントリポイント
# ─────────────────────────────────────
if __name__ == "__main__":
    import sys

    model_key = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REFINER_MODEL
    refiner   = LlamaRefiner(model=model_key)
    mode      = "サーバー" if refiner._use_server else ("LLM(CLI)" if refiner._available else "ルールベース")
    print(f"\n使用中: {mode}\n")

    test_cases = [
        "えーっとですね、あのー、今日の会議なんですけど、まあ、3時からに変更になったっていう感じです",
        "なんか、そのー、プロジェクトの進捗がですね、えっと、少し遅れてますが来週中には完成する予定です",
    ]
    for raw in test_cases:
        print(f"\n入力: {raw}")
        print(f"出力: {refiner.refine(raw)}")

    print("\n--- インタラクティブ入力 (空行で終了) ---")
    try:
        while True:
            text = input("> ").strip()
            if not text:
                break
            print(f"→ {refiner.refine(text)}\n")
    finally:
        refiner.shutdown()
