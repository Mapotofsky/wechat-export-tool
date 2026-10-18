"""Isolated audio decoder and local Whisper worker; models are never downloaded."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import wave
from runtime_paths import models_root, ffmpeg_executable, package_root


def select_device():
    try:
        import torch
        if torch.cuda.is_available():
            torch.empty(1, device='cuda')
            return {'device': 'cuda', 'device_name': torch.cuda.get_device_name(0), 'device_reason': ''}
        reason = '当前 PyTorch 为 CPU 版本' if not torch.version.cuda else 'CUDA 驱动或设备不可用'
    except (ImportError, OSError, RuntimeError) as exc:
        reason = f'GPU 检测不可用：{exc}'
    return {'device': 'cpu', 'device_name': 'CPU', 'device_reason': reason}


def capabilities(decoder_only=False):
    model_dirs = [models_root()]
    if not (package_root() / 'runtime' / 'python.exe').is_file():
        model_dirs.append(Path.home() / '.cache' / 'whisper')
    models = sorted({str(p) for directory in model_dirs
                     for p in directory.glob('*.pt')})
    ffmpeg = ffmpeg_executable()
    if not ffmpeg and importlib.util.find_spec('imageio_ffmpeg'):
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    info = {'whisper': bool(importlib.util.find_spec('whisper')), 'models': models,
            'silk': bool(importlib.util.find_spec('pilk')), 'ffmpeg': ffmpeg,
            'available': bool(importlib.util.find_spec('whisper')) and bool(models) and bool(ffmpeg)}
    info['decoder_available'] = bool(ffmpeg)
    if not decoder_only:
        info.update(select_device())
    return info


def decode_audio(source, output):
    import subprocess
    import tempfile
    info = capabilities(decoder_only=True)
    if not info['ffmpeg']:
        raise RuntimeError('缺少 FFmpeg 音频转换工具')
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
        result = subprocess.run([info['ffmpeg'], '-nostdin', '-y', '-i', str(source),
                                 '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(output)],
                                capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError(f'本地音频解码失败（退出码 {result.returncode}）：' +
                               result.stderr.decode('utf-8', errors='replace')[-4000:].strip())


def recognize(source, model_path):
    import whisper
    import numpy as np
    import tempfile
    if not Path(model_path).is_file():
        raise RuntimeError('本地模型文件不存在；请在设置中选择已缓存的 .pt 文件')
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'decoded.wav'
        decode_audio(source, wav)
        with wave.open(str(wav), 'rb') as stream:
            audio = np.frombuffer(stream.readframes(stream.getnframes()), np.int16).astype(np.float32) / 32768.0
        device = select_device()
        def run(chosen):
            model = whisper.load_model(str(Path(model_path).resolve()), device=chosen)
            return model.transcribe(audio, language='zh', fp16=chosen == 'cuda', verbose=False)['text'].strip()
        try:
            text = run(device['device'])
        except RuntimeError as exc:
            if device['device'] != 'cuda':
                raise
            import torch
            # Release failed model frames before retrying after a CUDA OOM.
            reason = str(exc)
            exc.__traceback__ = None
            torch.cuda.empty_cache()
            device = {'device': 'cpu', 'device_name': 'CPU', 'device_reason': f'GPU 识别失败，已回退 CPU：{reason}'}
            text = run('cpu')
        return dict(device, text=text, status='success')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--decoder-only', action='store_true')
    parser.add_argument('--decode-output')
    parser.add_argument('--audio')
    parser.add_argument('--model')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(capabilities(args.decoder_only), ensure_ascii=False))
    elif args.decode_output:
        decode_audio(args.audio, args.decode_output)
    else:
        try:
            result = recognize(args.audio, args.model)
        except Exception as exc:
            result = {'status': 'failed', 'reason': f'{type(exc).__name__}：{exc}'}
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
