"""Offline negative acceptance check; exits 1 while D16 bypass remains."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_p9_05 import modern
from storycg.planning import validate_storyboard
from storycg.assembly import assemble_prompt

board = modern()
board['shots'][0]['scene']['tags'].append('sitting')
try:
    validate_storyboard(board, 4)
    prompt = assemble_prompt({'identity_block': '1girl'}, board['shots'][0], props=board['props'])
except ValueError as exc:
    print('PASS: cross-block second pose refused:', exc)
else:
    print('FAIL: D16 cross-block bypass accepted:', prompt['positive_tags'])
    raise SystemExit(1)
