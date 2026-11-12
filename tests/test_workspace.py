from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from workspace import DEFAULT_WORKSPACE, load_workspace, normalize_workspace, save_workspace

class WorkspaceTests(unittest.TestCase):
    def test_manual_grids_and_window_geometry_survive_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config'/'workspace.json'
            state=normalize_workspace({
                'window_geometry':'1680x1050+30+40','page':'Live & Playback',
                'show_live':False,'show_playback':True,'split_orientation':'stacked','sash_position':612,
                'live_grid':{'columns':4,'rows':2},'playback_grid':{'columns':2,'rows':3},
            })
            save_workspace(path,state)
            loaded=load_workspace(path)
            self.assertEqual(loaded['window_geometry'],'1680x1050+30+40')
            self.assertEqual(loaded['live_grid'],{'columns':4,'rows':2})
            self.assertEqual(loaded['playback_grid'],{'columns':2,'rows':3})
            self.assertFalse(loaded['show_live'])
            self.assertTrue(loaded['show_playback'])
            self.assertEqual(loaded['split_orientation'],'stacked')
            self.assertEqual(loaded['sash_position'],612)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_invalid_grid_values_are_bounded_and_bad_workspace_falls_back(self):
        clean=normalize_workspace({'live_grid':{'columns':999,'rows':999},'page':'not-a-page'})
        self.assertLessEqual(clean['live_grid']['columns']*clean['live_grid']['rows'],64)
        self.assertEqual(clean['page'],DEFAULT_WORKSPACE['page'])
        self.assertEqual(load_workspace(Path('/not/a/real/workspace.json'))['live_grid'],DEFAULT_WORKSPACE['live_grid'])

if __name__=='__main__':
    unittest.main()
