"""Update notice tests. No network: urllib.request.urlopen is always patched."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kanbot import update


def pypi(version):
    return patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps({"info": {"version": version}}).encode()))


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"KANBOT_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("KANBOT_NO_UPDATE_CHECK", None)
        version = patch.object(update, "__version__", "0.9.9")
        version.start()
        self.addCleanup(version.stop)
        self.cache = Path(self.tmp.name) / "update-check.json"

    def test_newer_gives_the_notice(self):
        with pypi("1.2.3") as urlopen:
            self.assertEqual(update.update_notice(), "Kanbot 1.2.3 is out (you have 0.9.9). "
                             "Update: uv tool upgrade kanbot  (or: pipx upgrade kanbot, or: uvx kanbot@latest)")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://pypi.org/pypi/kanbot/json")
        self.assertEqual(request.get_header("User-agent"), "kanbot/0.9.9")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 2)

    def test_same_or_older_gives_nothing(self):
        for version in ("0.9.9", "0.9.8", "0.9.9+local", "0.9.9rc1", "0.9"):
            self.cache.unlink(missing_ok=True)
            with pypi(version):
                self.assertEqual(update.update_notice(), "", version)

    def test_compares_numbers_not_text(self):
        with pypi("0.10.0"):
            self.assertIn("Kanbot 0.10.0 is out", update.update_notice())
        self.assertTrue(update.newer("0.10.0"))
        self.assertFalse(update.newer("0.9.9"))
        self.assertFalse(update.newer(None))
        self.assertFalse(update.newer("not a version"))

    def test_young_cache_makes_no_request(self):
        self.cache.write_text(json.dumps({"checked": time.time() - 3600, "latest": "1.0.0"}))
        with pypi("2.0.0") as urlopen:
            self.assertEqual(update.latest_version(), "1.0.0")
        urlopen.assert_not_called()

    def test_old_cache_asks_again(self):
        self.cache.write_text(json.dumps({"checked": time.time() - 90000, "latest": "1.0.0"}))
        with pypi("2.0.0") as urlopen:
            self.assertEqual(update.latest_version(), "2.0.0")
        urlopen.assert_called_once()
        self.assertEqual(json.loads(self.cache.read_text())["latest"], "2.0.0")

    def test_failed_request_returns_the_cache_and_renews_its_time(self):
        self.cache.write_text(json.dumps({"checked": 1, "latest": "1.0.0"}))
        with patch("urllib.request.urlopen", side_effect=OSError("offline")) as urlopen:
            self.assertEqual(update.latest_version(), "1.0.0")
            self.assertEqual(update.latest_version(), "1.0.0")
        urlopen.assert_called_once()  # the second call reads the renewed cache
        saved = json.loads(self.cache.read_text())
        self.assertEqual(saved["latest"], "1.0.0")
        self.assertGreater(saved["checked"], time.time() - 60)

    def test_failed_request_without_a_cache_returns_none_once_a_day(self):
        with patch("urllib.request.urlopen", side_effect=RuntimeError("anything")) as urlopen:
            self.assertIsNone(update.latest_version())
            self.assertEqual(update.update_notice(), "")
        urlopen.assert_called_once()

    def test_opt_out_makes_no_request_and_no_file(self):
        with patch.dict(os.environ, {"KANBOT_NO_UPDATE_CHECK": "1"}), pypi("2.0.0") as urlopen:
            self.assertIsNone(update.latest_version())
            self.assertEqual(update.update_notice(), "")
        urlopen.assert_not_called()
        self.assertFalse(self.cache.exists())

    def test_corrupt_cache_is_no_cache(self):
        now = time.time()
        for junk in ("{not json", "[]", '{"latest": "1.0.0", "checked": "soon"}',
                     json.dumps({"latest": "1.0.0", "checked": now + 1e6}),  # dated in the future
                     json.dumps({"latest": 5, "checked": now - 60}),  # young, but not a version
                     json.dumps({"latest": "9.0\x1b[2J\nrm", "checked": now - 60})):
            self.cache.write_text(junk)
            with pypi("2.0.0") as urlopen:
                self.assertEqual(update.latest_version(), "2.0.0", junk)
            urlopen.assert_called_once()

    def test_bad_answer_keeps_the_cache(self):
        for version in (None, 5, ["1"], "9.0\x1b[2J\nrm -rf", "1" * 41):
            self.cache.write_text(json.dumps({"checked": 1, "latest": "1.0.0"}))
            with pypi(version):
                self.assertEqual(update.latest_version(), "1.0.0", version)
            self.assertEqual(json.loads(self.cache.read_text())["latest"], "1.0.0")

    def test_hung_request_gives_up_at_the_limit(self):
        self.cache.write_text(json.dumps({"checked": 1, "latest": "1.0.0"}))
        hang = threading.Event()
        self.addCleanup(hang.set)
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: hang.wait(30)), \
                patch.object(update, "LIMIT", 0.05):
            start = time.time()
            self.assertEqual(update.latest_version(), "1.0.0")
        self.assertLess(time.time() - start, 1)
        self.assertGreater(json.loads(self.cache.read_text())["checked"], start - 1)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [self.cache])  # no temp file left

    def test_unwritable_home_does_not_raise(self):
        with patch.object(update, "config_dir", side_effect=OSError("read-only")), pypi("2.0.0"):
            self.assertIsNone(update.latest_version())

    def test_swarm_status_json_carries_the_two_keys(self):
        from kanbot import swarm_cli
        for cmd, keys in (("status", True), ("pause", False)):
            out = io.StringIO()
            with patch.object(swarm_cli, "call", return_value={"running": True}), pypi("1.2.3"), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(swarm_cli.main(SimpleNamespace(swarm_command=cmd)), 0)
            result = json.loads(out.getvalue())
            self.assertEqual("latestVersion" in result, keys)
            if keys:
                self.assertEqual((result["latestVersion"], result["updateAvailable"]), ("1.2.3", True))

    def test_banner_shows_the_notice_only_in_a_terminal(self):
        from kanbot import cli

        class Terminal(io.StringIO):
            tty = True

            def isatty(self):
                return self.tty

        args = SimpleNamespace(db=None, host="127.0.0.1", port=1, log_level="warning")
        for notice, tty, shown in (("X", True, True), ("", True, False), ("X", False, False)):
            out = Terminal()
            out.tty = tty
            with patch("uvicorn.run"), patch("kanbot.server.app.create_app"), patch.object(update, "update_notice", return_value=notice) as asked, \
                    contextlib.redirect_stdout(out):
                self.assertEqual(cli.cmd_server(args), 0)
            lines = out.getvalue().splitlines()
            self.assertTrue(lines[0].startswith("KanBot server v"))
            self.assertEqual(lines[-1] == "  X", shown, (notice, tty))
            self.assertEqual(len(lines), 4 if shown else 3)
            self.assertEqual(asked.called, tty)  # no terminal, no request


if __name__ == "__main__":
    unittest.main()
