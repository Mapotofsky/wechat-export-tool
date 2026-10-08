# -*- coding: utf-8 -*-
"""Windows 本机微信聊天记录导出入口。"""
import argparse
from pathlib import Path
import sqlite3
import threading
import time
import urllib.request
import webbrowser


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / 'decrypted'
DEFAULT_OUT = ROOT / 'exports'


def database_dir(path):
    db_dir = Path(path).expanduser().resolve()
    if not db_dir.is_dir():
        raise ValueError(f'解密数据库目录不存在：{db_dir}。请先运行密钥工具解密，或用 --db 指定目录。')
    if not (db_dir / 'contact' / 'contact.db').is_file():
        raise ValueError(f'缺少 contact/contact.db：{db_dir}。--db 应指向解密数据库的根目录。')
    if not list((db_dir / 'message').glob('message_*.db')):
        raise ValueError(f'缺少 message/message_*.db：{db_dir}。请确认数据库已解密。')
    return db_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description='微信 4.x 聊天记录本机导出')
    sub = parser.add_subparsers(dest='command', required=True)

    list_cmd = sub.add_parser('list', help='列出有聊天记录的联系人')
    export_cmd = sub.add_parser('export', help='导出 TXT 和 JSON')
    web_cmd = sub.add_parser('web', help='启动本机网页')
    for cmd in (list_cmd, export_cmd, web_cmd):
        cmd.add_argument('--db', type=Path, default=DEFAULT_DB, help='解密数据库目录（默认：项目目录/decrypted）')

    target = export_cmd.add_mutually_exclusive_group(required=True)
    target.add_argument('--contact', help='备注、昵称或微信 ID，支持模糊匹配')
    target.add_argument('--all', action='store_true', help='导出全部联系人')
    export_cmd.add_argument('--out', type=Path, default=DEFAULT_OUT, help='输出目录（默认：项目目录/exports）')
    export_cmd.add_argument('--my-name', default='我', help='自己的显示名')
    web_cmd.add_argument('--port', type=int, default=8765, help='本机端口（默认：8765）')
    web_cmd.add_argument('--open-browser', action='store_true', help='服务就绪后自动打开浏览器')

    args = parser.parse_args(argv)
    try:
        if args.command == 'web':
            if not 1 <= args.port <= 65535:
                parser.error('--port 必须在 1 到 65535 之间')
            try:
                import uvicorn
                from web import app as web_app
            except ImportError as exc:
                raise RuntimeError('缺少网页依赖。请运行：python -m pip install -r requirements.txt') from exc
            if not web_app.we.HAS_ZSTD:
                raise RuntimeError('缺少 zstandard。请运行：python -m pip install -r requirements.txt')
            web_app.DB_DIR = args.db.resolve()
            print(f'请在浏览器打开 http://127.0.0.1:{args.port}')
            if args.open_browser:
                def open_when_ready():
                    url = f'http://127.0.0.1:{args.port}'
                    for _ in range(100):
                        try:
                            with urllib.request.urlopen(url + '/api/data-status', timeout=1) as response:
                                if response.status == 200:
                                    webbrowser.open(url)
                                    return
                        except OSError:
                            time.sleep(.2)
                threading.Thread(target=open_when_ready, daemon=True).start()
            uvicorn.run(web_app.app, host='127.0.0.1', port=args.port, access_log=False)
            return

        db_dir = database_dir(args.db)

        try:
            from wechat_export import load_contacts, find_contact, export_contact
            from wechat_export import HAS_ZSTD
        except ImportError as exc:
            raise RuntimeError('缺少导出依赖。请运行：python -m pip install -r requirements.txt') from exc
        if not HAS_ZSTD:
            raise RuntimeError('缺少 zstandard。请运行：python -m pip install -r requirements.txt')
        contacts = load_contacts(str(db_dir))
        if not contacts:
            raise ValueError(f'未找到有聊天记录的联系人：{db_dir}。请确认这是解密后的数据库。')
        if args.command == 'list':
            print(f'共 {len(contacts)} 个联系人：')
            for contact in contacts:
                tag = '群' if contact['category'] == 'group' else '友'
                print(f"  [{tag}] {contact['name']}  ({contact['username']})")
            return

        if args.all:
            targets = contacts
        else:
            contact = find_contact(contacts, args.contact)
            if contact is None:
                raise ValueError(f'未找到联系人「{args.contact}」。可先运行 list 查看。')
            targets = [contact]
        for contact in targets:
            result = export_contact(str(db_dir), contact, str(args.out), my_name=args.my_name)
            print(f"[{contact['name']}] {result['total']} 条 -> {result['txt']}，{result['json']}")
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'错误：{exc}\n')


if __name__ == '__main__':
    main()
