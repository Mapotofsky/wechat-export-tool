"""ZIP the release inventory, excluding any runtime-created user data."""
import argparse
import json
from pathlib import Path
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    root = args.directory.resolve()
    manifest = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
    with zipfile.ZipFile(args.output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative in sorted(set(manifest['files']) | {'release-manifest.json'}):
            path = root / relative
            if 'local_data' in Path(relative).parts or not path.resolve().is_relative_to(root):
                raise ValueError('发布清单不能包含用户数据或目录外文件')
            archive.write(path, f'{root.name}/{relative}')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
