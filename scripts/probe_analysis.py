"""用指定 app 和当前解释器验证便携包的离线分词与分析，不读取用户档案。"""
import argparse
from contextlib import closing
import datetime
import json
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.app.resolve()))
    from archive_store import Archive
    import archive_analysis
    import jieba
    with tempfile.TemporaryDirectory(prefix='analysis_probe_') as directory:
        archive = Archive(directory)
        archive.save_config({'analysis_blocklist': []})
        stamp = int(datetime.datetime(2026, 10, 18, 10).timestamp())
        with closing(archive.connect()) as c, c:
            c.execute('INSERT INTO chats VALUES (?,?)', ('synthetic', json.dumps(
                dict(username='synthetic', name='synthetic', category='group'))))
            for i in range(2):
                m = dict(timestamp=stamp+i*1200, type=1, content='报销 合同',
                         sender_username='self', sender='synthetic', type_name='文本',
                         local_id=i, sort_seq=i, source_shard='synthetic.db')
                c.execute('INSERT INTO messages VALUES (?,?,?,?,?)',
                          ('synthetic', 'server:'+str(i), m['timestamp'], json.dumps(m), ''))
        req = dict(usernames=['synthetic'], q='报销', gap_minutes=17)
        result = archive_analysis.analyze(archive, req)
        assert result['overview']['total'] == 2
        assert result['session_total'] == 2
        assert '报销' in [w['term'] for w in result['content']['words']]
        archive.save_config({'analysis_blocklist':[dict(text='报销', mode='exact')]})
        filtered = archive_analysis.analyze(archive, req)
        assert filtered['overview']['total'] == 2
        assert '报销' not in [w['term'] for w in filtered['content']['words']]
    output = dict(passed=True, python=sys.executable, app=str(args.app.resolve()),
                  jieba=jieba.__version__, total=2, gap_minutes=17, sessions=2,
                  blocklist_preserves_message_count=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(output, ensure_ascii=True))


if __name__ == '__main__':
    main()
