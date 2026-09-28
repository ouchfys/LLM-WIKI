import sqlite3

from system.wiki.paper_pipeline.store import PaperWikiPipelineStore


def test_reader_resolves_exact_packet_provenance_without_linking_unrelated_pages(tmp_path):
    db_path = str(tmp_path / 'reader.db')
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE wiki_pages (id TEXT PRIMARY KEY, title TEXT, page_type TEXT)')
        conn.executemany('INSERT INTO wiki_pages VALUES (?, ?, ?)', [
            ('topic', 'Memory', 'TopicPage'),
            ('paper', 'Beyond Scaling', 'PaperPage'),
            ('other', 'Another Memory Paper', 'PaperPage'),
            ('explicit', 'Second source', 'PaperPage'),
        ])
    store = PaperWikiPipelineStore(db_path)
    store.add_card_source('topic', 'packet-a')
    store.add_card_source('topic', 'packet-a', section_id='duplicate-evidence')
    store.add_card_source('paper', 'packet-a')
    store.add_card_source('other', 'packet-b')
    for card_id in ('topic', 'paper', 'other'):
        store.add_card_source(card_id, 'markdown')
    store.add_card_source('topic', '', source_card_id='explicit')
    assert store.list_card_links('topic')['source_papers'] == [
        {'id': 'paper', 'title': 'Beyond Scaling'},
        {'id': 'explicit', 'title': 'Second source'},
    ]
    assert store.list_card_links('paper')['source_papers'] == []
