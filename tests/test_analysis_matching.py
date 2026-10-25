"""批量匹配必须与逐表达回查一致，不能用截断范围换取速度。"""
import unittest
import tempfile
import sqlite3
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
import analysis_lexicon as lex
import archive_analysis as analysis


class MatchingTests(unittest.TestCase):
    def test_real_data_probe_connection_is_read_only(self):
        from archive_store import Archive
        from scripts.profile_analysis import ReadOnlyArchive
        with tempfile.TemporaryDirectory() as tmp:
            original=Archive(tmp)
            with closing(ReadOnlyArchive(Path(tmp)/'archive.sqlite').connect()) as c:
                self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0],1)
                with self.assertRaises(sqlite3.OperationalError):
                    c.execute("INSERT INTO settings VALUES ('probe','1')")
            self.assertEqual(original.config(),{})

    def test_status_does_not_repeat_contact_directory_or_change_saved_config(self):
        from maintenance import Manager
        with tempfile.TemporaryDirectory() as tmp:
            manager=Manager(tmp)
            try:
                manager.registry={'active':'a','accounts':[{'id':'a'}]}
                archive=manager.archive()
                archive.save_config({'contact_directory':[{'username':str(i)} for i in range(1000)],'self_username':'self'})
                status=manager.status()
                self.assertNotIn('contact_directory',status['config'])
                self.assertEqual(status['config']['self_username'],'self')
                self.assertEqual(len(archive.config()['contact_directory']),1000)
            finally:manager.executor.shutdown()

    def test_batch_matches_every_overlapping_literal_and_boundary(self):
        texts=['支持方案甲，但不支持方案乙。收到收到。',
               'C++ C# C GPT-4 GPT v1.2 v1 -1200 ￥1,200.50 API FF14',
               '微信WeChat WECHAT wechat2 微信平台 微信',
               '合同https://example.com/a?id=12报销 wxid_abc123 后续',
               '甲甲甲。甲乙甲，🙂收到。']
        terms={text[a:b].lower() for text in texts for a in range(len(text))
               for b in range(a+1,min(len(text),a+9)+1)}
        matcher=lex.LiteralMatcher(terms)
        for ignore in (True,False):
            for text in texts:
                expected={term:s for term in terms if (s:=lex.literal_spans(text,term,ignore))}
                self.assertEqual(dict(matcher.spans(text,ignore)),expected)

    def test_index_keeps_counts_without_per_candidate_full_scan(self):
        rows=[]
        for i in range(80):
            rows.append(dict(type=1,content=f'项目编号{10000+i}等待核对。收到。',day='2026-10-25',
                             username='a',message_id=str(i)))
        context=dict(options={**lex.DEFAULT_OPTIONS,'discover':False},aliases=[],rules=[])
        for m in rows:m['_analysis']=context
        with patch.object(lex,'literal_spans',side_effect=AssertionError('不应逐候选重扫全文')):
            index=analysis.content_index(rows,[],True,context)
            result=analysis.content_stats(rows,[],True,False,index=index)
        self.assertGreater(result['candidate_total'],80)
        self.assertEqual(index['counts']['收到'],80)
        self.assertNotIn('sets',index)
        self.assertTrue(all('_tokens' not in m for m in rows))

    def test_discovery_mixed_blocks_preserves_original_subranges(self):
        texts=['今天API星澜智研开始测试','请用API星澜智研处理样例',
               '新版API星澜智研已经就绪','介绍API星澜智研相关接口']
        found=lex.discover(texts,[lex.scan(t) for t in texts],[])
        self.assertTrue(all(any(x['term'] in text for text in texts) for x in found))


if __name__=='__main__':unittest.main()
