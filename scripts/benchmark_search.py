"""仅临时合成档案的单会话检索测量；不读取本机聊天或设置。"""
import argparse
from contextlib import closing
import datetime
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_store import Archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=50000)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if args.rows < 1 or args.repeats < 1:
        parser.error('rows 与 repeats 必须为正整数')
    username = 'synthetic@chatroom'
    with tempfile.TemporaryDirectory(prefix='wechat-search-bench-') as directory:
        archive = Archive(Path(directory))
        started = time.perf_counter()
        expected_body = expected_transcript = 0
        with closing(archive.connect()) as c, c:
            for i in range(args.rows):
                voice = i % 97 == 0
                body_match = not voice and i % 101 == 0
                transcript_match = voice and i % 194 == 0
                expected_body += body_match
                expected_transcript += transcript_match
                stamp = 1700000000 + i * 60
                message = dict(type=34 if voice else 1, type_name='语音' if voice else '文本',
                    content='[语音]' if voice else ('报销材料已经提交。' if body_match else '合成日常讨论。') + '背景内容。' * 25,
                    timestamp=stamp, time=datetime.datetime.fromtimestamp(stamp).isoformat(),
                    local_id=i+1, sort_seq=i, sender='合成发送者', source_shard='message_0.db')
                key = 'server:' + str(i+1)
                c.execute('INSERT INTO messages VALUES (?,?,?,?,?)',
                          (username, key, stamp, json.dumps(message, ensure_ascii=False), ''))
                if voice:
                    c.execute('INSERT INTO transcripts VALUES (?,?,?,?,?,?,?)',
                        (username, key, b'synthetic', 'success',
                         '语音报销说明' if transcript_match else '普通成功转写', '', 'null'))
        report = {'rows': args.rows, 'successful_transcripts': (args.rows-1)//97+1,
                  'expected_content_hits': expected_body, 'expected_transcript_hits': expected_transcript,
                  'seed_seconds': round(time.perf_counter()-started, 4), 'repeats': args.repeats,
                  'python': sys.version.split()[0], 'measurements': {}}

        def measure(name, operation):
            samples = []
            for _ in range(args.repeats):
                began = time.perf_counter()
                result = operation()
                samples.append(round((time.perf_counter()-began)*1000, 3))
            report['measurements'][name] = {'milliseconds': samples,
                'median_ms': statistics.median(samples), 'max_ms': max(samples)}
            return result

        result = measure('search_total_daily_page', lambda: archive.search(username, q='报销'))
        assert result['total'] == expected_body + expected_transcript
        assert sum(item['count'] for item in result['daily']) == result['total']
        assert len(result['messages']) == min(50, result['total'])
        body = measure('search_without_transcripts', lambda: archive.search(username, q='报销', include_transcripts=False))
        assert body['total'] == expected_body
        for name, statement in (
            ('standalone_total_sql_diagnostic', 'SELECT count(*)'),
            ('daily_sql', "SELECT date(m.timestamp,'unixepoch','localtime') AS day,count(*)"),
            ('page_sql', 'SELECT m.message_key,m.data,t.status,t.text')):
            condition, params = archive.search_condition(username, q='报销')
            query = statement + ''' FROM messages m LEFT JOIN transcripts t
                ON t.username=m.username AND t.message_key=m.message_key WHERE ''' + condition
            if name == 'daily_sql':
                query += ' GROUP BY day ORDER BY day'
            elif name == 'page_sql':
                query += " ORDER BY m.timestamp DESC,json_extract(m.data,'$.sort_seq') DESC,json_extract(m.data,'$.local_id') DESC,m.message_key DESC LIMIT 50"
            def run_sql():
                with closing(archive.connect()) as c:
                    return c.execute(query, params).fetchall()
            measure(name, run_sql)
        target = 'server:' + str(args.rows//2+1)
        context = measure('context_middle', lambda: archive.context(username, target))
        assert context['messages'][context['target_index']]['message_id'] == target
        report.update(actual_hits=result['total'], daily_buckets=len(result['daily']),
                      page_size=len(result['messages']), context_size=len(context['messages']))
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
