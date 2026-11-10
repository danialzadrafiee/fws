import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import pipeline as p


class ClassificationTests(unittest.TestCase):
    def test_codex_receives_model_effort_and_heic_image(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "cover.heic"
            Image.new("RGB", (16, 16), "white").save(image, format="JPEG")
            payload = [{"username": "example", "thumbnails": [str(image)]}]
            observed = {}

            def fake_run(cmd, **kwargs):
                observed["cmd"] = cmd
                observed["prompt"] = kwargs["input"]
                attachment = Path(cmd[cmd.index("--image") + 1])
                with Image.open(attachment) as opened:
                    observed["format"] = opened.format
                schema = json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text(encoding="utf-8"))
                self.assertIn("results", schema["properties"])
                Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
                    json.dumps({"results": [{"username": "example"}]}), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            cfg = {"codex_cmd": "codex", "codex_model": "gpt-6-luna",
                   "codex_reasoning_effort": "low"}
            with patch.object(p, "CFG", cfg), patch.object(p.shutil, "which", return_value="codex.CMD"), patch.object(p.subprocess, "run", side_effect=fake_run):
                result = p.call_codex(payload)
            self.assertEqual(result[0]["username"], "example")
            self.assertEqual(observed["format"], "JPEG")
            self.assertIn("gpt-6-luna", observed["cmd"])
            self.assertIn("model_reasoning_effort=low", observed["cmd"])
            self.assertIn('"username": "example"', observed["prompt"])

    def test_provider_switch_updates_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"classifier_provider": "codex", "classify_batch_size": 25}
            with patch.object(p, "DB", Path(directory) / "test.db"), patch.object(p, "CFG", cfg):
                con = p.db()
                try:
                    p.add_candidate(con, "example", "1", "test")
                    con.execute("UPDATE accounts SET status='enriched', thumbs='[]' WHERE username='example'")
                    con.commit()
                    answer = [{"username": "example", "is_fal_page": True, "is_daily": True,
                               "is_text_based": True, "fal_types": ["تاروت"],
                               "confidence": 0.9, "reason": "متن فال دارد"}]
                    with patch.object(p, "call_codex", return_value=answer) as codex, patch.object(p, "call_haiku") as claude, patch.object(p, "log"), patch.object(p, "summary"):
                        p.classify(con, SimpleNamespace(limit=10))
                    codex.assert_called_once()
                    claude.assert_not_called()
                    self.assertEqual(con.execute("SELECT status FROM accounts WHERE username='example'").fetchone()[0], "classified")
                finally:
                    con.close()


if __name__ == "__main__":
    unittest.main()
