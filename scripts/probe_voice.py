"""Verify bundled CPU model loading, FFmpeg and Tencent SILK using synthetic speech."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--app', type=Path, required=True)
parser.add_argument('--audio', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.app.resolve()))
import local_asr
import pilk
import torch

args.output.mkdir(parents=True, exist_ok=True)
info = local_asr.capabilities()
pcm = args.output / 'speech.pcm'
silk = args.output / 'speech.silk'
subprocess.run([info['ffmpeg'], '-nostdin', '-y', '-i', str(args.audio),
                '-f', 's16le', '-ac', '1', '-ar', '24000', str(pcm)], check=True,
               capture_output=True)
pilk.encode(str(pcm), str(silk), pcm_rate=24000, tencent=True)
started = time.monotonic()
result = {'capabilities': info, 'python': sys.executable, 'torch': torch.__version__,
          'cuda_compiled': torch.version.cuda, 'silk_header': silk.read_bytes()[:10].hex(),
          'text': local_asr.recognize(silk, args.app.resolve().parent / 'models' / 'small.pt'),
          'seconds': round(time.monotonic() - started, 2)}
(args.output / 'voice-probe.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
