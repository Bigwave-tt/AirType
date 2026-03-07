"""
AirType - Step 3: Ollama API によるテキスト整形
 
設計:
- OllamaRefiner クラスで Ollama の /api/generate エンドポイントをラップ
- DeepSeek-R1 系モデルが出力する <think>...</think> タグを除去
- タイムアウト・接続エラーは例外として上位に伝播 (Step 5 でハンドリング)
- 単体テスト: このファイルを直接実行するとテキスト整形を試せる
"""
 
import re
import json
from typing import Optional
 
import requests
 
 
# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "deepseek-r1:7b"      # ollama pull deepseek-r1:7b で取得
REQUEST_TIMEOUT = 120                  # 秒 (LLM 推論には時間がかかる)
 
# 整形プロンプト
REFINE_SYSTEM_PROMPT = (
    "あなたは音声認識テキストの校正アシスタントです。"
    "入力テキストと同じ言語で出力してください。絶対に翻訳しないでください。"
    "与えられたテキストのみを修正し、説明や前置き、コメントは一切出力しないでください。"
)
 
REFINE_USER_TEMPLATE = """\
以下の音声認識テキストから「えーっと」「あのー」「まあ」「なんか」などのフィラーや言い淀みを取り除き、\
文脈を補完して、文法的に自然で読みやすい文章に修正してください。\
入力が日本語なら日本語で、英語なら英語で出力してください。絶対に翻訳しないでください。\
修正後のテキストのみを出力してください。
 
音声認識テキスト:
{raw_text}"""
 
 
# ─────────────────────────────────────
# OllamaRefiner クラス
# ─────────────────────────────────────
class OllamaRefiner:
    """
    Ollama の REST API を使ってテキストを整形する。
 
    Parameters
    ----------
    model : str
        Ollama で pull 済みのモデル名 (例: "deepseek-r1:7b")
    base_url : str
        Ollama サーバーの URL (デフォルト: http://localhost:11434)
    timeout : int
        API タイムアウト秒数
    """
 
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = OLLAMA_BASE_URL,
        timeout: int = REQUEST_TIMEOUT,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._check_connection()
 
    def _check_connection(self):
        """Ollama サーバーへの疎通確認"""
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            print(f"[Refiner] Ollama 接続OK  利用可能モデル: {models}")
            if self.model not in models:
                print(f"[警告] モデル '{self.model}' が見つかりません。")
                print(f"  → ollama pull {self.model}  を実行してください")
        except requests.exceptions.ConnectionError:
            raise ConnectionError(
                f"Ollama に接続できません ({self.base_url})\n"
                "Ollama が起動しているか確認してください: ollama serve"
            )
 
    @staticmethod
    def _strip_think_tags(text: str) -> str:
        """
        DeepSeek-R1 系モデルが出力する <think>...</think> ブロックを除去する。
        入れ子になっている場合も考慮して貪欲でなく全体を除去する。
        """
        # DOTALL フラグで改行を含む複数行の <think> ブロックに対応
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        return cleaned.strip()
 
    def refine(self, raw_text: str) -> str:
        """
        音声認識テキストを整形して返す。
 
        Parameters
        ----------
        raw_text : str
            faster-whisper が出力した生テキスト
 
        Returns
        -------
        str
            整形済みテキスト
        """
        if not raw_text.strip():
            return ""
 
        prompt = REFINE_USER_TEMPLATE.format(raw_text=raw_text)
 
        payload = {
            "model": self.model,
            "system": REFINE_SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": False,    # stream=True にすると逐次出力可能 (Step 5 で検討)
            "options": {
                "temperature": 0.3,   # 低めで安定した出力を得る
                "top_p": 0.9,
            },
        }
 
        print(f"[Refiner] Ollama API 呼び出し中 (model={self.model}) ...")
 
        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            raise TimeoutError(f"Ollama API がタイムアウトしました ({self.timeout}秒)")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Ollama API エラー: {e}")
 
        raw_response = resp.json().get("response", "")
        refined = self._strip_think_tags(raw_response)
 
        print(f"[Refiner] 整形前:\n  → {raw_text!r}")
        print(f"[Refiner] 整形後:\n  → {refined!r}")
 
        return refined
 
    def is_available(self) -> bool:
        """Ollama が利用可能かどうかを確認する (例外なし版)"""
        try:
            requests.get(f"{self.base_url}/api/tags", timeout=3)
            return True
        except Exception:
            return False
 
 
# ─────────────────────────────────────
# 単体テスト用エントリポイント
# ─────────────────────────────────────
def _test_interactive():
    """対話形式でテキスト整形をテストする"""
    refiner = OllamaRefiner()
 
    test_cases = [
        "えーっとですね、あのー、今日の会議なんですけど、まあ、あのー、3時からに変更になったっていう感じです",
        "なんか、そのー、プロジェクトの進捗がですね、えっと、少し遅れてるんですが、まあ来週中には完成する予定です",
        "あのー、田中さんに、えーと、資料を送っておいてもらえますか、なんか明日の朝までに",
    ]
 
    print("\n" + "="*50)
    print("テキスト整形テスト")
    print("="*50)
 
    for i, raw in enumerate(test_cases, 1):
        print(f"\n--- テストケース {i} ---")
        refined = refiner.refine(raw)
        print(f"入力: {raw}")
        print(f"出力: {refined}")
 
    # インタラクティブ入力モード
    print("\n" + "="*50)
    print("カスタムテキストを入力してください (空行で終了):")
    while True:
        text = input("> ").strip()
        if not text:
            break
        result = refiner.refine(text)
        print(f"→ {result}\n")
 
 
if __name__ == "__main__":
    _test_interactive()