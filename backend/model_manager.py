import os

from huggingface_hub import snapshot_download, try_to_load_from_cache


PRIMARY_MLX_MODEL = os.environ.get(
    "RADIO_MLX_MODEL",
    "mlx-community/whisper-medium-mlx",
)
RETRY_MLX_MODEL = os.environ.get(
    "RADIO_RETRY_MLX_MODEL",
    "mlx-community/whisper-large-v3-mlx",
)


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


def ensure_model(repo_id, model_dir):
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
