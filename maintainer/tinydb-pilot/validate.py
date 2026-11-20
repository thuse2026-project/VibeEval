"""Validate a pinned TinyDB candidate; mutations only exist in temporary copies."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PROBE = r'''
import json
from pathlib import Path
import tempfile
from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage

results = {}
def check(name, fn):
    try:
        fn()
        results[name] = {"passed": True}
    except Exception as exc:
        results[name] = {"passed": False, "error": type(exc).__name__ + ': ' + str(exc)}

def cache(storage):
    with tempfile.TemporaryDirectory() as d:
        db = TinyDB(storage=MemoryStorage) if storage == 'memory' else TinyDB(Path(d)/'data.json')
        with db:
            db.insert({'done': False})
            cond = Query().done == False
            assert len(db.search(cond)) == 1
            db.update({'done': True}, cond)
            actual = len(db.search(cond))
            assert actual == 0, f'expected 0 pending documents, got {actual}'

def ids():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)/'data.json'
        with TinyDB(path) as db:
            assert db.insert_multiple({'n': n} for n in range(10)) == list(range(1, 11))
        with TinyDB(path) as db:
            assert db.insert({'n': 10}) == 11

def normal():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)/'data.json'
        with TinyDB(path) as db:
            db.table('users').insert({'name': 'Ada', 'age': 20})
            assert db.all() == []
            assert len(db.table('users').search(Query().age >= 18)) == 1
        with TinyDB(path) as db:
            assert db.table('users').all()[0]['name'] == 'Ada'

def query():
    with TinyDB(storage=MemoryStorage) as db:
        db.insert_multiple([{'name':'A','age':20}, {'name':'B','age':17}, {'name':'C'}])
        assert [x['name'] for x in db.search(Query().age >= 18)] == ['A']

check('cache_memory', lambda: cache('memory'))
check('cache_json', lambda: cache('json'))
check('reopen_id', ids)
check('normal_persistence_and_table_isolation', normal)
check('missing_field_query', query)
print(json.dumps(results))
'''

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    original = (source/'tinydb/table.py').read_text(encoding='utf-8')
    cache_old = '        # Clear the query cache, as the table contents have changed\n        self.clear_cache()'
    id_old = 'max_id = max(self.document_id_class(i) for i in table.keys())'
    assert original.count(cache_old) == original.count(id_old) == 1
    cache_new = '        # Mutation for maintainer validation only: cache invalidation omitted\n        pass'
    id_new = 'max_id = self.document_id_class(max(table.keys()))'
    variants = {
        'baseline': original,
        'cache_mutant': original.replace(cache_old, cache_new),
        'id_mutant': original.replace(id_old, id_new),
        'combined_mutant': original.replace(cache_old, cache_new).replace(id_old, id_new),
    }
    expected = {'baseline': set(), 'cache_mutant': {'cache_memory','cache_json'},
                'id_mutant': {'reopen_id'}, 'combined_mutant': {'cache_memory','cache_json','reopen_id'}}
    report = {'python': sys.version, 'source': str(source), 'variants': {}, 'source_sha256': {}}
    for p in sorted((source/'tinydb').glob('*.py')):
        report['source_sha256'][p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    for name, text in variants.items():
        ast.parse(text)
        with tempfile.TemporaryDirectory(prefix='tinydb-pilot-') as d:
            target = Path(d)
            shutil.copytree(source/'tinydb', target/'tinydb', ignore=shutil.ignore_patterns('__pycache__'))
            (target/'tinydb/table.py').write_text(text, encoding='utf-8')
            (target/'probe.py').write_text(PROBE, encoding='utf-8')
            run = subprocess.run([sys.executable, 'probe.py'], cwd=target, capture_output=True, text=True, check=True)
            result = json.loads(run.stdout)
            failed = {k for k, v in result.items() if not v['passed']}
            assert failed == expected[name], (name, result)
            report['variants'][name] = result
    assert (source/'tinydb/table.py').read_text(encoding='utf-8') == original
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: {t:v['passed'] for t,v in x.items()} for k,x in report['variants'].items()}, indent=2))

if __name__ == '__main__':
    main()
