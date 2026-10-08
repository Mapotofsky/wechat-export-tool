"""Isolated local Whisper worker. No model downloads or cloud recognition."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import wave


def capabilities():
    models = sorted(str(p) for p in (Path.home() / '.cache' / 'whisper').glob('*.pt'))
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg and importlib.util.find_spec('imageio_ffmpeg'):
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    return {'whisper': bool(importlib.util.find_spec('whisper')), 'models': models,
            'silk': bool(importlib.util.find_spec('pilk')), 'ffmpeg': ffmpeg,
            'available': bool(importlib.util.find_spec('whisper')) and bool(models) and bool(ffmpeg)}


def recognize(source, model_path):
    import whisper
    import numpy as np
    import subprocess
    import tempfile
    info = capabilities()
    if not Path(model_path).is_file():
        raise RuntimeError('本地模型文件不存在；请在网页设置已缓存的 .pt 文件')
    source = Path(source)
    with tempfile.TemporaryDirectory() as tmp:
        data = source.read_bytes()
        if data.startswith(b'\x02#!SILK_V3') or data.startswith(b'#!SILK_V3'):
            if not info['silk']:
                raise RuntimeError('缺少 SILK 解码工具 pilk')
            import pilk
            silk = Path(tmp) / 'input.silk'
            pcm = Path(tmp) / 'input.pcm'
            # pilk supports the original Tencent framing, including the 0x02 prefix.
            silk.write_bytes(data)
            pilk.decode(str(silk), str(pcm), 24000)
            wav = Path(tmp) / 'input.wav'
            with wave.open(str(wav), 'wb') as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(24000)
                out.writeframes(pcm.read_bytes())
            source = wav
        result = subprocess.run([info['ffmpeg'], '-nostdin', '-i', str(source), '-f', 's16le',
                                 '-ac', '1', '-ar', '16000', 'pipe:1'], capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError('本地音频解码失败，媒体格式不受支持或缓存不完整')
        audio = np.frombuffer(result.stdout, np.int16).astype(np.float32) / 32768.0
        model = whisper.load_model(str(Path(model_path).resolve()))
        return model.transcribe(audio, language='zh', fp16=False, verbose=False)['text'].strip()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--audio')
    parser.add_argument('--model')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(capabilities(), ensure_ascii=False))
    else:
        try:
            result = {'text': recognize(args.audio, args.model), 'status': 'success'}
        except Exception as exc:
            result = {'status': 'failed', 'reason': str(exc)}
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
