"""已归档消息的本地统计、内容探索和可回查筛选。"""
from collections import Counter, defaultdict
from contextlib import closing
import datetime as dt
import itertools
import json
import math
import re

from wechat_export import normalize_system_sender


DEFAULT_BLOCKLIST = [dict(text=t, mode='exact') for t in
                     ('的', '了', '是', '我', '你', '他', '她', '我们', '你们', '这个', '那个',
                      '一个', '可以', '就是', '然后', '好的', '收到', '嗯', '啊', '哈哈')]


def rules_for(archive):
    return archive.config().get('analysis_blocklist', DEFAULT_BLOCKLIST)


def normalize_rules(rules):
    result, seen = [], set()
    for rule in rules:
        text, mode = rule['text'].strip().lower(), rule['mode']
        if not text or mode not in ('exact', 'contains'):
            raise ValueError('黑名单词不能为空，匹配方式须为 exact 或 contains')
        if (text, mode) not in seen:
            result.append(dict(text=text, mode=mode))
            seen.add((text, mode))
    return result


def sender_key(m):
    if (m.get('type', 0) & 0xffffffff) == 10000:
        return 'system'
    # 未知身份按会话/来源标识保留，不能把同名的人合并。
    return m.get('sender_username') or 'unknown:' + ':'.join(
        str(m.get(k) or '') for k in ('username', 'source_shard', 'sender_id'))


def text_for(m, include_transcripts):
    kind = m.get('type', 0) & 0xffffffff
    if kind == 1:
        return m.get('content') or ''
    if kind == 34 and include_transcripts and m.get('transcription', {}).get('status') == 'success':
        return m.get('content') or ''
    return ''


def load_rows(archive, c, usernames, start, end, q, include_transcripts):
    contacts = {x['username']: x for x in archive.contacts(c)}
    if not usernames or set(usernames) - contacts.keys():
        raise ValueError('分析范围须为已导入的会话')
    rows = []
    for username in dict.fromkeys(usernames):
        condition, values = archive.search_condition(username, q, start, end,
                                                     include_transcripts=include_transcripts)
        base, bounds = archive.search_condition(username, start=start, end=end)
        sql = '''SELECT m.message_key,m.data,t.status,t.text,t.reason,
            CASE WHEN ''' + condition + ''' THEN 1 ELSE 0 END
            FROM messages m LEFT JOIN transcripts t
            ON t.username=m.username AND t.message_key=m.message_key WHERE ''' + base + '''
            ORDER BY m.timestamp,json_extract(m.data,'$.sort_seq'),
            json_extract(m.data,'$.local_id'),m.message_key'''
        for key, data, status, text, reason, matched in c.execute(sql, values + bounds):
            m = normalize_system_sender(json.loads(data))
            m.update(username=username, chat_name=contacts[username]['name'], message_id=key,
                     query_match=bool(matched))
            if (m['type'] & 0xffffffff) == 34:
                m['transcription'] = dict(status=status or 'not_requested', reason=reason or '')
                if status == 'success':
                    m['content'] = text
            stamp = dt.datetime.fromtimestamp(m['timestamp'])
            m.update(day=stamp.date().isoformat(), hour=stamp.hour, weekday=stamp.weekday(),
                     sender_key=sender_key(m), kind=m['type'] & 0xffffffff)
            rows.append(m)
    return rows, contacts


def lexical_parts(m, include_transcripts):
    import jieba
    if '_lexical' not in m:
        chunks = []
        for chunk in re.findall(r'[\u4e00-\u9fffA-Za-z0-9 \t]+', text_for(m, include_transcripts).lower()):
            chunks.append([w for w in jieba.cut(chunk, HMM=False) if w.strip()])
        m['_lexical'] = chunks
    return m['_lexical']


def phrase_parts(chunks):
    for words in chunks:
        for size in (2, 3):
            for i in range(len(words) - size + 1):
                part = words[i:i + size]
                phrase = ''.join(part) if re.fullmatch(r'[\u4e00-\u9fff]+', ''.join(part)) else ' '.join(part)
                if 3 <= len(phrase) <= 24:
                    yield part, phrase


def matches(m, f, include_transcripts):
    if not m['query_match']:
        return False
    for field in ('sender_key', 'kind', 'hour', 'weekday', 'username'):
        if f.get(field) is not None and m[field] != f[field]:
            return False
    if f.get('voice_status') is not None and m.get('transcription', {}).get('status') != f['voice_status']:
        return False
    if f.get('start') is not None and m['timestamp'] < f['start']:
        return False
    if f.get('end') is not None and m['timestamp'] > f['end']:
        return False
    terms = [t.lower() for t in f.get('terms', [])]
    if not terms:
        return True
    if f.get('term_mode') == 'word':
        words = {w for chunk in lexical_parts(m, include_transcripts) for w in chunk}
        return all(t in words for t in terms)
    if f.get('term_mode') == 'phrase':
        phrases = {phrase for _, phrase in phrase_parts(lexical_parts(m, include_transcripts))}
        return all(t in phrases for t in terms)
    return all(t in text_for(m, include_transcripts).lower() for t in terms)


def summary(rows, start=None, end=None):
    daily = Counter(m['day'] for m in rows)
    stamps = [m['timestamp'] for m in rows]
    first = dt.datetime.fromtimestamp(start if start is not None else min(stamps)).date() if stamps or start is not None else None
    last = dt.datetime.fromtimestamp(end if end is not None else max(stamps)).date() if stamps or end is not None else None
    days = max(1, (last - first).days + 1) if first and last else 0
    peak = max(daily, key=lambda d: (daily[d], d)) if daily else None
    return dict(total=len(rows), active_days=len(daily), calendar_days=days,
                daily_average=round(len(rows) / days, 2) if days else 0,
                first=first.isoformat() if first else None, last=last.isoformat() if last else None,
                peak=peak, peak_count=daily.get(peak, 0),
                participants=len({m['sender_key'] for m in rows if m['kind'] != 10000}))


def ranked(counter, names=None):
    return [dict(key=k, name=(names or {}).get(k, str(k)), count=n)
            for k, n in counter.most_common()]


def content_stats(rows, rules, include_transcripts, multiple):
    exact = {r['text'] for r in rules if r['mode'] == 'exact'}
    contained = [r['text'] for r in rules if r['mode'] == 'contains']

    def allowed(word):
        return word not in exact and not any(t in word for t in contained)

    mentions, occurrences, phrase_mentions, phrase_occurrences = Counter(), Counter(), Counter(), Counter()
    documents = defaultdict(Counter)
    token_sets, examples = [], {}
    for m in rows:
        tokens, phrases = [], []
        # 标点是短语边界；被排除的词保留边界，不拼接出原文不存在的短语。
        chunks = lexical_parts(m, include_transcripts)
        for words in chunks:
            tokens.extend(w for w in words if allowed(w))
        for part, phrase in phrase_parts(chunks):
            if all(allowed(w) for w in part) and allowed(phrase):
                phrases.append(phrase)
        unique = set(tokens)
        mentions.update(unique)
        occurrences.update(tokens)
        phrase_mentions.update(set(phrases))
        phrase_occurrences.update(phrases)
        doc = m['username'] if multiple else m['day'][:7]
        documents[doc].update(unique)
        token_sets.append(unique)
        for t in unique | set(phrases):
            original = text_for(m, include_transcripts)
            position = max(0, original.lower().find(t) - 45)
            examples.setdefault(t, dict(username=m['username'], message_id=m['message_id'],
                                        snippet=original[position:position + 180]))
    top = [w for w, _ in mentions.most_common(30)]
    top_set, pairs = set(top), Counter()
    for tokens in token_sets:
        pairs.update(itertools.combinations(sorted(tokens & top_set), 2))
    doc_frequency = Counter(t for doc in documents.values() for t in doc)
    distinctive = []
    for doc, counts in documents.items():
        denominator = sum(counts.values()) or 1
        for word, n in counts.items():
            score = n / denominator * (math.log((1 + len(documents)) / (1 + doc_frequency[word])) + 1)
            distinctive.append(dict(term=word, document=doc, score=round(score, 4), count=n,
                                    example=examples[word]))
    distinctive.sort(key=lambda r: (-r['score'], -r['count'], r['term']))
    def items(counts, times, minimum=1):
        return [dict(term=w, count=n, occurrences=times[w], example=examples[w])
                for w, n in counts.most_common() if n >= minimum][:30]
    return dict(words=items(mentions, occurrences), phrases=items(phrase_mentions, phrase_occurrences, 2),
                distinctive=distinctive[:20] if len(documents) > 1 else [],
                document_basis='会话' if multiple else '月份',
                cooccurrences=[dict(terms=list(pair), count=n) for pair, n in pairs.most_common(20)],
                text_messages=sum(bool(text_for(m, include_transcripts)) for m in rows))


def analyze(archive, req):
    values = req if isinstance(req, dict) else req.model_dump()
    usernames = list(dict.fromkeys(values['usernames']))
    start, end, q = values.get('start'), values.get('end'), values.get('q', '')
    if start is not None and end is not None and start > end:
        raise ValueError('开始日期不能晚于结束日期')
    gap = values.get('gap_minutes', 30)
    if not isinstance(gap, int) or gap <= 0:
        raise ValueError('片段间隔须为正整数分钟')
    include = values.get('include_transcripts', True)
    f = values.get('filters') or {}
    with closing(archive.connect()) as c:
        c.execute('BEGIN')
        rows, contacts = load_rows(archive, c, usernames, start, end, q, include)
        rules_row = c.execute("SELECT value FROM settings WHERE key='analysis_blocklist'").fetchone()
        rules = json.loads(rules_row[0]) if rules_row else DEFAULT_BLOCKLIST
        current = [m for m in rows if matches(m, f, include)]
        effective_start = max(x for x in (start, f.get('start')) if x is not None) if start is not None or f.get('start') is not None else None
        effective_end = min(x for x in (end, f.get('end')) if x is not None) if end is not None or f.get('end') is not None else None
        overview = summary(current, effective_start, effective_end)
        comparison = values.get('comparison') or {}
        cs, ce = comparison.get('start'), comparison.get('end')
        if cs is None or ce is None:
            first, last = overview['first'], overview['last']
            if first and last:
                a, b = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
                ce = int(dt.datetime.combine(a, dt.time()).timestamp()) - 1
                cs = int(dt.datetime.combine(a - dt.timedelta(days=(b-a).days + 1), dt.time()).timestamp())
        previous = None
        if cs is not None and ce is not None:
            if cs > ce:
                raise ValueError('对比开始日期不能晚于结束日期')
            previous_rows, _ = load_rows(archive, c, usernames, cs, ce, q, include)
            # 对比继承分类和内容条件；图中选择的绝对日期不强加到另一时段。
            comparable = {k: v for k, v in f.items() if k not in ('start', 'end')}
            previous = summary([m for m in previous_rows if matches(m, comparable, include)], cs, ce)
    daily = Counter(m['day'] for m in current)
    senders, kinds, voices, hours, weekdays, heat = (Counter() for _ in range(6))
    sender_names, type_names = {}, {}
    participant_daily = defaultdict(set)
    sender_daily = defaultdict(Counter)
    for m in current:
        key = m['sender_key']
        senders[key] += 1
        sender_names[key] = m.get('sender') or '身份未知'
        kinds[m['kind']] += 1
        type_names[m['kind']] = m.get('type_name') or str(m['kind'])
        hours[m['hour']] += 1
        weekdays[m['weekday']] += 1
        heat[(m['weekday'], m['hour'])] += 1
        if m['kind'] != 10000:
            participant_daily[m['day']].add(key)
        sender_daily[m['day']][key] += 1
        if m['kind'] == 34:
            voices[m['transcription']['status']] += 1
    config = archive.config()
    me = config.get('self_username')
    mine = sum(n for k, n in senders.items() if k == me) if me else None
    sessions = []
    for username, group in itertools.groupby(rows, lambda m: m['username']):
        segment = []
        def flush():
            hits = [m for m in segment if matches(m, f, include)]
            if not hits:
                return
            sessions.append(dict(username=username, name=contacts[username]['name'],
                start=segment[0]['timestamp'], end=segment[-1]['timestamp'],
                total=len(segment), count=len(hits), participants=len({m['sender_key'] for m in segment if m['kind'] != 10000}),
                first_id=hits[-1]['message_id'], snippet=hits[-1].get('content', '')[:140]))
        for m in group:
            if segment and m['timestamp'] - segment[-1]['timestamp'] > gap * 60:
                flush()
                segment = []
            segment.append(m)
        flush()
    sessions.sort(key=lambda r: (r['end'], r['username'], r['first_id']), reverse=True)
    session_offset = values.get('session_offset', 0)
    terms = list(dict.fromkeys(t.strip() for t in values.get('keywords', []) if t.strip()))
    keyword_series = []
    for term in terms:
        counts = Counter(m['day'] for m in current if term.lower() in text_for(m, include).lower())
        keyword_series.append(dict(term=term, count=sum(counts.values()),
            daily=[dict(day=d, count=counts[d]) for d in sorted(daily, reverse=True)]))
    content = content_stats(current, rules, include, len(usernames) > 1)
    return dict(overview=overview, previous=previous,
        daily=[dict(day=d, count=daily[d], participants=len(participant_daily[d]),
                    senders=dict(sender_daily[d])) for d in sorted(daily, reverse=True)],
        senders=ranked(senders, sender_names), types=ranked(kinds, type_names), voices=ranked(voices),
        hours=[dict(key=h, count=hours[h]) for h in range(24)],
        weekdays=[dict(key=d, count=weekdays[d]) for d in range(7)],
        heat=[dict(weekday=d, hour=h, count=heat[(d, h)]) for d in range(7) for h in range(24)],
        self_count=mine, other_count=len(current)-senders['system']-mine if mine is not None else None,
        chats=[dict(username=u, name=contacts[u]['name'],
                    daily=[dict(day=d, count=n) for d, n in sorted(Counter(m['day'] for m in current if m['username']==u).items(), reverse=True)],
                    **summary([m for m in current if m['username']==u], effective_start, effective_end)) for u in usernames],
        content=content, keywords=keyword_series,
        sessions=sessions[session_offset:session_offset+30], session_total=len(sessions),
        blocklist_count=len(rules), query=dict(usernames=usernames, q=q, start=start, end=end, filters=f))


def analysis_messages(archive, req):
    v = req.model_dump()
    with closing(archive.connect()) as c:
        c.execute('BEGIN')
        rows, _ = load_rows(archive, c, v['usernames'], v.get('start'), v.get('end'),
                            v.get('q', ''), v.get('include_transcripts', True))
    rows = [m for m in rows if matches(m, v.get('filters') or {}, v.get('include_transcripts', True))]
    rows.sort(key=lambda m: (m['timestamp'], m.get('sort_seq', 0), m.get('local_id', 0),
                             m['message_id'], m['username']), reverse=True)
    messages = rows[v['offset']:v['offset']+50]
    for m in messages:
        m.pop('_lexical', None)
    return dict(total=len(rows), messages=messages)
