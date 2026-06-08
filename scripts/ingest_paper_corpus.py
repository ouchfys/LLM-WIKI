from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.deps import get_chunk_index, get_merge_llm, get_review_llm, get_summary_llm
from system.paper_index.store import PaperIndexStore
from system.wiki.maintenance.runner import WikiMaintenanceRunner
from system.wiki.paper_pipeline import run_paper_pipeline
from system.wiki.wiki_store import WikiStore


DEFAULT_CORPUS = REPO_ROOT / 'sources' / 'papers' / 'originals'
RUNS_DIR = REPO_ROOT / 'test' / 'paper_ingestion_runs'


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_completed_source_hashes(store) -> set[str]:
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT sp.source_hash
            FROM source_packets sp
            JOIN wiki_card_sources wcs ON wcs.source_packet_id = sp.id
            WHERE sp.source_hash != ''
            """
        ).fetchall()
    return {row['source_hash'] for row in rows}


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description='Batch ingest the paper test corpus with the four-agent pipeline.')
    parser.add_argument('--corpus-dir', default=str(DEFAULT_CORPUS))
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--pattern', default='*.pdf')
    parser.add_argument('--exclude', action='append', default=[], help='Case-insensitive filename substring to skip. Can be repeated.')
    parser.add_argument('--continue-on-error', action='store_true')
    parser.add_argument('--no-maintenance', action='store_true')
    parser.add_argument('--skip-existing', action='store_true', help='Skip PDFs whose sha256 already exists in source_packets.')
    args = parser.parse_args()

    corpus_dir = Path(args.corpus_dir)
    if not corpus_dir.is_absolute():
        corpus_dir = REPO_ROOT / corpus_dir
    pdfs = sorted(corpus_dir.glob(args.pattern), key=lambda p: p.name.lower())
    excludes = [item.lower() for item in args.exclude if item]
    if excludes:
        pdfs = [pdf for pdf in pdfs if not any(item in pdf.name.lower() for item in excludes)]
    if args.limit > 0:
        pdfs = pdfs[:args.limit]
    if not pdfs:
        raise SystemExit(f'No PDFs found in {corpus_dir}')

    run_id = time.strftime('paper_ingestion_%Y%m%d_%H%M%S')
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    paper_store = PaperIndexStore()
    wiki_store = WikiStore()
    pipeline_store = None
    existing_hashes: set[str] = set()
    if args.skip_existing:
        from system.wiki.paper_pipeline.store import PaperWikiPipelineStore

        pipeline_store = PaperWikiPipelineStore(db_path=wiki_store.db_path)
        existing_hashes = _existing_completed_source_hashes(pipeline_store)
    chunk_index = get_chunk_index()
    summary_llm = get_summary_llm()
    review_llm = get_review_llm()
    merge_llm = get_merge_llm()

    rows = []
    started = time.perf_counter()
    for index, pdf in enumerate(pdfs, start=1):
        item_started = time.perf_counter()
        if args.skip_existing:
            source_hash = _file_sha256(pdf)
            if source_hash in existing_hashes:
                row = {
                    'ok': True,
                    'skipped': True,
                    'reason': 'source_hash_exists',
                    'pdf': _display_path(pdf),
                    'elapsed_seconds': round(time.perf_counter() - item_started, 2),
                }
                rows.append(row)
                print(f'[{index}/{len(pdfs)}] skipping existing {pdf.name}', flush=True)
                continue
        print(f'[{index}/{len(pdfs)}] ingesting {pdf.name}', flush=True)
        try:
            result = run_paper_pipeline(
                pdf_path=pdf,
                source_url='',
                paper_store=paper_store,
                wiki_store=wiki_store,
                chunk_index=chunk_index,
                llm=summary_llm,
                review_llm=review_llm,
                merge_llm=merge_llm,
            )
            row = result.model_dump() if hasattr(result, 'model_dump') else result.dict()
            row.update({
                'ok': True,
                'pdf': _display_path(pdf),
                'elapsed_seconds': round(time.perf_counter() - item_started, 2),
            })
            if args.skip_existing and row.get('source_packet_id') and pipeline_store:
                existing_hashes = _existing_completed_source_hashes(pipeline_store)
        except Exception as exc:
            row = {
                'ok': False,
                'pdf': _display_path(pdf),
                'error': str(exc),
                'traceback': traceback.format_exc(),
                'elapsed_seconds': round(time.perf_counter() - item_started, 2),
            }
            print(f'  FAILED: {exc}', flush=True)
            if not args.continue_on_error:
                rows.append(row)
                break
        rows.append(row)
        print(f"  done ok={row.get('ok')} parser={row.get('parser','')} elapsed={row.get('elapsed_seconds')}s", flush=True)

    maintenance = {}
    if not args.no_maintenance:
        print('running maintenance...', flush=True)
        maintenance = WikiMaintenanceRunner().run_once(
            check_storage=False,
            create_repair_tasks=True,
            process_deterministic_repairs=True,
            process_llm_repairs=False,
            distill_query_insights=False,
            process_candidates=True,
            process_web_sources=False,
            generate_indices=True,
            upload_indices=True,
        )

    summary = {
        'run_id': run_id,
        'corpus_dir': str(corpus_dir),
        'count': len(rows),
        'ok_count': sum(1 for row in rows if row.get('ok')),
        'skipped_count': sum(1 for row in rows if row.get('skipped')),
        'failed_count': sum(1 for row in rows if not row.get('ok')),
        'elapsed_seconds': round(time.perf_counter() - started, 2),
        'maintenance_ok': bool(maintenance.get('ok')) if maintenance else None,
        'maintenance_run_id': maintenance.get('run_id', '') if maintenance else '',
    }
    (run_dir / 'details.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    (run_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary['failed_count'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
