import csv
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import pipeline as p


class ManualReviewTests(unittest.TestCase):
    def test_selection_in_gui_controls_final_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thumbs = root / "thumbs"
            thumbs.mkdir()
            files = [thumbs / f"cover_{i}.jpg" for i in range(2)]
            for path in files:
                Image.new("RGB", (80, 80), "white").save(path)
            with patch.object(p, "DB", root / "fal.db"), patch.object(p, "HERE", root), patch.object(p, "THUMBS", thumbs):
                con = p.db()
                try:
                    for username in ("accepted", "rejected"):
                        p.add_candidate(con, username, None, "test")
                    con.execute("""UPDATE accounts SET status='classified', thumbs=?,
                        is_fal_page=0, is_daily=0, is_text_based=0 WHERE username='accepted'""",
                        (json.dumps([str(path) for path in files]),))
                    con.execute("""UPDATE accounts SET status='classified', thumbs=?,
                        is_fal_page=1, is_daily=1, is_text_based=1, manual_decision=0
                        WHERE username='rejected'""",
                        (json.dumps([str(path) for path in files]),))
                    con.commit()

                    App = runpy.run_path("gui.pyw", run_name="gui_test")["App"]
                    App.refresh_status.__globals__["DB"] = root / "fal.db"
                    app = App()
                    try:
                        app.withdraw()
                        app.manual_filter.set("همه")
                        app.load_manual()
                        app.update_idletasks()
                        self.assertEqual(len(app.manual_images), 4)
                        app.save_manual("accepted", 1, SimpleNamespace(set=lambda value: None),
                                        SimpleNamespace(set=lambda value: None))
                    finally:
                        app.destroy()

                    self.assertEqual(con.execute("SELECT manual_decision FROM accounts WHERE username='accepted'").fetchone()[0], 1)
                    with patch.object(p, "log"), patch.object(p, "summary"):
                        p.export(con, SimpleNamespace())
                    with (root / "fal_pages.csv").open(encoding="utf-8-sig", newline="") as stream:
                        selected = list(csv.DictReader(stream))
                    with (root / "ai_candidates.csv").open(encoding="utf-8-sig", newline="") as stream:
                        suggested = list(csv.DictReader(stream))
                    self.assertEqual([row["username"] for row in selected], ["accepted"])
                    self.assertEqual([row["username"] for row in suggested], ["rejected"])
                finally:
                    con.close()


if __name__ == "__main__":
    unittest.main()
