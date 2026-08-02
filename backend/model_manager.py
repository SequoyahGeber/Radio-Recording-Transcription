import os
import time

from huggingface_hub import snapshot_download, try_to_load_from_cache
from tqdm.auto import tqdm


PRIMARY_MLX_MODEL = os.environ.get(
    "RADIO_MLX_MODEL",
    "mlx-community/whisper-medium-mlx",
)
RETRY_MLX_MODEL = os.environ.get(
    "RADIO_RETRY_MLX_MODEL",
    "mlx-community/whisper-large-v3-mlx",
)


class ModelDownloadCancelled(RuntimeError):
    pass


def _progress_class(progress_callback=None, control_callback=None):
    class ModelProgress(tqdm):
        def update(self, amount=1):
            while control_callback and control_callback() == "pause":
                time.sleep(0.25)
            if control_callback and control_callback() == "cancel":
                raise ModelDownloadCancelled("Model download cancelled")
            changed = super().update(amount)
            if progress_callback:
                progress_callback(int(self.n), int(self.total or 0), str(self.desc or ""))
            return changed

    return ModelProgress


def model_cache_dir(model_dir):
    return os.path.join(os.path.abspath(model_dir), "hf-mlx", "hub")


def model_is_cached(repo_id, model_dir):
    cache_dir = model_cache_dir(model_dir)
    config_path = try_to_load_from_cache(
        repo_id,
        "config.json",
        cache_dir=cache_dir,
    )
    weights_path = try_to_load_from_cache(
        repo_id,
        "weights.npz",
        cache_dir=cache_dir,
    )
    if not isinstance(weights_path, str):
        weights_path = try_to_load_from_cache(
            repo_id,
            "weights.safetensors",
            cache_dir=cache_dir,
        )
    return isinstance(config_path, str) and isinstance(weights_path, str)


def estimate_model_size(repo_id, model_dir):
    files = snapshot_download(
        repo_id=repo_id,
        cache_dir=model_cache_dir(model_dir),
        allow_patterns=["config.json", "weights.npz", "weights.safetensors"],
        dry_run=True,
    )
    total = sum(int(getattr(item, "file_size", 0) or 0) for item in files)
    return {"bytes": total, "files": len(files)}


def ensure_model(
    repo_id,
    model_dir,
    *,
    progress_callback=None,
    control_callback=None,
):
    os.makedirs(model_dir, exist_ok=True)
    if model_is_cached(repo_id, model_dir):
        return {
            "cached": True,
            "downloaded": False,
            "model": repo_id,
        }
    previous_progress_setting = os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS")
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    try:
        snapshot_download(
            repo_id=repo_id,
            cache_dir=model_cache_dir(model_dir),
            allow_patterns=[
                ".gitattributes",
                "README.md",
                "config.json",
                "weights.npz",
                "weights.safetensors",
            ],
            tqdm_class=_progress_class(progress_callback, control_callback),
        )
    finally:
        if previous_progress_setting is None:
            os.environ.pop("HF_HUB_DISABLE_PROGRESS_BARS", None)
        else:
            os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = previous_progress_setting
    if not model_is_cached(repo_id, model_dir):
        raise RuntimeError(f"The downloaded model is incomplete: {repo_id}")
    return {
        "cached": True,
        "downloaded": True,
        "model": repo_id,
    }
