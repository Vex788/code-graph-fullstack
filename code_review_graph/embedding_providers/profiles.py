"""Embedding profiles: which backend and model serve a profile on this machine.

Resolution is a pure function of the settings, the platform and which
optional backends are importable, so every branch is testable without a model.

=========  ===========================================  ======  ====================
profile    backend (first importable wins)              dim     extra
=========  ===========================================  ======  ====================
fast       model2vec ``minishlab/potion-code-16M-v2``   256     embeddings-fast
balanced   MLX ``mlx-embeddings`` (darwin-arm64, only   256     embeddings-mlx
           after the ONNX parity check passed), else            embeddings-onnx
           fastembed ONNX ``google/embeddinggemma-300m``
accurate   mlx-lm ``Qwen3-Embedding-0.6B-4bit-DWQ``     512     embeddings-mlx
           (darwin-arm64 only; elsewhere -> balanced)
legacy     sentence-transformers ``BAAI/bge-small-en-v1.5``  384  embeddings
=========  ===========================================  ======  ====================

Provider ids are ``backend:model:quant:dN``, so switching profile, model,
quantization or dimension re-embeds instead of mixing vector spaces. The
``legacy`` profile keeps the historic ``local:<model>`` id so existing
vectors stay valid.
"""

from __future__ import annotations

import importlib.util
import platform as _platform
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from ..repo_settings import CLOUD_PROVIDER_PROFILES, EmbeddingSettings

MAC_ARM = "darwin-arm64"
PARITY_MIN_COSINE = 0.99


@dataclass(frozen=True)
class BackendSpec:
    backend: str
    module: str  # import probe; the backend is usable when it is importable
    model: str
    quant: str
    native_dim: int
    default_dim: int
    # The leading components carry the signal (MRL or PCA-ordered), so a
    # shorter vector is a prefix plus renormalization.
    truncatable: bool
    extra: str
    platforms: tuple[str, ...] = ()

    def supports(self, platform_key: str) -> bool:
        return not self.platforms or platform_key in self.platforms


FAST = BackendSpec(
    "model2vec", "model2vec", "minishlab/potion-code-16M-v2", "f32",
    256, 256, True, "embeddings-fast",
)
BALANCED_MLX = BackendSpec(
    "mlx", "mlx_embeddings", "mlx-community/embeddinggemma-300m-4bit", "q4",
    768, 256, True, "embeddings-mlx", (MAC_ARM,),
)
BALANCED_ONNX = BackendSpec(
    "onnx", "fastembed", "google/embeddinggemma-300m", "fp32",
    768, 256, True, "embeddings-onnx",
)
ACCURATE_MLX = BackendSpec(
    "mlx-lm", "mlx_lm", "mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ", "q4-dwq",
    1024, 512, True, "embeddings-mlx", (MAC_ARM,),
)
LEGACY = BackendSpec(
    "sentence-transformers", "sentence_transformers", "BAAI/bge-small-en-v1.5", "f32",
    384, 384, False, "embeddings",
)

PROFILE_BACKENDS: dict[str, tuple[BackendSpec, ...]] = {
    "fast": (FAST,),
    "balanced": (BALANCED_MLX, BALANCED_ONNX),
    "accurate": (ACCURATE_MLX,),
    "legacy": (LEGACY,),
}


def platform_key() -> str:
    machine = _platform.machine().lower()
    if machine == "aarch64":
        machine = "arm64"
    return f"{sys.platform}-{machine}"


def module_available(name: str) -> bool:
    """True when *name* is importable, without importing it."""
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@dataclass(frozen=True)
class Resolution:
    requested: str  # the profile the settings asked for
    profile: str  # the profile that serves it (accurate may fall back to balanced)
    spec: Optional[BackendSpec]  # None for cloud providers and when unavailable
    model: str
    dim: int
    available: bool
    warning: Optional[str] = None

    @property
    def cloud(self) -> bool:
        return self.profile in CLOUD_PROVIDER_PROFILES

    @property
    def provider_id(self) -> str:
        """Stored ``embeddings.provider`` identity ('' until a cloud provider is built)."""
        if self.spec is None:
            return ""
        if self.spec is LEGACY or self.spec.backend == "sentence-transformers":
            return f"local:{self.model}"
        return f"{self.spec.backend}:{self.model}:{self.spec.quant}:d{self.dim}"

    @property
    def backend(self) -> str:
        if self.cloud:
            return self.profile
        return self.spec.backend if self.spec else "none"


def _dim_for(spec: BackendSpec, requested: Optional[int]) -> tuple[int, Optional[str]]:
    if requested is None or requested == spec.default_dim:
        return spec.default_dim, None
    if requested > spec.native_dim or (not spec.truncatable and requested != spec.native_dim):
        return spec.default_dim, (
            f"dim={requested} is not supported by {spec.model} "
            f"(max {spec.native_dim}{'' if spec.truncatable else ', fixed'}); "
            f"using {spec.default_dim}"
        )
    return requested, None


def _install_hint(profile: str, platform: str) -> str:
    extras = {
        "fast": "embeddings-fast",
        "balanced": (
            "embeddings-mlx,embeddings-onnx" if platform == MAC_ARM else "embeddings-onnx"
        ),
        "accurate": "embeddings-mlx",
        "legacy": "embeddings",
    }[profile]
    return f'pip install "code-graph-fullstack[{extras}]"'


def resolve_profile(
    settings: EmbeddingSettings,
    *,
    platform: Optional[str] = None,
    has_module: Optional[Callable[[str], bool]] = None,
    mlx_parity: Optional[bool] = None,
) -> Resolution:
    """Pick the backend that serves ``settings.profile`` here.

    Args:
        platform: ``sys.platform-machine`` key; defaults to this machine.
        has_module: import probe; defaults to :func:`module_available`.
        mlx_parity: result of the MLX-vs-ONNX parity check recorded at
            ``embeddings enable`` (None: never checked). MLX serves
            ``balanced`` only when it is True.
    """
    platform = platform or platform_key()
    probe = has_module or module_available
    requested = settings.profile
    if requested in CLOUD_PROVIDER_PROFILES:
        return Resolution(requested, requested, None, settings.model or "", settings.dim or 0,
                          True)

    profile = requested
    notes: list[str] = []
    if requested == "accurate" and not ACCURATE_MLX.supports(platform):
        notes.append(
            f"profile 'accurate' needs Apple Silicon (MLX); this is {platform}, "
            "using 'balanced'"
        )
        profile = "balanced"

    chosen: Optional[BackendSpec] = None
    for spec in PROFILE_BACKENDS[profile]:
        if not spec.supports(platform) or not probe(spec.module):
            continue
        if spec is BALANCED_MLX and mlx_parity is not True:
            if mlx_parity is False:
                notes.append("MLX failed the parity check against ONNX; using ONNX")
            elif not probe(BALANCED_ONNX.module):
                notes.append(
                    "MLX is used for 'balanced' only after its parity check against the "
                    "ONNX reference; install embeddings-onnx and run "
                    "`code-review-graph embeddings enable`"
                )
            continue
        chosen = spec
        break

    if chosen is None:
        notes.append(f"no backend installed for profile '{profile}': "
                     f"{_install_hint(profile, platform)}")
        first = PROFILE_BACKENDS[profile][-1]
        return Resolution(requested, profile, None, settings.model or first.model,
                          settings.dim or first.default_dim, False, "; ".join(notes))

    dim, dim_note = _dim_for(chosen, settings.dim)
    if dim_note:
        notes.append(dim_note)
    # A model override names a model of the requested profile, not the fallback's.
    model = (settings.model if profile == requested else None) or chosen.model
    return Resolution(requested, profile, chosen, model, dim, True,
                      "; ".join(notes) or None)


__all__ = [
    "ACCURATE_MLX",
    "BALANCED_MLX",
    "BALANCED_ONNX",
    "BackendSpec",
    "FAST",
    "LEGACY",
    "MAC_ARM",
    "PARITY_MIN_COSINE",
    "PROFILE_BACKENDS",
    "Resolution",
    "module_available",
    "platform_key",
    "resolve_profile",
]
