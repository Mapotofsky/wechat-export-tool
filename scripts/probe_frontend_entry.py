"""Open an existing CMD entry with generated data, leaving installed data untouched."""
import argparse
import json
import os
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--entry', type=Path, required=True)
parser.add_argument('--evidence', type=Path, required=True)
args = parser.parse_args()
repo = Path(__file__).resolve().parent.parent
evidence = args.evidence.resolve()
evidence.mkdir(parents=True, exist_ok=True)
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(evidence / 'state')
sys.path[:0] = [str(repo), str(repo / 'tests')]
from u2_synthetic import prepare
manager, source = prepare(evidence)
manager.executor.shutdown()
entry = args.entry.resolve()
os.startfile(str(entry))
(evidence / 'launch.json').write_text(json.dumps({'entry':str(entry), 'state':str(evidence / 'state'),
    'method':'ShellExecute, same association as double-click'}, ensure_ascii=False, indent=2), encoding='utf-8')
print(str(evidence / 'launch.json'))
