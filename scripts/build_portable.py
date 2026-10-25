"""Assemble only declared program files; never copy a developer environment or data."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PYTHON_VERSION = '3.11.9'
PYTHON_URL = f'https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip'
PREREQUISITES = {
    'MicrosoftEdgeWebView2RuntimeInstallerX64.exe': 'https://go.microsoft.com/fwlink/?LinkId=2124701',
    'NDP48-x86-x64-AllOS-ENU.exe': 'https://go.microsoft.com/fwlink/?linkid=2088631',
    'VC_redist.x64.exe': 'https://aka.ms/vs/17/release/vc_redist.x64.exe',
}
PROGRAM_FILES = ('archive_store.py', 'archive_analysis.py', 'analysis_lexicon.py', 'encrypted_sync.py', 'local_asr.py', 'maintenance.py',
    'voice_service.py', 'wcdb_key_tool_windows.py', 'wechat_export.py', 'run.py',
    'runtime_paths.py', 'desktop.py')


def run(*args):
    subprocess.run([str(a) for a in args], check=True)


def download(url, target):
    if not target.is_file():
        print(f'Download {url}', flush=True)
        partial = target.with_name(target.name + '.partial')
        for attempt in range(3):
            try:
                urllib.request.urlretrieve(url, partial)
                partial.replace(target)
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(1)


def prepare_runtime(target, cache):
    if (target / 'python.exe').is_file():
        return
    archive = cache / f'python-{PYTHON_VERSION}-embed-amd64.zip'
    download(PYTHON_URL, archive)
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as source:
        source.extractall(target)
    (target / 'python311._pth').write_text('python311.zip\n.\nLib/site-packages\n../app\nimport site\n', encoding='utf-8')
    # Bootstrap pip at build time only. The released interpreter is ordinary CPython.
    run(sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--target',
        target / 'Lib' / 'site-packages', 'pip==25.0.1', 'setuptools==75.8.2', 'wheel==0.45.1')


def collect_licenses(runtime, output):
    output.mkdir(parents=True, exist_ok=True)
    packages = []
    site = runtime / 'Lib' / 'site-packages'
    for package in metadata.distributions(path=[str(site)]):
        name = package.metadata['Name']
        packages.append({'name': name, 'version': package.version,
                         'license': package.metadata.get('License-Expression') or package.metadata.get('License'),
                         'homepage': package.metadata.get('Home-page')})
        for file in package.files or []:
            if any(part.lower().startswith(('license', 'copying', 'notice')) for part in Path(str(file)).parts):
                source = Path(package.locate_file(file))
                if source.is_file():
                    relative = Path(str(file))
                    if '..' in relative.parts:
                        continue
                    destination = output / name / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    return sorted(packages, key=lambda p: p['name'].lower())


def collect_frontend_licenses(output):
    output.mkdir(parents=True, exist_ok=True)
    packages = []
    for name in ('react', 'react-dom', 'scheduler'):
        directory = ROOT / 'frontend' / 'node_modules' / name
        info = json.loads((directory / 'package.json').read_text(encoding='utf-8'))
        shutil.copy2(directory / 'LICENSE', output / (name + '-LICENSE.txt'))
        packages.append({'name': name, 'version': info['version'], 'license': info['license']})
    return packages


def windows_file_version(path):
    import ctypes
    from ctypes import wintypes
    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    buffer = ctypes.create_string_buffer(size)
    if not size or not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not ctypes.windll.version.VerQueryValueW(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length)):
        return None
    fields = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    return '.'.join(str(v) for v in (fields[4] >> 16, fields[4] & 65535, fields[5] >> 16, fields[5] & 65535))


def release_files(destination):
    return sorted(str(p.relative_to(destination)).replace('\\', '/') for p in destination.rglob('*')
                  if p.is_file() and 'local_data' not in p.relative_to(destination).parts
                  and '__pycache__' not in p.relative_to(destination).parts
                  and p.name != 'release-manifest.json')


def build(args):
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    app = destination / 'app'
    app.mkdir(exist_ok=True)
    for name in PROGRAM_FILES:
        shutil.copy2(ROOT / name, app / name)
    (app / 'web').mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'README.md', destination / 'README.md')
    if (ROOT / 'docs').is_dir():
        (destination / 'docs').mkdir(exist_ok=True)
        for document in (ROOT / 'docs').glob('*.md'):
            shutil.copy2(document, destination / 'docs' / document.name)
    for name in ('app.py', '__init__.py'):
        source = ROOT / 'web' / name
        if source.is_file():
            shutil.copy2(source, app / 'web' / name)
    static = ROOT / 'web' / 'static'
    static_target = app / 'web' / 'static'
    if static_target.is_dir():
        for old in static_target.rglob('*'):
            if old.is_file() and not (static / old.relative_to(static_target)).is_file():
                old.unlink()
    shutil.copytree(static, app / 'web' / 'static', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.map'))
    if args.resources_only:
        manifest_file = destination / 'release-manifest.json'
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        manifest['program_files'] = list(PROGRAM_FILES)
        manifest['packages']['base'] = collect_licenses(destination / 'runtime', destination / 'licenses' / 'base')
        if (destination / 'voice-runtime' / 'python.exe').is_file():
            manifest['packages']['voice'] = collect_licenses(destination / 'voice-runtime', destination / 'licenses' / 'voice')
            torch_version = next(p['version'] for p in manifest['packages']['voice'] if p['name'].lower() == 'torch')
            manifest['flavor'] = 'voice-cuda' if '+cu' in torch_version else 'voice-cpu'
        for lock in ('requirements-desktop.lock', 'requirements-voice.lock'):
            shutil.copy2(ROOT / lock, destination / lock)
        manifest['files'] = release_files(destination)
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Refreshed resources {destination}', flush=True)
        return
    runtime = destination / 'runtime'
    prepare_runtime(runtime, cache)
    run(runtime / 'python.exe', '-m', 'pip', 'install', '--disable-pip-version-check',
        '-r', ROOT / 'requirements-desktop.lock')
    licenses = destination / 'licenses'
    packages = {'base': collect_licenses(runtime, licenses / 'base'),
                'frontend': collect_frontend_licenses(licenses / 'frontend')}
    shutil.copy2(runtime / 'LICENSE.txt', licenses / 'CPython-LICENSE.txt')
    for lock in ('requirements-desktop.lock', 'requirements-voice.lock'):
        shutil.copy2(ROOT / lock, destination / lock)
    prerequisites = destination / 'prerequisites'
    prerequisites.mkdir(exist_ok=True)
    (prerequisites / '说明.txt').write_text(
        '支持 Windows 10/11 x64。需要 .NET Framework 4.6.2+ 与 WebView2 Runtime。\n'
        '完整语音版还需要 Microsoft Visual C++ 2015-2022 x64 Runtime（VC_redist.x64.exe）。\n'
        '若启动提示缺失，运行此目录中的微软离线安装器，再重新启动程序。\n'
        '安装器可能要求管理员权限或重启；请按微软安装界面操作。\n'
        '官方下载：\nhttps://developer.microsoft.com/microsoft-edge/webview2/\n'
        'https://dotnet.microsoft.com/download/dotnet-framework/net48\n', encoding='utf-8')
    if args.prerequisites:
        for name, url in PREREQUISITES.items():
            download(url, cache / name)
            shutil.copy2(cache / name, prerequisites / name)
    if args.voice:
        voice = destination / 'voice-runtime'
        prepare_runtime(voice, cache)
        variant = 'cu121' if args.voice_device == 'cuda' else 'cpu'
        run(voice / 'python.exe', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps',
            '--index-url', f'https://download.pytorch.org/whl/{variant}', f'torch==2.5.1+{variant}')
        run(voice / 'python.exe', '-m', 'pip', 'install', '--disable-pip-version-check',
            '-r', ROOT / 'requirements-voice.lock')
        packages['voice'] = collect_licenses(voice, licenses / 'voice')
        if not args.model or not args.model.is_file():
            raise ValueError('--voice 要求 --model 指向已下载的 Whisper .pt 模型')
        models = destination / 'models'
        models.mkdir(exist_ok=True)
        shutil.copy2(args.model, models / args.model.name)
        tools = destination / 'tools'
        tools.mkdir(exist_ok=True)
        result = subprocess.check_output([str(voice / 'python.exe'), '-c',
            'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'], text=True).strip()
        shutil.copy2(result, tools / 'ffmpeg.exe')
        ffmpeg_info = subprocess.run([str(tools / 'ffmpeg.exe'), '-buildconf'], capture_output=True, text=True, check=True)
        (licenses / 'FFmpeg-buildconf.txt').write_text(ffmpeg_info.stdout + ffmpeg_info.stderr, encoding='utf-8')
        (licenses / 'FFmpeg-source.txt').write_text(
            'Bundled executable: FFmpeg 7.1 essentials_build by www.gyan.dev, from imageio-ffmpeg 0.6.0 wheel.\n'
            'License: GPL version 3 or later (--enable-gpl --enable-version3).\n'
            'Upstream source release: https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz\n'
            'Windows build and dependency-source instructions: https://www.gyan.dev/ffmpeg/builds/\n'
            'Build scripts: https://github.com/m-ab-s/media-autobuild_suite\n'
            'Wheel packaging: https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0\n'
            'See FFmpeg-buildconf.txt for every enabled external library. For public redistribution,\n'
            'provide the corresponding source for the actual executable and all linked GPL dependencies.\n', encoding='utf-8')
        packages['model'] = {'name': args.model.name, 'bytes': args.model.stat().st_size,
                             'source': 'https://github.com/openai/whisper', 'license': 'MIT'}
        download('https://raw.githubusercontent.com/openai/whisper/main/LICENSE', licenses / 'Whisper-model-LICENSE.txt')
        # imageio's bundled FFmpeg is distributed under GPL; preserve its notice.
        download('https://raw.githubusercontent.com/imageio/imageio-ffmpeg/main/LICENSE', licenses / 'imageio-ffmpeg-LICENSE.txt')
        download('https://www.gnu.org/licenses/gpl-3.0.txt', licenses / 'FFmpeg-GPL-3.0.txt')
    (destination / '启动微信聊天档案.cmd').write_text(
        '@echo off\r\ncd /d "%~dp0"\r\nstart "" "%~dp0runtime\\pythonw.exe" "%~dp0app\\desktop.py"\r\n', encoding='utf-8')
    (destination / '诊断启动.cmd').write_text(
        '@echo off\r\ncd /d "%~dp0"\r\n"%~dp0runtime\\python.exe" "%~dp0app\\desktop.py"\r\npause\r\n', encoding='utf-8')
    inventory = release_files(destination)
    manifest = {'python': PYTHON_VERSION, 'python_source': PYTHON_URL, 'flavor': 'voice-' + args.voice_device if args.voice else 'base',
                'program_files': list(PROGRAM_FILES), 'packages': packages, 'files': inventory,
                'prerequisites': {name: {'source': url, 'bytes': (prerequisites / name).stat().st_size,
                    'product_version': windows_file_version(prerequisites / name)}
                    for name, url in PREREQUISITES.items()} if args.prerequisites else {}}
    (destination / 'release-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Built {destination}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=ROOT / 'build-cache')
    parser.add_argument('--voice', action='store_true')
    parser.add_argument('--voice-device', choices=('cuda', 'cpu'), default='cuda', help='语音包默认包含 CUDA 12.1 运行时，无可用 GPU 时自动使用 CPU')
    parser.add_argument('--model', type=Path)
    parser.add_argument('--prerequisites', action='store_true', help='随包附带微软 WebView2/.NET 离线安装器')
    parser.add_argument('--resources-only', action='store_true', help='仅更新已有包的程序和静态资源，不改变运行时依赖')
    build(parser.parse_args())
