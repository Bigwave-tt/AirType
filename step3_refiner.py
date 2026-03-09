"""
AirType - Step 3: テキスト整形

LlamaRefiner   : llama.cpp (Vulkan GPU) で Qwen 3.5 を使った LLM 整形 [優先]
RuleBasedRefiner: 正規表現によるフィラー除去 [フォールバック / 単体利用も可]

フォールバック戦略:
  llama-cli.exe / モデルファイルが存在しない場合、またはタイムアウト・
  エラーが発生した場合は、自動的に RuleBasedRefiner の結果を返す。
  パイプライン全体を止めることはない。

フォルダ構成:
  AirType/
    AirType/              ← このファイルがある場所
    llama.cpp-windows-vulkan/
      llama-cli.exe
      qwen3.5-2b-instruct-q5_k_m.gguf   ← 推奨 (速度重視・約2GB)
      qwen3.5-4b-instruct-q5_k_m.gguf   ← 高精度 (約4GB)
"""

import re
import subprocess
from pathlib import Path

# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
_HERE     = Path(__file__).parent
LLAMA_DIR = _HERE.parent / "llama.cpp-windows-vulkan"
LLAMA_CLI = LLAMA_DIR / "llama-cli.exe"

REFINER_MODELS = {
    "qwen3.5-2b": LLAMA_DIR / "qwen3.5-2b-instruct-q5_k_m.gguf",
    "qwen3.5-4b": LLAMA_DIR / "qwen3.5-4b-instruct-q5_k_m.gguf",
}
DEFAULT_REFINER_MODEL = "qwen3.5-2b"

MODEL_TIMEOUTS = {
    "qwen3.5-2b": 20,
    "qwen3.5-4b": 35,
}

# 非思考モード誘導サンプリングパラメータ (Qwen 3 系共通)
_TEMP   = "0.7"
_TOP_P  = "0.8"
_TOP_K  = "20"
_N_GPU  = "99"    # Vulkan GPU へのオフロード層数
_N_CTX  = "2048"  # コンテキスト長
_N_PRED = "256"   # 最大生成トークン数 (整形後テキストは元文より長くならない)

_SYSTEM_PROMPT = (
    "あなたは優秀な音声入力アシスタントです。"
    "認識されたテキストからフィラーを削除し、句読点を補って自然な日本語にしてください。"
    "思考プロセスや解説は一切出力せず、整形後のテキストのみを直接出力してください。"
)


def _build_chatml(raw_text: str) -> str:
    """ChatML 形式のプロンプト文字列を生成する"""
    return (
        f"<|im_start|>system\n{_SYSTEM_PROMPT}<|im_end|>\n"
        f"<|im_start|>user\n{raw_text}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )


def _parse_llm_output(output: str) -> str:
    """
    llama-cli の stdout からアシスタントの返答だけを抽出する。

    - プロンプトがエコーされている場合は <|im_start|>assistant\n 以降を使用
    - <|im_end|> 以降は切り捨て
    - Qwen3 が思考モードに入った場合の <think>...</think> ブロックを除去
    """
    # プロンプトエコー対策: assistant ターンの開始以降を取得
    marker = "<|im_start|>assistant\n"
    if marker in output:
        output = output.split(marker, 1)[1]

    # EOS / 次のターン以降を切り捨て
    output = output.split("<|im_end|>")[0]

    # 思考ブロックの除去 (非思考モード誘導に失敗した場合の安全策)
    output = re.sub(r"<think>.*?</think>", "", output, flags=re.DOTALL)

    return output.strip()


# ─────────────────────────────────────
# LlamaRefiner クラス (LLM 整形・優先)
# ─────────────────────────────────────
class LlamaRefiner:
    """
    llama.cpp (Vulkan GPU) で Qwen 3.5 を呼び出してテキストを整形する。

    llama-cli.exe またはモデルファイルが見つからない場合、
    タイムアウト・エラーが発生した場合は RuleBasedRefiner で代替する。

    Parameters
    ----------
    model : str
        使用するモデルキー。REFINER_MODELS のいずれか。
    """

    def __init__(self, model: str = DEFAULT_REFINER_MODEL):
        self._fallback  = RuleBasedRefiner()
        self._available = False
        self.model_key  = model

        if not LLAMA_CLI.exists():
            print(f"[Refiner] llama-cli.exe が見つかりません: {LLAMA_CLI}")
            print("[Refiner] → ルールベース整形で動作します")
            return

        model_path = REFINER_MODELS.get(model)
        if model_path is None:
            print(f"[Refiner] 不明なモデルキー: {model!r}")
            print("[Refiner] → ルールベース整形で動作します")
            return

        if not model_path.exists():
            print(f"[Refiner] モデルファイルが見つかりません: {model_path.name}")
            print("[Refiner] → ルールベース整形で動作します")
            return

        self._model_path = model_path
        self._timeout    = MODEL_TIMEOUTS.get(model, 30)
        self._available  = True
        print(f"[Refiner] LLM整形: {model_path.name}")
        print(f"[Refiner] タイムアウト: {self._timeout}秒")

    def refine(self, raw_text: str) -> str:
        """音声認識テキストを整形して返す。失敗時はルールベース結果を返す。"""
        if not raw_text.strip():
            return ""

        if not self._available:
            return self._fallback.refine(raw_text)

        try:
            return self._llm_refine(raw_text)
        except subprocess.TimeoutExpired:
            print(f"[Refiner] タイムアウト ({self._timeout}秒)。ルールベースにフォールバックします")
            return self._fallback.refine(raw_text)
        except Exception as e:
            print(f"[Refiner] エラー: {e}。ルールベースにフォールバックします")
            return self._fallback.refine(raw_text)

    def _llm_refine(self, raw_text: str) -> str:
        prompt = _build_chatml(raw_text)
        cmd = [
            str(LLAMA_CLI),
            "-m",       str(self._model_path),
            "-p",       prompt,
            "-n",       _N_PRED,
            "-c",       _N_CTX,
            "--temp",   _TEMP,
            "--top-p",  _TOP_P,
            "--top-k",  _TOP_K,
            "-ngl",     _N_GPU,
            "--log-disable",  # stderr へのログを抑制
        ]

        print(f"[Refiner] LLM整形開始: {raw_text!r}")
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

        refined = _parse_llm_output(result.stdout)
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
    print(f"\n使用中: {'LLM' if refiner._available else 'ルールベース'}\n")

    test_cases = [
        "えーっとですね、あのー、今日の会議なんですけど、まあ、3時からに変更になったっていう感じです",
        "なんか、そのー、プロジェクトの進捗がですね、えっと、少し遅れてますが来週中には完成する予定です",
    ]
    for raw in test_cases:
        print(f"\n入力: {raw}")
        print(f"出力: {refiner.refine(raw)}")

    print("\n--- インタラクティブ入力 (空行で終了) ---")
    while True:
        text = input("> ").strip()
        if not text:
            break
        print(f"→ {refiner.refine(text)}\n")
