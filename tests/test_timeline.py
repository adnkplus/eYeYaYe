from __future__ import annotations
import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from timeline import archive_day_for, day_window, output_position_for_wall_time, choose_record, next_record
from naming import filename, parse_path


class TimelineTests(unittest.TestCase):
    def test_archive_day_boundary_defaults_to_0500(self):
        tz = ZoneInfo("Europe/Paris")
        before = datetime(2026, 11, 2, 4, 59, tzinfo=tz)
        boundary = datetime(2026, 11, 2, 5, 0, tzinfo=tz)
        after = datetime(2026, 11, 2, 17, 0, tzinfo=tz)
        self.assertEqual(archive_day_for(before), date(2026, 11, 1))
        self.assertEqual(archive_day_for(boundary), date(2026, 11, 2))
        self.assertEqual(archive_day_for(after), date(2026, 11, 2))

    def test_archive_day_window_tracks_dst_elapsed_duration(self):
        tz = ZoneInfo("Europe/Paris")
        spring_start, spring_end = day_window("2026-03-28", "05:00", tz)
        autumn_start, autumn_end = day_window("2026-10-24", "05:00", tz)
        self.assertEqual(spring_end.timestamp() - spring_start.timestamp(), 23 * 3600)
        self.assertEqual(autumn_end.timestamp() - autumn_start.timestamp(), 25 * 3600)
        # 05:00 is a local boundary, so spring/autumn days have 23/25 elapsed hours.

    def test_timeline_maps_accelerated_time_lapse_to_source_time(self):
        start = datetime(2026, 11, 2, 5, 0, tzinfo=ZoneInfo("Europe/Paris"))
        end = datetime(2026, 11, 2, 5, 0, 10, tzinfo=ZoneInfo("Europe/Paris"))
        record = {
            "camera_id": "cam1", "type_code": "TLI", "start_ts": start.timestamp(),
            "duration_seconds": 5,
            "time_map": [{
                "source_start": start.isoformat(), "source_end": end.isoformat(),
                "output_start_seconds": 0, "output_end_seconds": 5, "speed": 2,
            }],
        }
        self.assertEqual(output_position_for_wall_time(record, start.timestamp() + 4), 2.0)

    def test_timeline_prefers_raw_then_merged_then_time_lapse(self):
        start = datetime(2026, 11, 2, 5, 0, tzinfo=ZoneInfo("Europe/Paris")).timestamp()
        records = [
            {"camera_id": "cam1", "type_code": "MER", "start_ts": start,
             "duration_seconds": 60, "path": "merged.mp4"},
            {"camera_id": "cam1", "type_code": "TLI", "start_ts": start,
             "duration_seconds": 60, "path": "tl.mp4"},
            {"camera_id": "cam1", "type_code": "RAW", "start_ts": start,
             "duration_seconds": 60, "path": "raw.ts"},
        ]
        selected = choose_record(records, "cam1", start + 10)
        self.assertEqual(selected[0]["path"], "raw.ts")
        records = [r for r in records if r["type_code"] != "RAW"]
        selected = choose_record(records, "cam1", start + 10)
        self.assertEqual(selected[0]["path"], "merged.mp4")

    def test_next_record_finds_the_next_available_camera_interval(self):
        start = datetime(2026, 11, 2, 5, 0, tzinfo=ZoneInfo("Europe/Paris")).timestamp()
        records = [{
            "camera_id": "cam2", "type_code": "RAW", "start_ts": start + 120,
            "duration_seconds": 60,
        }]
        result = next_record(records, "cam2", start)
        self.assertIsNotNone(result)
        self.assertEqual(result[2], start + 120)

    def test_raw_year_timezone_path_and_new_time_first_filename(self):
        early = parse_path(Path("2026/+0200/C1_023000_2510_RAW.ts"))
        late = parse_path(Path("2026/+0100/C1_023000_2510_RAW.ts"))
        self.assertIsNotNone(early)
        self.assertIsNotNone(late)
        self.assertEqual(early["format"], "raw-yearless")
        self.assertNotEqual(early["started"].timestamp(), late["started"].timestamp())
        when = datetime(2026, 11, 2, 14, 32, tzinfo=ZoneInfo("Europe/Paris"))
        self.assertEqual(filename("C1", when, "RAW", "ts"), "143200_02112026_RAW_C1.ts")
        self.assertEqual(filename("C1", when, "MER", "mp4", part=1),
                         "143200_02112026_MER_C1_P01.mp4")

    def test_gui_exposes_common_24_hour_timeline(self):
        gui = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
        self.assertIn("self.timeline_canvas", gui)
        self.assertIn("def _timeline_seek_all", gui)
        self.assertIn("def _timeline_play_all", gui)
        self.assertIn("def _timeline_continue_after_eof", gui)
        self.assertIn("PLAY ALL", gui)
        self.assertIn("PAUSE ALL", gui)

    def test_daily_timelapse_scheduler_only_builds_enabled_modes(self):
        control = {}
        import ast
        tree = ast.parse((ROOT / "USER_CONTROL.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                try:
                    value = ast.literal_eval(node.value)
                except Exception:
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        control[target.id] = value
        self.assertTrue(control["TIMELAPSE_AUTO_BUILD"])
        self.assertFalse(control["ENABLE_REGULAR_TIMELAPSE"])
        self.assertTrue(control["ENABLE_INTELLIGENT_TIMELAPSE"])
        core = (ROOT / "app" / "core.py").read_text(encoding="utf-8")
        self.assertIn("ctl.get('TIMELAPSE_AUTO_BUILD', False)", core)
        self.assertIn("'--mode', mode", core)
        self.assertIn("MER manifest exists", core)


if __name__ == "__main__":
    unittest.main()
