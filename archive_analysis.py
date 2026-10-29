"""已归档消息的本地统计、内容探索和可回查筛选。"""
from collections import Counter, defaultdict
from contextlib import closing
import datetime as dt
import itertools
import json
import math
import re

from wechat_export import normalize_system_sender
import analysis_lexicon as lex


DEFAULT_BLOCKLIST = []


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
    contacts = {}
    for username in dict.fromkeys(usernames):
        row=c.execute('SELECT data FROM chats WHERE username=?',(username,)).fetchone()
        if row:contacts[username]=json.loads(row[0])
    if not usernames or set(usernames) - contacts.keys():
        raise ValueError('分析范围须为已导入的会话')
    rows = []
    for username in dict.fromkeys(usernames):
        condition, values = archive.search_condition(username, q, start, end,
                                                     include_transcripts=include_transcripts)
        base, bounds = archive.search_condition(username, start=start, end=end)
        sql = '''SELECT m.message_key,m.data,CASE WHEN EXISTS(SELECT 1 FROM voice_skips s WHERE s.username=m.username AND s.message_key=m.message_key) THEN 'skipped' ELSE t.status END,t.text,t.reason,
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
    return [[t[0].lower() for t in block] for block in lexical_blocks(m, include_transcripts)]


def lexical_blocks(m, include):
    context = m.get('_analysis', {})
    ignore = context.get('options', lex.DEFAULT_OPTIONS)['ignore_structured']
    cache = m.setdefault('_tokens', {})
    if ignore not in cache:
        cache[ignore] = lex.scan(text_for(m, include), ignore)
    return cache[ignore]


def phrase_parts(chunks):
    for words in chunks:
        for size in (2, 3, 4, 5, 6):
            for i in range(len(words)-size+1):
                part = words[i:i+size]
                phrase = ''.join(part)
                if len(phrase) <= 32:
                    yield part, phrase


def independent_occurrences(a, b):
    return any(y <= u or v <= x for x,y in a for u,v in b)


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
    if f.get('term_mode') in ('word', 'phrase'):
        chunks = lexical_parts(m, include_transcripts)
        expressions = ({w for chunk in chunks for w in chunk} if f['term_mode'] == 'word'
                       else {p for _,p in phrase_parts(chunks)})
        return all(t in expressions for t in terms)
    if f.get('term_mode') == 'expression':
        positions = [lex.expression_spans(text_for(m, include_transcripts), t, m.get('_analysis')) for t in terms]
        return all(positions) and (not f.get('independent_terms') or
                                 all(independent_occurrences(a,b) for a,b in itertools.combinations(positions,2)))
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


def candidates(m, rules, include):
    context = m.get('_analysis', {})
    words, phrases, _ = lex.token_candidates(lexical_blocks(m, include), context.get('options', lex.DEFAULT_OPTIONS), rules)
    return Counter(words), Counter(phrases)


def content_index(rows, rules, include, context=None):
    context = context or (rows[0].get('_analysis', {}) if rows else {})
    options = context.get('options', lex.DEFAULT_OPTIONS)
    aliases = context.get('aliases', [])
    texts = [text_for(m, include) for m in rows]
    blocks = [lexical_blocks(m, include) for m in rows]
    words, phrases, labels = set(), set(), {}
    for block in blocks:
        w,p,names = lex.token_candidates(block, options, rules)
        words.update(w); phrases.update(p)
    discovered = lex.discover(texts, blocks, rules) if options['discover'] else []
    del blocks
    for m in rows:m.pop('_tokens',None)
    new_terms = {x['term'] for x in discovered}
    vocabulary = words | phrases | new_terms
    for group in aliases:
        if group['enabled']:
            vocabulary.update(t.lower() for t in group['members'])
    positions={term:{} for term in vocabulary}
    matcher=lex.LiteralMatcher(vocabulary)
    for i,text in enumerate(texts):
        for term,spans in matcher.spans(text,options['ignore_structured']).items():
            positions[term][i]=spans
    del matcher
    counts, times, dates, examples, variants = Counter(), Counter(), defaultdict(set), {}, {}
    for term,hits in positions.items():
        counts[term]=len(hits)
        times[term]=sum(len(s) for s in hits.values())
        if len(hits)>1:dates[term]={rows[i]['day'] for i in hits}
    # 原始计数覆盖全候选；只为可能展示的项构造原文形式和摘录。
    def order(t):return -counts[t],-(len(dates[t]) if counts[t]>1 else counts[t]),t
    detail_terms={t for t in vocabulary if counts[t]>=2}
    detail_terms.update(sorted(words,key=order)[:60])
    detail_terms.update(sorted((t for t in phrases if counts[t]>=options['min_phrase_messages']),key=order)[:60])
    if options['min_phrase_messages']==1:detail_terms.update(phrases)
    detail_terms.update(new_terms)
    detail_terms.update(t.lower() for g in aliases if g['enabled'] for t in g['members'])
    positions={t:hits for t,hits in positions.items() if t in detail_terms}
    for term in sorted(vocabulary):
        if term not in detail_terms:continue
        hits, forms = positions[term], defaultdict(set)
        if counts[term]<=1:dates[term]={rows[i]['day'] for i in hits}
        for i,spans in hits.items():
            for a,b in spans:forms[texts[i][a:b]].add(i)
        variants[term] = [dict(text=t,count=len(ids)) for t,ids in sorted(forms.items())]
        if hits:
            i = next(iter(hits)); m=rows[i]; a,b=hits[i][0]
            examples[term]=dict(username=m['username'],message_id=m['message_id'],snippet=texts[i][max(0,a-40):b+100])
            labels[term]=texts[i][a:b]
    group_items=[]
    for group in aliases:
        if not group['enabled']: continue
        key='@alias/'+group['id']; hits={}; members=[]
        for member in group['members']:
            term=member.lower(); excluded=not lex.allowed(term,rules)
            members.append(dict(term=term,display=member,count=counts[term],occurrences=times[term],
                                excluded=excluded,daily=[dict(day=d,count=n) for d,n in sorted(Counter(rows[i]['day'] for i in positions[term]).items(),reverse=True)]))
            if not excluded:
                for i,spans in positions[term].items(): hits.setdefault(i,[]).extend(spans)
        positions[key]={i:lex.union_spans(spans) for i,spans in hits.items()}
        counts[key]=len(hits); times[key]=sum(len(s) for s in positions[key].values())
        dates[key]={rows[i]['day'] for i in hits}; labels[key]=group['name']
        if hits:
            i=next(iter(hits)); examples[key]=dict(username=rows[i]['username'],message_id=rows[i]['message_id'],snippet=texts[i][:180])
        group_items.append(dict(term=key,display=group['name'],members=members))
    active = words | phrases | new_terms | {g['term'] for g in group_items}
    return dict(counts=counts,times=times,positions=positions,dates=dates,examples=examples,
                words=words,phrases=phrases,discovered=discovered,groups=group_items,
                labels=labels,variants=variants,vocabulary=active,options=options)


def content_comparison(current, previous, a, b, anchor=''):
    n,p=a['text_messages'],b['text_messages']; result=[]
    if n>=5 and p>=5:
        for term in current['vocabulary'] | previous['vocabulary']:
            x,y=current['counts'][term],previous['counts'][term]
            # 同一字面候选可能只在一侧被生成，另一侧仍按统一跨度匹配补齐。
            if x+y<3 or x/n==y/p: continue
            pooled=(x+y)/(n+p); score=0
            for hits,size in ((x,n),(y,p)):
                for observed,expected in ((hits,size*pooled),(size-hits,size*(1-pooled))):
                    if observed and expected: score+=2*observed*math.log(observed/expected)
            result.append(dict(term=term,display=current['labels'].get(term,previous['labels'].get(term,term)),
                               current=x,previous=y,current_total=n,previous_total=p,current_ratio=x/n,previous_ratio=y/p,
                               percentage_points=100*(x/n-y/p),score=score,previous_unobserved=y==0))
    result.sort(key=lambda r:(r['term']!=anchor.lower(),-r['score'],-max(r['current'],r['previous']),r['term']))
    return dict(items=result[:30],sufficient=n>=5 and p>=5,current_total=n,previous_total=p,minimum_mentions=3)


def content_stats(rows, rules, include_transcripts, multiple, anchor='', index=None):
    index = index if index is not None else content_index(rows,rules,include_transcripts)
    counts=index['counts']; positions=index['positions']
    def item(t):
        return dict(term=t,display=index['labels'].get(t,t),count=counts[t],occurrences=index['times'][t],
                    days=len(index['dates'][t]),variants=index['variants'].get(t,[]),
                    example=index['examples'].get(t),daily=[dict(day=d,count=n) for d,n in sorted(Counter(rows[i]['day'] for i in positions[t]).items(),reverse=True)])
    def ranked_terms(terms,minimum=1):
        return [t for t in sorted(terms,key=lambda t:(-counts[t],-(len(index['dates'].get(t,())) if counts[t]>1 else counts[t]),t)) if counts[t]>=minimum]
    raw_phrases=[dict(term=t,count=counts[t]) for t in ranked_terms(index['phrases'],index['options']['min_phrase_messages'])]
    displayed=lex.nested_groups(raw_phrases,positions) if index['options']['deduplicate'] else raw_phrases
    phrase_items=[dict(item(x['term']),nested=[item(n['term']) for n in x.get('nested',[])]) for x in displayed[:60]]
    anchor=anchor.lower()
    anchor_positions=positions.get(anchor)
    if anchor_positions is None:
        anchor_positions={i:s for i,m in enumerate(rows) if (s:=lex.expression_spans(text_for(m,include_transcripts),anchor,m.get('_analysis')))} if anchor else {}
    associations=[]
    for term in index['vocabulary']:
        if term==anchor: continue
        term_positions=positions.get(term,{})
        joint=sum(independent_occurrences(anchor_positions[i],term_positions[i]) for i in anchor_positions.keys() & term_positions.keys())
        if joint>=2:
            dice=2*joint/(len(anchor_positions)+counts[term])
            associations.append(dict(term=term,display=index['labels'].get(term,term),terms=[anchor,term],count=joint,
                                     anchor_count=len(anchor_positions),term_count=counts[term],dice=dice))
    associations.sort(key=lambda r:(-r['dice']*math.log1p(r['count']),-r['count'],r['term']))
    return dict(words=[item(t) for t in ranked_terms(index['words'])[:60]],phrases=phrase_items,raw_phrase_total=len(raw_phrases),
                discovered=[dict(item(x['term']),**{k:v for k,v in x.items() if k!='term'}) for x in index['discovered']],
                groups=[dict(item(g['term']),members=g['members']) for g in index['groups']],
                associations=associations[:30],anchor=anchor,anchor_count=len(anchor_positions),
                candidate_total=len(index['vocabulary']),options=index['options'],
                text_messages=sum(bool(text_for(m,include_transcripts).strip()) for m in rows))


def representative(rows, rules, include):
    # 原文能否展示与候选入榜完全分开，包括短回复、数字及仅含结构化文本的消息。
    distinct={}
    for m in rows:
        text=text_for(m,include).strip()
        if text and (text not in distinct or m['timestamp']<distinct[text]['timestamp']):distinct[text]=m
    ranked_rows=[(min(len(text),160)/40,m) for text,m in distinct.items()]
    chosen,seen=[],set()
    while ranked_rows and len(chosen)<3:
        def priority(pair):
            score,m=pair
            diversity=sum(x['sender_key']!=m['sender_key'] for x in chosen)
            diversity+=sum(x['username']!=m['username'] or abs(x['timestamp']-m['timestamp'])>1800 for x in chosen)
            return score+diversity,-m['timestamp']
        best=max(ranked_rows,key=priority); ranked_rows.remove(best); m=best[1]
        text=text_for(m,include).strip()
        if text not in seen: seen.add(text); chosen.append(m)
    return sorted(chosen,key=lambda m:m['timestamp'])


def analysis_context(c):
    config={k:json.loads(v) for k,v in c.execute("SELECT key,value FROM settings WHERE key IN ('analysis_options','analysis_aliases','analysis_blocklist')")}
    return dict(options=lex.options_for(config),aliases=config.get('analysis_aliases',[]),
                rules=config.get('analysis_blocklist',DEFAULT_BLOCKLIST))


def fill_comparison_counts(index, terms, rows, include):
    missing=terms-index['counts'].keys()
    if not missing or not rows:
        index['counts'].update({t:0 for t in missing})
        return
    matcher=lex.LiteralMatcher(t for t in missing if not t.startswith('@alias/'))
    counts=Counter()
    for m in rows:
        counts.update(matcher.spans(text_for(m,include),index['options']['ignore_structured']).keys())
    for term in missing:
        index['counts'][term]=(sum(bool(lex.expression_spans(text_for(m,include),term,m.get('_analysis'))) for m in rows)
                               if term.startswith('@alias/') else counts[term])


def public_message(m):
    return {k:v for k,v in m.items() if not k.startswith('_')}




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
        context=analysis_context(c); rules=context['rules']
        self_row=c.execute("SELECT value FROM settings WHERE key='self_username'").fetchone()
        me=json.loads(self_row[0]) if self_row else None
        for m in rows: m['_analysis']=context
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
        previous, previous_current = None, []
        if cs is not None and ce is not None:
            if cs > ce:
                raise ValueError('对比开始日期不能晚于结束日期')
            previous_rows, _ = load_rows(archive, c, usernames, cs, ce, q, include)
            for m in previous_rows: m['_analysis']=context
            # 对比继承分类和内容条件；图中选择的绝对日期不强加到另一时段。
            comparable = {k: v for k, v in f.items() if k not in ('start', 'end')}
            previous_current = [m for m in previous_rows if matches(m, comparable, include)]
            previous = summary(previous_current, cs, ce)
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
    mine = sum(n for k, n in senders.items() if k == me) if me else None
    sessions = []
    for username, group in itertools.groupby(rows, lambda m: m['username']):
        segment = []
        def flush():
            hits = [m for m in segment if matches(m, f, include)]
            if not hits:
                return
            evidence = representative(hits, rules, include)
            clue_counts=Counter(t for m in hits for t in candidates(m,rules,include)[0])
            sessions.append(dict(username=username, name=contacts[username]['name'],
                start=segment[0]['timestamp'], end=segment[-1]['timestamp'],
                total=len(segment), count=len(hits), participants=len({m['sender_key'] for m in segment if m['kind'] != 10000}),
                first_id=(evidence or hits)[0]['message_id'],
                snippet=text_for(evidence[0], include)[:180] if evidence else '本片段没有文字（媒体消息仍保留）',
                clues=[t for t,_ in clue_counts.most_common(5)],
                evidence=[dict(username=m['username'], message_id=m['message_id'], sender=m.get('sender'),
                               time=m.get('time'), snippet=text_for(m, include)[:240]) for m in evidence]))
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
    current_index = content_index(current, rules, include, context)
    previous_index = content_index(previous_current, rules, include, context)
    all_terms=current_index['vocabulary'] | previous_index['vocabulary']
    fill_comparison_counts(current_index,all_terms,current,include)
    fill_comparison_counts(previous_index,all_terms,previous_current,include)
    content = content_stats(current, rules, include, len(usernames) > 1, values.get('anchor', ''), current_index)
    previous_content = content_stats(previous_current, rules, include, len(usernames) > 1, index=previous_index)
    content['comparison'] = dict(content_comparison(current_index, previous_index, content, previous_content, values.get('anchor', '')),
                                 start=cs, end=ce, current_start=effective_start, current_end=effective_end)
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
        blocklist_count=len(rules), analysis_settings=context,
        query=dict(usernames=usernames, q=q, start=start, end=end, filters=f))


def analysis_messages(archive, req):
    v = req.model_dump()
    with closing(archive.connect()) as c:
        c.execute('BEGIN')
        rows, _ = load_rows(archive, c, v['usernames'], v.get('start'), v.get('end'),
                            v.get('q', ''), v.get('include_transcripts', True))
        context=analysis_context(c); rules=context['rules']
        for m in rows: m['_analysis']=context
    rows = [m for m in rows if matches(m, v.get('filters') or {}, v.get('include_transcripts', True))
            and matches(m, v.get('evidence_filters') or {}, v.get('include_transcripts', True))]
    rows.sort(key=lambda m: (m['timestamp'], m.get('sort_seq', 0), m.get('local_id', 0),
                             m['message_id'], m['username']), reverse=True)
    messages = rows[v['offset']:v['offset']+50]
    samples = representative(rows, rules, v.get('include_transcripts', True))
    terms=(v.get('filters') or {}).get('terms',[])+(v.get('evidence_filters') or {}).get('terms',[])
    highlight_terms=[]
    for term in terms:
        group=next((g for g in context['aliases'] if '@alias/'+g['id']==term and g['enabled']),None)
        highlight_terms.extend([t for t in group['members'] if lex.allowed(t,rules)] if group else [term])
    def evidence_message(m):
        spans=lex.union_spans([s for term in terms for s in lex.expression_spans(text_for(m,v.get('include_transcripts',True)),term,context)])
        return dict(public_message(m),highlights=spans)
    return dict(total=len(rows),messages=[evidence_message(m) for m in messages],samples=[evidence_message(m) for m in samples],
                highlight_terms=highlight_terms,
                evidence_state='no_matches' if not rows else 'no_text' if not any(text_for(m,v.get('include_transcripts',True)).strip() for m in rows) else 'selected' if samples else 'no_representative')
