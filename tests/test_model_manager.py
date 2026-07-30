import os
import tempfile
import unittest
from unittest import mock

from backend import model_manager


class ModelManagerTests(unittest.TestCase):
    def test_cached_model_does_not_download_again(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(model_manager, "model_is_cached", return_value=True),
            mock.patch.object(model_manager, "snapshot_download") as download,
        ):
            result = model_manager.ensure_model(
                model_manager.PRIMARY_MLX_MODEL,
                directory,
            )

        download.assert_not_called()
        self.assertTrue(result["cached"])
        self.assertFalse(result["downloaded"])

    def test_model_download_uses_application_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    model_manager,
                    "model_is_cached",
                    side_effect=[False, True],
                ),
                mock.patch.object(model_manager, "snapshot_download") as download,
            ):
                result = model_manager.ensure_model(
                    model_manager.PRIMARY_MLX_MODEL,
                    directory,
                )

        self.assertTrue(result["downloaded"])
        self.assertEqual(
            download.call_args.kwargs["cache_dir"],
            os.path.join(directory, "hf-mlx", "hub"),
        )


if __name__ == "__main__":
    unittest.main()
