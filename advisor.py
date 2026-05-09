"""
AirType - 最適設定アドバイザー

ハードウェア自動診断 + ユーザー問診 → 最適なモデル設定を推奨する。
"""

import json
import platform
import re
import subprocess
from pathlib import Path
from typing import Optional


# ─── ハードウェア検出 ────────────────────────────────────────────────────

def detect_hardware() -> dict:
    """利用可能なハードウェア情報を収集して返す"""
    info = {
        "gpu_name":          "不明",
        "gpu_vendor":        "unknown",
        "vram_gb":           0.0,
        "unified_memory":    False,
        "effective_vram_gb": 0.0,
        "npu_name":          None,
        "directml":          False,
        "vulkan":            False,
        "ram_gb":            0.0,
        "cpu_name":          "",
        "cpu_cores":         0,
        "cpu_threads":       0,
    }
    _detect_cpu_ram(info)
    _detect_gpu(info)
    _detect_npu(info)
    _detect_runtime(info)
    info["effective_vram_gb"] = (
        min(info["ram_gb"] * 0.5, 16.0) if info["unified_memory"] else info["vram_gb"]
    )
    return info


def _run_ps(script: str, timeout: int = 10) -> str:
    """PowerShell スクリプトを実行して stdout を返す。失敗時は空文字。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _detect_cpu_ram(info: dict) -> None:
    # platform.processor() は CPUID 文字列になる場合があるため WMI を優先
    out = _run_ps(
        "Get-WmiObject Win32_Processor"
        " | Select-Object -First 1 -ExpandProperty Name"
    )
    info["cpu_name"] = out.strip() if out else platform.processor()

    try:
        import psutil
        info["ram_gb"]      = psutil.virtual_memory().total / (1024 ** 3)
        info["cpu_cores"]   = psutil.cpu_count(logical=False) or 0
        info["cpu_threads"] = psutil.cpu_count(logical=True)  or 0
    except Exception:
        pass

    # psutil が利用不可の場合は WMI でフォールバック
    if info["ram_gb"] == 0.0:
        out = _run_ps("(Get-WmiObject Win32_ComputerSystem).TotalPhysicalMemory")
        if out and re.match(r"^\d+$", out):
            info["ram_gb"] = round(int(out) / (1024 ** 3), 1)
    if info["cpu_cores"] == 0:
        out = _run_ps(
            "(Get-WmiObject Win32_Processor"
            " | Measure-Object -Property NumberOfCores -Sum).Sum"
        )
        if out and re.match(r"^\d+$", out):
            info["cpu_cores"] = int(out)
    if info["cpu_threads"] == 0:
        out = _run_ps(
            "(Get-WmiObject Win32_Processor"
            " | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum"
        )
        if out and re.match(r"^\d+$", out):
            info["cpu_threads"] = int(out)


def _detect_gpu(info: dict) -> None:
    # GPU名・ベンダーを WMI で取得（VRAM は 4GB キャップがあるが名前は正確）
    out = _run_ps(
        "Get-WmiObject Win32_VideoController "
        "| Select-Object Name,AdapterRAM "
        "| ConvertTo-Json -Depth 1 -Compress"
    )
    if out:
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            best = max(data, key=lambda d: d.get("AdapterRAM") or 0)
            _parse_wmi_gpu(info, best)
        except Exception:
            pass

    # DXGI API で正確な VRAM を取得（4GB 上限なし・全ベンダー対応）
    vram_dxgi = _vram_via_dxgi()
    if vram_dxgi > 0:
        info["vram_gb"] = vram_dxgi
    elif info["gpu_vendor"] == "nvidia":
        _refine_nvidia_vram(info)
    elif info["gpu_vendor"] == "amd":
        v = _amd_vram_registry()
        if v > 0:
            info["vram_gb"] = v

    _classify_unified_memory(info)


def _parse_wmi_gpu(info: dict, data: dict) -> None:
    name = (data.get("Name") or "").strip()
    if not name:
        return
    info["gpu_name"] = name
    nl = name.lower()
    if any(k in nl for k in ("nvidia", "geforce", "quadro", "tesla", "rtx", "gtx")):
        info["gpu_vendor"] = "nvidia"
    elif any(k in nl for k in ("amd", "radeon")):
        info["gpu_vendor"] = "amd"
    elif "intel" in nl:
        info["gpu_vendor"] = "intel"
    elif any(k in nl for k in ("qualcomm", "adreno")):
        info["gpu_vendor"] = "qualcomm"
    ram = data.get("AdapterRAM") or 0
    if ram > 0:
        info["vram_gb"] = round(ram / (1024 ** 3), 1)


def _vram_via_dxgi() -> float:
    """DXGI API で GPU 専用メモリを取得する（WMI の 4GB 上限を回避・全ベンダー対応）"""
    try:
        import ctypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_uint8 * 8),
            ]

        class _DESC(ctypes.Structure):
            _fields_ = [
                ("Description",           ctypes.c_wchar * 128),
                ("VendorId",              ctypes.c_uint),
                ("DeviceId",              ctypes.c_uint),
                ("SubSysId",              ctypes.c_uint),
                ("Revision",              ctypes.c_uint),
                ("DedicatedVideoMemory",  ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory",    ctypes.c_size_t),
                ("LuidLow",               ctypes.c_uint),
                ("LuidHigh",              ctypes.c_int),
            ]

        # IDXGIFactory GUID: {7b7166ec-21c7-44ae-b21a-c9ae321ae369}
        iid = _GUID(0x7b7166ec, 0x21c7, 0x44ae,
                    (ctypes.c_uint8 * 8)(0xb2, 0x1a, 0xc9, 0xae, 0x32, 0x1a, 0xe3, 0x69))

        dxgi = ctypes.WinDLL("dxgi")
        dxgi.CreateDXGIFactory.restype  = ctypes.c_int32
        dxgi.CreateDXGIFactory.argtypes = [ctypes.POINTER(_GUID),
                                            ctypes.POINTER(ctypes.c_void_p)]
        factory = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory(ctypes.byref(iid), ctypes.byref(factory)) != 0 or not factory:
            return 0.0

        def _vtable_fn(obj: ctypes.c_void_p, idx: int) -> int:
            """COM vtable の idx 番目の関数ポインタを整数アドレスで返す"""
            # POINTER(c_void_p) のインデックスアクセスは Python int を返す
            vt_addr = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
            fn_addr = ctypes.cast(ctypes.c_void_p(vt_addr),
                                  ctypes.POINTER(ctypes.c_void_p))[idx]
            return fn_addr  # int

        # IDXGIFactory vtable: [7]=EnumAdapters, [2]=Release
        EnumAdapters = ctypes.WINFUNCTYPE(
            ctypes.c_int32, ctypes.c_void_p,
            ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p),
        )(_vtable_fn(factory, 7))

        max_vram = 0.0
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            if EnumAdapters(factory, i, ctypes.byref(adapter)) != 0:
                break

            # IDXGIAdapter vtable: [8]=GetDesc, [2]=Release
            GetDesc = ctypes.WINFUNCTYPE(
                ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(_DESC),
            )(_vtable_fn(adapter, 8))

            desc = _DESC()
            if GetDesc(adapter, ctypes.byref(desc)) == 0:
                vram = desc.DedicatedVideoMemory / (1024 ** 3)
                if vram > max_vram:
                    max_vram = vram

            ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(
                _vtable_fn(adapter, 2)
            )(adapter)
            i += 1

        ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(
            _vtable_fn(factory, 2)
        )(factory)

        return round(max_vram, 1) if max_vram > 0 else 0.0

    except Exception as e:
        print(f"[Advisor] DXGI VRAM 検出失敗: {e}")
        return 0.0


def _refine_nvidia_vram(info: dict) -> None:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000,
        )
        val = r.stdout.strip()
        if r.returncode == 0 and val.isdigit():
            info["vram_gb"] = round(int(val) / 1024, 1)
    except Exception:
        pass


def _amd_vram_registry() -> float:
    """AMD GPU の VRAM をレジストリから取得する（WMI の 4GB 上限を回避）"""
    reg_key = r"HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    script = (
        f"$p='{reg_key}';"
        "$m=0;"
        "Get-ChildItem $p -EA 0"
        " | Where-Object {$_.PSChildName -match '^[0-9]{4}$'}"
        " | ForEach-Object {"
        "   $v=(Get-ItemProperty $_.PSPath"
        "     -Name 'HardwareInformation.MemorySize' -EA 0).'HardwareInformation.MemorySize';"
        "   if($v -and $v -gt $m){$m=$v}"
        " };"
        "$m"
    )
    out = _run_ps(script, timeout=15)
    if out and re.match(r"^\d+$", out):
        return round(int(out) / (1024 ** 3), 1)
    return 0.0


_IGPU_RE = re.compile(
    r"Intel (UHD|Iris|HD Graphics)|Radeon\s*(Vega|Graphics)\b|AMD Radeon\(TM\)|Adreno",
    re.IGNORECASE,
)


def _classify_unified_memory(info: dict) -> None:
    info["unified_memory"] = bool(_IGPU_RE.search(info["gpu_name"])) or info["vram_gb"] < 1.0


def _detect_npu(info: dict) -> None:
    keywords = "NPU|Neural|AI Boost|XDNA|Ryzen AI|Hexagon|Intel AI"
    out = _run_ps(
        f'Get-WmiObject Win32_PnPEntity '
        f'| Where-Object {{$_.Name -match "{keywords}"}}'
        f' | Select-Object -First 1 -ExpandProperty Name',
        timeout=12,
    )
    if out:
        info["npu_name"] = out.splitlines()[0].strip()


def _detect_runtime(info: dict) -> None:
    try:
        import onnxruntime as ort
        info["directml"] = "DmlExecutionProvider" in ort.get_available_providers()
    except Exception:
        pass
    for p in (
        Path(r"C:\Windows\System32\vulkan-1.dll"),
        Path(r"C:\Windows\SysWOW64\vulkan-1.dll"),
    ):
        if p.exists():
            info["vulkan"] = True
            break


# ─── インストール済みモデル確認 ──────────────────────────────────────────

_WHISPER_FILES = {
    "kotoba-q5":   "ggml-kotoba-whisper-v2.0-q5_0.bin",
    "kotoba-full": "ggml-kotoba-whisper-v2.0.bin",
}
_LLAMA_FILES = {
    "qwen3.5-2b": "Qwen3.5-2B-Q5_K_M.gguf",
    "qwen3.5-4b": "qwen3.5-4b-instruct-q5_k_m.gguf",
    "gemma4-e2b": "google_gemma-4-E2B-it-Q8_0.gguf",
    "gemma4-e4b": "google_gemma-4-E4B-it-Q8_0.gguf",
}
_SV_REQUIRED = ["sense-voice-encoder.onnx", "chn_jpn_yue_eng_ko_spectok.bpe.model"]


def check_installed(cfg: dict) -> dict:
    """インストール済みのモデル・実行ファイルを確認する"""
    import config as _config

    wd = _config.resolve_dir(cfg.get("whisper",     {}).get("dir",            ""), "whisper.cpp-windows-vulkan")
    ld = _config.resolve_dir(cfg.get("llama",       {}).get("dir",            ""), "llama.cpp-windows-vulkan")
    sd = _config.resolve_dir(cfg.get("transcriber", {}).get("sensevoice_dir", ""), "sensevoice-onnx")

    return {
        "whisper_exe":    (wd / "whisper-server.exe").exists() or (wd / "whisper-cli.exe").exists(),
        "whisper_models": {k for k, f in _WHISPER_FILES.items() if (wd / f).exists()},
        "sensevoice":     all((sd / f).exists() for f in _SV_REQUIRED),
        "llama_exe":      (ld / "llama-server.exe").exists() or (ld / "llama-cli.exe").exists(),
        "llama_models":   {k for k, f in _LLAMA_FILES.items()  if (ld / f).exists()},
    }


# ─── レシピ定義 ────────────────────────────────────────────────────────────

# base_score: 同点時のタイブレーク用の微小値のみ（主な差別化は _qa_bonus が担う）
RECIPES: list[dict] = [
    {
        "id":          "sensevoice_llm",
        "label":       "高速 + 高品質モード",
        "description": "SenseVoice (DirectML) + LLM整形。速くて高品質。",
        "min_vram":    4.0,
        "needs_dml":   True,
        "base_score":  5,
        "config": {
            "transcriber.backend": "sensevoice",
            "refiner.enabled":     True,
        },
        "needs_sv":    True,
        "needs_llama": True,
    },
    {
        "id":          "balanced_whisper",
        "label":       "バランスモード (Whisper)",
        "description": "Whisper (標準モデル) + LLM整形。速度と精度のバランスが良い。",
        "min_vram":    6.0,
        "needs_vulkan": True,
        "base_score":  4,
        "config": {
            "transcriber.backend":   "whisper",
            "transcriber.model_key": "kotoba-q5",
            "refiner.enabled":       True,
        },
        "needs_wm":    "kotoba-q5",
        "needs_llama": True,
    },
    {
        "id":          "ultra_accuracy",
        "label":       "最高精度モード",
        "description": "Whisper (大モデル) + LLM整形。最高の認識精度。VRAM 8GB以上推奨。",
        "min_vram":    8.0,
        "needs_vulkan": True,
        "base_score":  3,
        "config": {
            "transcriber.backend":   "whisper",
            "transcriber.model_key": "kotoba-full",
            "refiner.enabled":       True,
        },
        "needs_wm":    "kotoba-full",
        "needs_llama": True,
    },
    {
        "id":          "sensevoice_fast",
        "label":       "最速モード",
        "description": "SenseVoice (DirectML) + ルールベース整形。LLM不要で最速動作。",
        "min_vram":    2.0,
        "needs_dml":   True,
        "base_score":  4,
        "config": {
            "transcriber.backend": "sensevoice",
            "refiner.enabled":     False,
        },
        "needs_sv":    True,
        "needs_llama": False,
    },
    {
        "id":          "whisper_simple",
        "label":       "シンプルモード (Whisper)",
        "description": "Whisper (標準モデル) + ルールベース整形のみ。LLM不要。",
        "min_vram":    4.0,
        "needs_vulkan": True,
        "base_score":  2,
        "config": {
            "transcriber.backend":   "whisper",
            "transcriber.model_key": "kotoba-q5",
            "refiner.enabled":       False,
        },
        "needs_wm":    "kotoba-q5",
        "needs_llama": False,
    },
    {
        "id":          "lightweight",
        "label":       "軽量モード",
        "description": "SenseVoice + ルールベース整形。低スペックPCやiGPUでも動作可能。",
        "min_vram":    0.0,
        "base_score":  1,
        "config": {
            "transcriber.backend": "sensevoice",
            "refiner.enabled":     False,
        },
        "needs_sv":    True,
        "needs_llama": False,
    },
]


# ─── 推薦ロジック ─────────────────────────────────────────────────────────

def _hw_ok(recipe: dict, hw: dict) -> bool:
    if hw["effective_vram_gb"] < recipe.get("min_vram", 0):
        return False
    if recipe.get("needs_vulkan") and not hw["vulkan"]:
        return False
    if recipe.get("needs_dml") and not hw["directml"]:
        return False
    return True


def _installed_ok(recipe: dict, inst: dict) -> bool:
    if recipe.get("needs_sv") and not inst["sensevoice"]:
        return False
    wm = recipe.get("needs_wm")
    if wm and (not inst["whisper_exe"] or wm not in inst["whisper_models"]):
        return False
    if recipe.get("needs_llama") and (not inst["llama_exe"] or not inst["llama_models"]):
        return False
    return True


def _qa_bonus(recipe: dict, answers: dict) -> int:
    """問診回答によるスコアボーナス（base_score より大きな値で差別化する）"""
    bonus  = 0
    env    = answers.get("env",      "normal")
    prio   = answers.get("priority", "balance")
    lang   = answers.get("language", "ja")
    cfg    = recipe["config"]
    back   = cfg.get("transcriber.backend",   "whisper")
    refine = cfg.get("refiner.enabled",        True)
    model  = cfg.get("transcriber.model_key",  "")

    # ── 使用環境 ──────────────────────────────────────────────────────────
    if env == "noisy":
        if back == "sensevoice":  bonus += 20   # ノイズ耐性が高い
        else:                     bonus -= 5    # 雑音でWhisperの認識精度が落ちやすい

    # ── 優先度（最も重要な差別化）────────────────────────────────────────
    if prio == "speed":
        # LLMなし SenseVoice が最速
        if back == "sensevoice" and not refine: bonus += 40
        elif back == "sensevoice" and refine:   bonus += 20
        elif back == "whisper" and not refine:  bonus += 10
        # whisper + refiner はレスポンスが遅い → ボーナスなし

    elif prio == "accuracy":
        # 精度最優先: 大モデル + LLM整形が最高
        if model == "kotoba-full" and refine:   bonus += 40
        elif model == "kotoba-q5" and refine:   bonus += 25
        elif back == "sensevoice" and refine:   bonus += 20
        if not refine:                          bonus -= 10  # 整形なしは精度が下がる

    else:  # balance
        # バランス: SenseVoice + LLM が日本語では最もコスパ良し
        if back == "sensevoice" and refine:     bonus += 20
        elif back == "whisper" and model == "kotoba-q5" and refine:
            bonus += 18  # Whisper+LLMも良い選択（英語混在で差がつく）

    # ── 言語 ──────────────────────────────────────────────────────────────
    if lang == "en":
        if back == "whisper":    bonus += 15    # Whisperは多言語に強い
        else:                    bonus -= 5     # SenseVoiceは英語が弱い
    elif lang == "ja":
        if back == "sensevoice": bonus += 5     # 日本語特化

    return bonus


def recommend(hw: dict, answers: dict, inst: dict) -> Optional[dict]:
    """最適なレシピを返す。該当なしの場合は None。"""
    scored = []
    for r in RECIPES:
        if not _hw_ok(r, hw):
            continue
        if not _installed_ok(r, inst):
            continue
        scored.append((_qa_bonus(r, answers) + r["base_score"], r))
    if not scored:
        return None
    return max(scored, key=lambda x: x[0])[1]


# ─── 設定適用 ──────────────────────────────────────────────────────────────

def apply_recipe(recipe: dict) -> bool:
    """レシピの config を airtype_config.json に保存する"""
    import config as _config
    ok = True
    for dotkey, value in recipe["config"].items():
        section, key = dotkey.split(".", 1)
        if not _config.save_value(section, key, value):
            ok = False
    return ok
