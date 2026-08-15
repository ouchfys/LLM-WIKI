"""Paper-to-Wiki compiler pipeline.

The public entry point is lazy so importing data models does not initialize the
entire orchestrator (and its merger/revision dependency graph).
"""


def run_paper_pipeline(*args, **kwargs):
    from system.wiki.paper_pipeline.orchestrator import run_paper_pipeline as _run

    return _run(*args, **kwargs)

__all__ = ["run_paper_pipeline"]
