#!/usr/bin/env python3
"""
YABBAI Local -- Hardware Detection

Inspects the machine (GPU/VRAM, CPU cores, RAM) and recommends which local model
size is realistic to run. No assumptions about your rig -- it checks and tells you.

Everything here is read-only inspection. No model is downloaded by this module.
"""

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class HardwareProfile:
    os_name: str
    cpu_cores: int
    ram_gb: float
    has_nvidia: bool = False
    has_apple_silicon: bool = False
    gpu_name: Optional[str] = None
    vram_gb: float = 0.0
    notes: List[str] = field(default_factory=list)


@dataclass
class ModelRecommendation:
    tier: str               # 'large' | 'medium' | 'small' | 'tiny'
    model_tag: str          # the Ollama tag to pull
    reason: str
    approx_ram_needed_gb: float
    alternatives: List[str] = field(default_factory=list)


def detect_hardware() -> HardwareProfile:
    os_name = platform.system()
    cpu_cores = os.cpu_count() or 2

    # RAM
    ram_gb = 8.0
    try:
        import psutil
        ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        # Fallback estimate by platform
        ram_gb = 8.0

    profile = HardwareProfile(os_name=os_name, cpu_cores=cpu_cores, ram_gb=ram_gb)

    # Apple Silicon -- unified memory, runs models well via Metal
    if os_name == "Darwin" and platform.machine() == "arm64":
        profile.has_apple_silicon = True
        profile.gpu_name = "Apple Silicon (Metal)"
        # On Apple Silicon, usable model memory ≈ system RAM (unified)
        profile.vram_gb = ram_gb
        profile.notes.append("Apple Silicon detected -- unified memory, Metal acceleration.")

    # NVIDIA -- query nvidia-smi if present
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=8)
            if out.returncode == 0 and out.stdout.strip():
                line = out.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in line.split(",")]
                profile.has_nvidia = True
                profile.gpu_name = parts[0]
                if len(parts) > 1:
                    profile.vram_gb = round(float(parts[1]) / 1024, 1)
                profile.notes.append(f"NVIDIA GPU detected: {profile.gpu_name} "
                                     f"({profile.vram_gb}GB VRAM).")
        except Exception as e:
            profile.notes.append(f"nvidia-smi present but query failed: {e}")

    if not (profile.has_nvidia or profile.has_apple_silicon):
        profile.notes.append("No dedicated GPU detected -- will run on CPU. "
                             "Smaller models recommended; responses will be slower.")

    return profile


def recommend_model(profile: HardwareProfile) -> ModelRecommendation:
    """
    Map hardware to a sensible Ollama model. Conservative on purpose -- better a
    model that runs smoothly than one that swaps to disk and crawls.
    """
    # Usable memory for the model: VRAM if GPU, else a fraction of system RAM
    if profile.has_nvidia:
        budget = profile.vram_gb
    elif profile.has_apple_silicon:
        budget = profile.vram_gb * 0.7   # leave headroom for the OS
    else:
        budget = profile.ram_gb * 0.5    # CPU inference uses system RAM

    if budget >= 14:
        return ModelRecommendation(
            tier="large", model_tag="llama3.1:8b",
            reason=f"~{budget:.0f}GB available -- comfortably runs an 8B model, "
                   "or step up to a 13B/14B if you want.",
            approx_ram_needed_gb=10,
            alternatives=["qwen2.5:14b", "mistral-nemo:12b", "llama3.1:8b"])
    if budget >= 8:
        return ModelRecommendation(
            tier="medium", model_tag="llama3.1:8b",
            reason=f"~{budget:.0f}GB available -- an 8B model runs well.",
            approx_ram_needed_gb=8,
            alternatives=["qwen2.5:7b", "mistral:7b", "gemma2:9b"])
    if budget >= 5:
        return ModelRecommendation(
            tier="small", model_tag="qwen2.5:3b",
            reason=f"~{budget:.0f}GB available -- a 3B model is the sweet spot.",
            approx_ram_needed_gb=4,
            alternatives=["llama3.2:3b", "phi3.5:3.8b"])
    return ModelRecommendation(
        tier="tiny", model_tag="llama3.2:1b",
        reason=f"~{budget:.0f}GB available -- a 1B model keeps things responsive. "
               "Capable for chat and simple tasks, limited for complex reasoning.",
        approx_ram_needed_gb=2,
        alternatives=["qwen2.5:1.5b", "gemma2:2b"])


def full_report() -> dict:
    profile = detect_hardware()
    rec = recommend_model(profile)
    return {
        "hardware": {
            "os": profile.os_name,
            "cpu_cores": profile.cpu_cores,
            "ram_gb": profile.ram_gb,
            "gpu": profile.gpu_name or "none",
            "vram_gb": profile.vram_gb,
            "notes": profile.notes,
        },
        "recommended_model": {
            "tier": rec.tier,
            "ollama_tag": rec.model_tag,
            "reason": rec.reason,
            "approx_ram_needed_gb": rec.approx_ram_needed_gb,
            "alternatives": rec.alternatives,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(full_report(), indent=2))
