from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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


def _source_url_for_pdf(path: Path) -> str:
    metadata_path = path.with_suffix('.json')
    if not metadata_path.exists():
        return ''
    try:
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return ''
    return str(metadata.get('abs_url') or metadata.get('source_url') or '').strip()


def main() -> int:
    parser = argparse.ArgumentParser(description='Batch ingest the paper test corpus with the Wiki compiler workflow.')
    parser.add_argument('--corpus-dir', default=str(DEFAULT_CORPUS))
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--pattern', default='*.pdf')
    parser.add_argument('--exclude', action='append', default=[], help='Case-insensitive filename substring to skip. Can be repeated.')
    parser.add_argument('--continue-on-error', action='store_true')
    parser.add_argument(
        '--max-consecutive-failures',
        type=int,
        default=0,
        help='Stop cleanly after this many consecutive attempted-paper failures; 0 disables the circuit breaker.',
    )
    parser.add_argument('--no-maintenance', action='store_true')
    parser.add_argument('--skip-existing', action='store_true', help='Skip PDFs whose sha256 already exists in source_packets.')
    parser.add_argument('--db-path', default='', help='Explicit SQLite target. Never copy sessions.db for an eval run.')
    parser.add_argument('--workspace-root', default='', help='Isolated root for wiki/source/query/maintenance artifacts.')
    parser.add_argument('--runs-dir', default='', help='Directory for batch summaries; defaults inside the isolated workspace.')
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root).resolve() if args.workspace_root else None
    if workspace_root:
        workspace_root.mkdir(parents=True, exist_ok=True)
        os.environ['PAPERWIKI_WORKSPACE_ROOT'] = str(workspace_root)
        os.environ['PAPERWIKI_MEMORY_ROOT'] = str(workspace_root / '.paperwiki' / 'memory')
        os.environ['STORAGE_BACKEND'] = 'local'
        os.environ['STORAGE_TENANT_ID'] = 'paperwiki-agent-eval-v1'
        os.environ['STORAGE_ROOT_PREFIX'] = 'users/paperwiki-agent-eval-v1'

    # Imports stay below the environment fence so config and storage singletons
    # are born inside the requested evaluation workspace.
    from backend.api.papers import _run_ingestion_job
    from system.agent_runtime import AgentRunStore
    from system.conversation.session_store import SessionStore
    from system.wiki.maintenance.runner import WikiMaintenanceRunner
    from system.wiki.ingestion_jobs import IngestionJobStore

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
    configured_runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else (
        workspace_root / 'runs' / 'paper_ingestion' if workspace_root else RUNS_DIR
    )
    run_dir = configured_runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.db_path:
        db_path = str(Path(args.db_path).resolve())
    elif workspace_root:
        db_path = str(workspace_root / 'paperwiki_agent_eval_v1.db')
    else:
        db_path = SessionStore().db_path
    if workspace_root and Path(db_path).resolve().parent != workspace_root:
        raise SystemExit('--db-path must be directly inside --workspace-root for an isolated eval run')
    os.environ['PAPERWIKI_DB_PATH'] = db_path
    SessionStore(db_path=db_path, memory_root=str((workspace_root or Path(db_path).parent) / '.paperwiki' / 'memory'))
    jobs = IngestionJobStore(db_path=db_path)
    runtime = AgentRunStore(db_path=db_path)
    pipeline_store = None
    existing_hashes: set[str] = set()
    if args.skip_existing:
        from system.wiki.paper_pipeline.store import PaperWikiPipelineStore

        pipeline_store = PaperWikiPipelineStore(db_path=db_path)
        existing_hashes = _existing_completed_source_hashes(pipeline_store)

    rows = []
    consecutive_failures = 0
    circuit_breaker_tripped = False
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
            source_url = _source_url_for_pdf(pdf)
            job = jobs.create_job(
                source_type="paper_pdf", source_uri=str(pdf.resolve()), stage="queued",
                metadata={"filename": pdf.name, "batch_run_id": run_id, "source_url": source_url},
            )
            run = runtime.create_run(
                run_type="paper_ingestion", source_uri=str(pdf.resolve()),
                approval_mode="auto", ingestion_job_id=str(job["id"]),
                context={
                    "job_id": job["id"], "pdf_path": str(pdf.resolve()),
                    "source_url": source_url, "pipeline": "wiki_compile",
                    "batch_run_id": run_id,
                },
            )
            jobs.merge_metadata(
                str(job["id"]), {"agent_run_id": run["id"], "approval_mode": "auto"}
            )
            _run_ingestion_job(
                db_path=db_path, job_id=str(job["id"]), pdf_path=str(pdf.resolve()),
                source_url=source_url, pipeline="wiki_compile", run_id=str(run["id"]),
                approval_mode="auto", run_maintenance=False,
            )
            finished_run = runtime.get_run(str(run["id"])) or {}
            finished_job = jobs.get_job(str(job["id"])) or {}
            pipeline_ok = finished_run.get("current_state") == "COMPLETED"
            row = dict(finished_run.get("result") or {})
            row.update({
                'pdf': _display_path(pdf),
                'agent_run_id': run["id"],
                'ingestion_job_id': job["id"],
                'agent_state': finished_run.get("current_state", ""),
                'job_stage': finished_job.get("stage", ""),
                'elapsed_seconds': round(time.perf_counter() - item_started, 2),
            })
            row['ok'] = pipeline_ok
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
        print(f"  done ok={row.get('ok')} state={row.get('agent_state','')} parser={row.get('parser','')} elapsed={row.get('elapsed_seconds')}s", flush=True)
        if row.get('ok'):
            consecutive_failures = 0
        else:
            consecutive_failures += 1
        if args.max_consecutive_failures > 0 and consecutive_failures >= args.max_consecutive_failures:
            circuit_breaker_tripped = True
            print(
                f'stopping after {consecutive_failures} consecutive failures '
                '(batch circuit breaker)',
                flush=True,
            )
            break

    maintenance = {}
    if not args.no_maintenance:
        print('running maintenance...', flush=True)
        maintenance = WikiMaintenanceRunner(db_path=db_path).run_once(
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
        'circuit_breaker_tripped': circuit_breaker_tripped,
        'consecutive_failures': consecutive_failures,
    }
    (run_dir / 'details.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    (run_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary['failed_count'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
