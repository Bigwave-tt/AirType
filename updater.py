"""
AirType - llama.cpp 自動アップデート機能

起動時に GitHub API で最新リリースを確認し、ローカルより新しければ
ユーザーに確認後、ZIP をダウンロードして exe/dll を差し替える。
GGUF モデルファイルは差し替えの対象外。

フォールバック戦略:
  ネットワーク不通・GitHub API エラー・バージョン取得失敗の場合は
  サイレントにスキップし、通常起動を妨げない。
"""

import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

_RELEASES_URL   = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
_ASSET_PATTERN  = re.compile(r"llama-b\d+-bin-win-vulkan-x64\.zip")
_VERSION_RE     = re.compile(r"version:\s*(\d+)")

# 差し替え対象の拡張子（GGUF は除外）
_UPDATE_SUFFIXES = {".exe", ".dll"}


class LlamaUpdater:
    """
    llama.cpp のバージョンチェックとファイル差し替えを行うクラス。

    Parameters
    ----------
    llama_dir : Path
        llama.cpp のバイナリが置かれているフォルダ。
    """

    def __init__(self, llama_dir: Path):
        self._dir        = Path(llama_dir)
        self._server_exe = self._dir / "llama-server.exe"

    # ── ローカルバージョン取得 ────────────────────────────────────────
    def get_local_version(self) -> int | None:
        """llama-server.exe --version からビルド番号を取得する。"""
        if not self._server_exe.exists():
            return None
        try:
            result = subprocess.run(
                [str(self._server_exe), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            combined = result.stdout + result.stderr
            match = _VERSION_RE.search(combined)
            return int(match.group(1)) if match else None
        except Exception as e:
            print(f"[Updater] ローカルバージョン取得失敗: {e}")
            return None

    # ── GitHub API: 最新リリース取得 ─────────────────────────────────
    def get_latest_release(self) -> dict | None:
        """
        GitHub API で最新リリース情報を取得する。

        Returns
        -------
        dict | None
            {"build": int, "tag": str, "url": str,
             "asset_url": str, "asset_name": str}
            アセットが見つからない・エラーの場合は None。
        """
        try:
            req = urllib.request.Request(
                _RELEASES_URL,
                headers={
                    "Accept":     "application/vnd.github.v3+json",
                    "User-Agent": "AirType-Updater",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            tag   = data["tag_name"]               # "b8302"
            build = int(tag.lstrip("b"))

            for asset in data.get("assets", []):
                if _ASSET_PATTERN.match(asset["name"]):
                    return {
                        "build":      build,
                        "tag":        tag,
                        "url":        data["html_url"],
                        "asset_url":  asset["browser_download_url"],
                        "asset_name": asset["name"],
                    }

            print("[Updater] Windows Vulkan アセットが見つかりませんでした")
            return None

        except Exception as e:
            print(f"[Updater] GitHub API 取得失敗: {e}")
            return None

    # ── バージョン比較 ────────────────────────────────────────────────
    def check_update(self) -> dict | None:
        """
        ローカルと最新を比較し、更新がある場合はリリース情報を返す。

        Returns
        -------
        dict | None
            更新がある場合は get_latest_release() の戻り値（local_build キー付き）。
            更新なし・エラーの場合は None。
        """
        local = self.get_local_version()
        if local is None:
            print("[Updater] ローカルバージョン不明のためスキップ")
            return None

        latest = self.get_latest_release()
        if latest is None:
            return None

        print(f"[Updater] ローカル: b{local}  最新: {latest['tag']}")

        if latest["build"] <= local:
            print("[Updater] 最新バージョンを使用中です")
            return None

        latest["local_build"] = local
        return latest

    # ── ダウンロード・差し替え ────────────────────────────────────────
    def download_and_apply(
        self,
        asset_url: str,
        on_progress=None,
    ) -> bool:
        """
        ZIP をダウンロードし、exe/dll のみを差し替える。

        - GGUF モデルファイルは保持する
        - 旧ファイルは .bak にリネームしてからコピー（部分失敗時も元ファイルは残る）
        - .bak は差し替え成功後に削除する

        Parameters
        ----------
        asset_url : str
            GitHub リリースアセットのダウンロード URL。
        on_progress : callable | None
            進捗コールバック。on_progress(downloaded_bytes, total_bytes) で呼ばれる。

        Returns
        -------
        bool
            成功した場合 True。
        """
        try:
            with tempfile.TemporaryDirectory() as tmp_str:
                tmp = Path(tmp_str)
                zip_path = tmp / "llama_update.zip"

                # ── 1. ZIP ダウンロード ──────────────────────────────
                print(f"[Updater] ダウンロード開始: {asset_url}")
                req = urllib.request.Request(
                    asset_url,
                    headers={"User-Agent": "AirType-Updater"},
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    total = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    chunk = 65536
                    with open(zip_path, "wb") as f:
                        while True:
                            buf = resp.read(chunk)
                            if not buf:
                                break
                            f.write(buf)
                            downloaded += len(buf)
                            if on_progress:
                                on_progress(downloaded, total)

                print(f"[Updater] ダウンロード完了: {downloaded // 1024 // 1024} MB")

                # ── 2. ZIP 展開 ──────────────────────────────────────
                extract_dir = tmp / "extracted"
                extract_dir.mkdir()
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)

                # ZIP 内のサブディレクトリを探す（build/ や llama-bXXXX/ など）
                subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
                src_dir = subdirs[0] if subdirs else extract_dir

                # ── 3. exe/dll のみ差し替え（GGUF は除外）──────────
                new_files  = [f for f in src_dir.iterdir()
                              if f.suffix in _UPDATE_SUFFIXES]
                bak_pairs  = []  # (元パス, .bak パス) のリスト（ロールバック用）

                for src in new_files:
                    dst = self._dir / src.name
                    bak = dst.with_suffix(dst.suffix + ".bak")

                    # 既存ファイルを .bak に退避
                    if dst.exists():
                        dst.rename(bak)
                        bak_pairs.append((dst, bak))

                    # 新ファイルをコピー
                    shutil.copy2(src, dst)
                    print(f"[Updater] 更新: {src.name}")

                # ── 4. .bak を削除（差し替え成功確認後）────────────
                for _, bak in bak_pairs:
                    try:
                        bak.unlink()
                    except Exception:
                        pass  # 削除失敗しても動作に影響なし

                print("[Updater] 差し替え完了")
                return True

        except Exception as e:
            print(f"[Updater] アップデート失敗: {e}")
            return False


# ─────────────────────────────────────
# 単体テスト用エントリポイント
# ─────────────────────────────────────
if __name__ == "__main__":
    from pathlib import Path as _Path
    _HERE = _Path(__file__).parent
    _LLAMA_DIR = _HERE.parent / "llama.cpp-windows-vulkan"

    updater = LlamaUpdater(_LLAMA_DIR)

    print(f"ローカルバージョン: b{updater.get_local_version()}")

    info = updater.check_update()
    if info:
        print(f"更新あり: {info['tag']} (現在: b{info['local_build']})")
        print(f"アセット: {info['asset_name']}")
    else:
        print("更新なし")
