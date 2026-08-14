"""Segment timing for the MLX transcription path.

Whisper timestamps each segment from the start of the audio it is handed. The
MLX path hands it one voice-activity chunk at a time, so every chunk's offset
has to be added back before the timings mean anything on the real recording.
Segment seeking in the detail drawer plays from these values.
"""

import atexit
import base64
import importlib
import os
import secrets
import tempfile
import unittest
from unittest import mock


TEST_ROOT = tempfile.TemporaryDirectory()
atexit.register(TEST_ROOT.cleanup)
for name, relative in (
    ("RADIO_DATA_DIR", "data"),
    ("RADIO_SOURCE_DIR", "source"),
    ("RADIO_AUDIO_DIR", "audio"),
    ("RADIO_SECURITY_DIR", "security"),
    ("RADIO_MODEL_DIR", "models"),
):
    os.environ.setdefault(name, os.path.join(TEST_ROOT.name, relative))
os.environ.setdefault(
    "RADIO_DB_PATH", os.path.join(os.environ["RADIO_DATA_DIR"], "worker-test.db")
)
# Keep the import on the MLX branch; the CPU branch would load a Whisper model.
os.environ["RADIO_TRANSCRIPTION_ENGINE"] = "mlx"

RATE = 16000


def _load_worker():
    from backend.security import load_security_config, save_security_config

    try:
        load_security_config()
    except RuntimeError:
        # Generated rather than written out, so no literal in this repository
        # looks like a credential to a secret scanner.
        save_security_config(
            {
                "users": [],
                "session_secret": base64.urlsafe_b64encode(
                    secrets.token_bytes(32)
                ).decode("ascii"),
                "internal_token": secrets.token_urlsafe(16),
            }
        )
    return importlib.import_module("backend.worker")


try:
    import mlx_whisper  # noqa: F401

    MLX_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the host machine
    MLX_AVAILABLE = False


@unittest.skipUnless(MLX_AVAILABLE, "MLX transcription engine is not installed")
class MlxSegmentTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = _load_worker()

    def test_importing_the_worker_does_not_start_the_service(self):
        # The module used to run the watcher and a blocking loop at import time,
        # which made every function in it untestable.
        self.assertTrue(hasattr(self.worker, "run_service"))
        self.assertTrue(callable(self.worker.run_service))

    def test_chunks_carry_their_offset_into_the_recording(self):
        import numpy as np

        audio = np.zeros(12 * RATE, dtype=np.float32)
        with mock.patch.object(
            self.worker,
            "get_speech_timestamps",
            return_value=[
                {"start": 0 * RATE, "end": 1 * RATE},
                {"start": 10 * RATE, "end": 11 * RATE},
            ],
        ):
            chunks = self.worker.voice_activity_segments(audio)

        self.assertEqual(len(chunks), 2)
        self.assertEqual([round(offset, 3) for _, offset in chunks], [0.0, 10.0])

    def test_segment_times_are_relative_to_the_whole_recording(self):
        import numpy as np

        audio = np.zeros(12 * RATE, dtype=np.float32)

        class SingleSegmentModel:
            """Reports one segment spanning whatever slice it was handed."""

            @staticmethod
            def transcribe(speech_audio, **kwargs):
                return {
                    "text": "copy that",
                    "segments": [
                        {
                            "start": 0.0,
                            "end": len(speech_audio) / RATE,
                            "text": "copy that",
                            "avg_logprob": -0.2,
                        }
                    ],
                }

        with mock.patch.object(
            self.worker,
            "get_speech_timestamps",
            return_value=[
                {"start": 0 * RATE, "end": 1 * RATE},
                {"start": 10 * RATE, "end": 11 * RATE},
            ],
        ), mock.patch.object(
            self.worker, "decode_audio", return_value=audio
        ), mock.patch.object(
            self.worker, "mlx_whisper", SingleSegmentModel
        ), mock.patch.object(
            self.worker, "active_engine", "mlx"
        ):
            _, duration, metrics = self.worker.transcribe_audio("ignored.wav")

        self.assertAlmostEqual(duration, 12.0, places=2)
        segments = metrics["_segments"]
        self.assertEqual(len(segments), 2)
        # The second burst must land at ten seconds, not back at zero.
        self.assertAlmostEqual(segments[0]["start"], 0.0, places=2)
        self.assertAlmostEqual(segments[1]["start"], 10.0, places=2)
        self.assertAlmostEqual(segments[1]["end"], 11.0, places=2)
        self.assertTrue(
            all(segment["end"] <= duration + 0.01 for segment in segments),
            f"segments must stay inside the recording: {segments}",
        )


if __name__ == "__main__":
    unittest.main()
