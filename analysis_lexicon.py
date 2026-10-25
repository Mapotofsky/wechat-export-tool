"""有原文位置的字面候选；不改全局词典，不推断语义等价。"""
from collections import Counter, defaultdict, deque
import math
import re

DEFAULT_OPTIONS = dict(content_only=False, hide_numbers=False, ignore_structured=True,
                       deduplicate=True, discover=True, complete_phrases=True, min_phrase_messages=2)
NOISE = re.compile(r'https?://[^\s<>"\u4e00-\u9fff，。！？；、]+|(?<![A-Za-z0-9_])wxid_[A-Za-z0-9_]+', re.I)
PART = re.compile(r'[\u4e00-\u9fff]+|[A-Za-z][A-Za-z0-9]*(?:(?:[-_.][A-Za-z0-9]+)|\+\+|#)*|[¥￥$]?[+-]?\d+(?:[.,:/-]\d+)*%?')
NEGATION = re.compile(r'不|没|未|无|非')


def options_for(config):
    return {**DEFAULT_OPTIONS, **config.get('analysis_options', {})}


def allowed(term, rules):
    term = term.lower()
    return not any((term == r['text'].lower() if r['mode'] == 'exact' else r['text'].lower() in term) for r in rules)


def normalize_aliases(groups):
    result, ids, names, owners = [], set(), set(), {}
    for group in groups:
        key, name = group['id'].strip().lower(), group['name'].strip()
        members = list(dict.fromkeys(t.strip() for t in group['members'] if t.strip()))
        if len({t.lower() for t in members}) != len(members):
            raise ValueError('同一别名组的表达忽略英文大小写后不能重复')
        if not key or not name or not members:
            raise ValueError('别名组需要稳定编号、组名和至少一个表达')
        if key in ids or name.lower() in names:
            raise ValueError('别名组编号和名称不能重复')
        ids.add(key); names.add(name.lower())
        if group.get('enabled', True):
            for term in members:
                normalized = term.lower()
                if normalized in owners and owners[normalized] != key:
                    raise ValueError('同一表达不能同时属于两个启用的别名组：' + term)
                owners[normalized] = key
        result.append(dict(id=key, name=name, members=members, enabled=group.get('enabled', True)))
    return result


def scan(text, ignore_structured=True):
    """返回连续块及 token(word, POS, start, end)，位置均指向原始字符串。"""
    import jieba.posseg as posseg
    ignored = [m.span() for m in NOISE.finditer(text)] if ignore_structured else []
    blocks, current, last = [], [], -1
    for match in PART.finditer(text):
        a, b = match.span()
        if any(a < y and b > x for x, y in ignored):
            if current: blocks.append(current); current = []
            last = -1
            continue
        if a != last and current:
            blocks.append(current); current = []
        source = match.group()
        if re.fullmatch(r'[\u4e00-\u9fff]+', source):
            offset = a
            for word, tag in posseg.cut(source, HMM=True):
                current.append((word, tag, offset, offset+len(word)))
                offset += len(word)
        else:
            current.append((source, 'eng' if re.search('[A-Za-z]',source) else 'm', a, b))
        last = b
    if current: blocks.append(current)
    return blocks


def literal_spans(text, term, ignore_structured=True):
    """英文/数字两端不嵌入另一字母数字串；中文按原文字面匹配。"""
    if not term: return []
    ignored = [m.span() for m in NOISE.finditer(text)] if ignore_structured else []
    lower, target = text.lower(), term.lower()
    result, start = [], 0
    while True:
        a = lower.find(target, start)
        if a < 0: break
        b = a + len(target); start = a + 1
        if any(a < y and b > x for x,y in ignored): continue
        if target[0].isascii() and target[0].isalnum() and a and re.match(r'[A-Za-z0-9_]', text[a-1]): continue
        if target[-1].isascii() and target[-1].isalnum() and b < len(text) and re.match(r'[A-Za-z0-9_+#]', text[b]): continue
        if target[0].isascii() and a>1 and text[a-1] in '-.' and text[a-2].isascii() and text[a-2].isalnum(): continue
        if target[-1].isascii() and b+1<len(text) and text[b] in '-._' and text[b+1].isascii() and text[b+1].isalnum(): continue
        if b < len(text) and text[b] in '+#' and target[-1].isascii(): continue
        result.append((a,b))
    return result


class LiteralMatcher:
    """请求内多模式字面匹配；一次扫描正文，保留全部重叠位置与原匹配边界。"""
    def __init__(self, terms):
        self.edges=[{}]; self.fail=[0]; self.outputs=[[]]
        for term in sorted(set(terms)):
            if not term: continue
            state=0
            for char in term:
                if char not in self.edges[state]:
                    self.edges[state][char]=len(self.edges)
                    self.edges.append({});self.fail.append(0);self.outputs.append([])
                state=self.edges[state][char]
            self.outputs[state].append(term)
        queue=deque(self.edges[0].values())
        while queue:
            state=queue.popleft()
            for char,child in self.edges[state].items():
                queue.append(child);fallback=self.fail[state]
                while fallback and char not in self.edges[fallback]:fallback=self.fail[fallback]
                self.fail[child]=self.edges[fallback].get(char,0)
                self.outputs[child].extend(self.outputs[self.fail[child]])

    def spans(self,text,ignore_structured=True):
        ignored=[m.span() for m in NOISE.finditer(text)] if ignore_structured else []
        result=defaultdict(list);state=0
        for i,char in enumerate(text.lower()):
            while state and char not in self.edges[state]:state=self.fail[state]
            state=self.edges[state].get(char,0)
            for term in self.outputs[state]:
                a,b=i+1-len(term),i+1
                if any(a<y and b>x for x,y in ignored):continue
                if term[0].isascii() and term[0].isalnum() and a and re.match(r'[A-Za-z0-9_]',text[a-1]):continue
                if term[-1].isascii() and term[-1].isalnum() and b<len(text) and re.match(r'[A-Za-z0-9_+#]',text[b]):continue
                if term[0].isascii() and a>1 and text[a-1] in '-.' and text[a-2].isascii() and text[a-2].isalnum():continue
                if term[-1].isascii() and b+1<len(text) and text[b] in '-._' and text[b+1].isascii() and text[b+1].isalnum():continue
                if b<len(text) and text[b] in '+#' and term[-1].isascii():continue
                result[term].append((a,b))
        return result


def expression_spans(text, term, context=None):
    context = context or {}
    ignore = context.get('options', DEFAULT_OPTIONS)['ignore_structured']
    group = next((g for g in context.get('aliases', []) if '@alias/'+g['id'] == term and g['enabled']), None)
    if term.startswith('@alias/'):
        return union_spans([span for member in (group or {}).get('members', [])
                            if allowed(member, context.get('rules', []))
                            for span in literal_spans(text, member, ignore)])
    return literal_spans(text, term, ignore)


def union_spans(spans):
    """同处重叠别名计一次；相邻但不重叠的两处仍分别计数。"""
    result = []
    for a,b in sorted(set(spans)):
        if result and a < result[-1][1]:
            result[-1] = (result[-1][0], max(b,result[-1][1]))
        else: result.append((a,b))
    return result


def token_candidates(blocks, options, rules):
    words, phrases, labels = set(), set(), {}
    def visible(part, phrase=False):
        text = ''.join(t[0] for t in part)
        if not allowed(text,rules): return False
        if options['hide_numbers'] and all(t[1] == 'm' for t in part): return False
        if phrase and options['complete_phrases'] and part[-1][1].startswith(('u','c','p','d')):
            return False
        if options['content_only']:
            content = any(t[1].startswith(('n','v','a','j','i','eng')) or
                          (t[1]=='m' and not options['hide_numbers']) for t in part)
            if not content: return False
            if phrase and part[-1][1].startswith(('u','c','p')): return False
        return True
    for block in blocks:
        for i, token in enumerate(block):
            if visible([token]): words.add(token[0].lower()); labels.setdefault(token[0].lower(),token[0])
            for size in (2,3,4,5,6):
                part = block[i:i+size]
                if len(part) != size: continue
                text = ''.join(t[0] for t in part)
                if len(text) <= 32 and visible(part,True):
                    phrases.add(text.lower()); labels.setdefault(text.lower(),text)
    return words, phrases, labels


def discover(texts, blocks_list, rules, min_cohesion=1.5, min_boundary=1.0):
    """去重复消息后计算字符 ngram 的支持、最弱切分 PMI 与双侧边界熵。"""
    counts, support = Counter(), Counter()
    base_words, seen, total = set(), set(), 0
    sources=[]
    for row, (text,blocks) in enumerate(zip(texts,blocks_list)):
        signature=tuple(''.join(t[0].lower() for t in block) for block in blocks)
        if signature in seen: continue
        seen.add(signature)
        base_words.update(t[0] for block in blocks for t in block)
        supported=set()
        for block in blocks:
            for match in re.finditer(r'[\u4e00-\u9fff]+', text[block[0][2]:block[-1][3]]):
                source=match.group(); total+=len(source)
                base=block[0][2]+match.start()
                starts={t[2]-base for t in block}; ends={t[3]-base for t in block}
                sources.append((source,starts,ends))
                for a in range(len(source)):
                    for size in range(1,min(8,len(source)-a)+1):
                        b=a+size; term=source[a:b]; counts[term]+=1
                        if size >= 2 and a in starts and b in ends:
                            supported.add(term)
        support.update(supported)
    eligible={term for term,n in support.items() if n>=3 and term not in base_words and allowed(term,rules)}
    lefts,rights=defaultdict(Counter),defaultdict(Counter)
    for source,starts,ends in sources:
        for a in starts:
            if not 0<=a<len(source):continue
            for size in range(2,min(8,len(source)-a)+1):
                b=a+size;term=source[a:b]
                if b in ends and term in eligible:
                    lefts[term][source[a-1] if a else '^']+=1
                    rights[term][source[b] if b<len(source) else '$']+=1
    def entropy(counter):
        n=sum(counter.values())
        return -sum((v/n)*math.log2(v/n) for v in counter.values())
    result=[]
    for term in eligible:
        cohesion=min(math.log2(counts[term]*max(1,total)/(counts[term[:i]]*counts[term[i:]])) for i in range(1,len(term)))
        boundary=min(entropy(lefts[term]),entropy(rights[term]))
        if cohesion>=min_cohesion and boundary>=min_boundary:
            result.append(dict(term=term,cohesion=round(cohesion,3),boundary_entropy=round(boundary,3),
                               distinct_text_support=support[term]))
    result.sort(key=lambda r: (-r['distinct_text_support']*r['boundary_entropy']*r['cohesion'],r['term']))
    return result[:40]


def nested_groups(items, positions):
    """只组织同消息集合、逐处一对一包含的位置证据，不删除候选。"""
    buckets=defaultdict(list)
    for item in items:
        if item['count']>=2: buckets[tuple(positions[item['term']])].append(item)
    hidden=set(); groups={}
    def equivalent(a,b):
        if NEGATION.findall(a) != NEGATION.findall(b): return False
        pa,pb=positions[a],positions[b]
        for row in pa:
            x,y=pa[row],pb[row]
            if len(x)!=len(y): return False
            if not all((u<=s and e<=v) or (s<=u and v<=e) for (s,e),(u,v) in zip(x,y)): return False
        return True
    for bucket in buckets.values():
        if len(bucket)<2:continue
        # 偏好边界完整且适中的表达；不是长度排序，长短原项均可展开。
        def quality(item):
            tokens=[t for block in scan(item['term'],False) for t in block]
            incomplete=(tokens[0][1].startswith(('u','c','p')) + tokens[-1][1].startswith(('u','c','p','d','r'))) if tokens else 2
            return incomplete,abs(len(item['term'])-6),-item.get('cohesion',0),item['term']
        ordered=sorted(bucket,key=quality)
        for item in ordered:
            a=item['term']
            if a in hidden: continue
            for other in ordered:
                b=other['term']
                if b==a or b in hidden or b in groups: continue
                if equivalent(a,b):
                    groups.setdefault(a,[]).append(other); hidden.add(b)
    return [dict(item,nested=groups.get(item['term'],[])) for item in items if item['term'] not in hidden]
