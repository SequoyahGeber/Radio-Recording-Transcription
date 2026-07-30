import json
import os
import tempfile
import unittest
from unittest import mock

from scripts import service_control
from scripts import supervisor


class ServiceControlTests(unittest.TestCase):
    def test_status_reports_worker_state_from_supervisor(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = os.path.join(directory, "supervisor.pid")
            status_path = os.path.join(directory, "service-status.json")
            with open(pid_path, "w", encoding="utf-8") as handle:
                handle.write("4242")
            with open(status_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "supervisor_pid": 4242,
                        "processes": {"worker": {"pid": 4343, "running": True}},
                    },
                    handle,
                )

            with (
                mock.patch.object(service_control, "PID_PATH", pid_path),
                mock.patch.object(service_control, "STATUS_PATH", status_path),
                mock.patch.object(service_control, "process_is_running", return_value=True),
                mock.patch.object(
                    service_control,
                    "load_settings",
                    return_value={"transcription_enabled": True},
                ),
            ):
                result = service_control.status()

        self.assertTrue(result["running"])
        self.assertTrue(result["transcription_enabled"])
        self.assertTrue(result["transcription_running"])

    def test_disabling_transcription_persists_without_stopping_services(self):
        running_status = {
            "running": True,
            "transcription_enabled": False,
            "transcription_running": False,
        }
        with (
            mock.patch.object(service_control, "save_settings") as save_settings,
            mock.patch.object(service_control, "status", return_value=running_status),
            mock.patch.object(service_control, "stop") as stop_services,
        ):
            result = service_control.set_transcription(False)

        save_settings.assert_called_once_with({"transcription_enabled": False})
        stop_services.assert_not_called()
        self.assertTrue(result["running"])
        self.assertFalse(result["transcription_running"])

    def test_enabling_transcription_downloads_medium_before_persisting(self):
        running_status = {
            "running": True,
            "transcription_enabled": True,
            "transcription_running": True,
        }
        calls = []
        with (
            mock.patch.object(service_control.platform, "system", return_value="Darwin"),
            mock.patch.object(service_control.platform, "machine", return_value="arm64"),
            mock.patch.object(
                service_control,
                "ensure_model",
                side_effect=lambda *args: calls.append("model"),
            ),
            mock.patch.object(
                service_control,
                "save_settings",
                side_effect=lambda *args: calls.append("settings"),
            ),
            mock.patch.object(service_control, "status", return_value=running_status),
        ):
            result = service_control.set_transcription(True)

        self.assertEqual(calls, ["model", "settings"])
        self.assertTrue(result["transcription_running"])

    def test_failed_model_download_does_not_enable_transcription(self):
        with (
            mock.patch.object(service_control.platform, "system", return_value="Darwin"),
            mock.patch.object(service_control.platform, "machine", return_value="arm64"),
            mock.patch.object(
                service_control,
                "ensure_model",
                side_effect=RuntimeError("download failed"),
            ),
            mock.patch.object(service_control, "save_settings") as save_settings,
        ):
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                service_control.set_transcription(True)

        save_settings.assert_not_called()


class ManagedProcessTests(unittest.TestCase):
    def test_disabled_process_is_stopped_and_can_start_cleanly_later(self):
        process = supervisor.ManagedProcess("worker", ["worker"], {})
        fake_process = mock.Mock()
        fake_process.poll.return_value = None
        process.process = fake_process
        process.failure_count = 3
        process.next_start = 100

        process.reconcile(False)

        fake_process.terminate.assert_called_once()
        self.assertIsNone(process.process)
        self.assertEqual(process.failure_count, 0)
        self.assertEqual(process.next_start, 0)


if __name__ == "__main__":
    unittest.main()
