"""Check published inventories for accidental user files and report actual artifacts."""
import argparse
import json
from pathlib import Path
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('archives', nargs='+', type=Path)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
results = []
for path in args.archives:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        forbidden = [name for name in names if set(Path(name).parts) & {
            'local_data', 'decrypted', 'exports', 'all_keys.json', '__pycache__'}]
        if forbidden:
            raise ValueError(f'私有数据不应进入发布文件：{forbidden}')
        manifest_name = next(name for name in names if name.count('/') == 1 and name.endswith('/release-manifest.json'))
        manifest = json.loads(archive.read(manifest_name))
        html_name = manifest_name.split('/')[0] + '/app/web/static/index.html'
        html = archive.read(html_name).decode('utf-8')
        result = {'archive': str(path.resolve()), 'bytes': path.stat().st_size,
                  'files': len(names), 'flavor': manifest['flavor'],
                  'frontend_entry': html.split('src="')[1].split('"')[0],
                  'frontend_dependencies': manifest['packages']['frontend'],
                  'prerequisites': manifest['prerequisites'], 'private_files': forbidden}
        results.append(result)
args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(results, ensure_ascii=False, indent=2))
