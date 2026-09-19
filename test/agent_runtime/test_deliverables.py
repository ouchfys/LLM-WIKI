import json

from system.agent_runtime.deliverables import declared_status, reader_report, write_deliverable_bundle


def test_reader_report_removes_control_plane_text():
    answer = "# 第 8 执行周期\n\n## 结论\n这是给读者的结论。\n<!-- PAPERWIKI_STATUS: DONE -->"

    report = reader_report(answer, title="KV Cache 研究")

    assert report.startswith("# KV Cache 研究")
    assert "第 8 执行周期" not in report
    assert "PAPERWIKI_STATUS" not in report
    assert declared_status(answer) == "done"


def test_deliverable_bundle_separates_report_evidence_and_audit(tmp_path):
    bundle = write_deliverable_bundle(
        tmp_path,
        answer="## 结论\n建议优先研究调度。\n<!-- PAPERWIKI_STATUS: CONTINUE -->",
        title="Research",
        task_key="kv_cache",
        citations=[{"card_id": "p1", "title": "Paper", "page_type": "PaperPage", "markdown_path": "papers/p1.md"}],
        audit={"cycle": 3, "job_id": "job-1"},
    )

    assert "job-1" not in bundle.report_path.read_text(encoding="utf-8")
    assert "papers/p1.md" in bundle.evidence_path.read_text(encoding="utf-8")
    audit = json.loads(bundle.audit_path.read_text(encoding="utf-8"))
    assert audit["job_id"] == "job-1"
    assert audit["declared_status"] == "continue"
