from __future__ import annotations

import base64
import json

import pytest

from yunpai_orchestrator.skills import identify_business_data
from yunpai_orchestrator.uploads import (
    MAX_BATCH_FILES,
    MAX_FILE_BYTES,
    UPLOAD_MODES,
    UploadSummary,
    require_content_b64,
    validate_mode,
)


def test_validate_mode_rejects_missing_or_free_text():
    with pytest.raises(ValueError):
        validate_mode("")
    with pytest.raises(ValueError):
        validate_mode("随便导入点资料")
    assert validate_mode("order") == "order"
    assert validate_mode("master_data") == "master_data"
    assert validate_mode("directory") == "directory"
    assert set(UPLOAD_MODES) == {"order", "master_data", "directory"}


def test_require_content_b64_rejects_missing_and_bad_base64():
    with pytest.raises(ValueError, match="缺少 content_b64"):
        require_content_b64({"filename": "a.xlsx"})
    with pytest.raises(ValueError, match="缺少 content_b64"):
        require_content_b64({"filename": "a.xlsx", "content_b64": ""})
    with pytest.raises(ValueError, match="不是合法 base64"):
        require_content_b64({"filename": "a.xlsx", "content_b64": "!!!not-base64!!!"})
    raw = require_content_b64({"filename": "a.xlsx", "content_b64": base64.b64encode(b"abc").decode()})
    assert raw == b"abc"


def test_upload_summary_counts_and_skip_reasons():
    summary = UploadSummary(mode="master_data")
    from yunpai_orchestrator.uploads import to_attachment_record

    summary.add(to_attachment_record({"filename": "a.json", "sha256": "s1"}, mode="master_data", status="accepted"))
    summary.add(to_attachment_record({"filename": "b.json", "sha256": "s2"}, mode="master_data", status="accepted"))
    summary.add(to_attachment_record({"filename": "c.xlsx"}, mode="master_data", status="skipped", reason="missing_content_b64"))
    data = summary.as_dict()
    assert data["total"] == 3
    assert data["accepted"] == 2
    assert data["skipped"] == 1
    assert data["skip_reasons"] == {"missing_content_b64": 1}
    assert data["files"][0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_skill_with_only_missing_content_files_fails_not_silent(tmp_path):
    context = {"task_id": "TASK-EMPTY-1"}
    payload = {
        "mode": "master_data",
        "files": [
            {"filename": "no-b64.xlsx", "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            {"filename": "bad-b64.xlsx", "content_b64": "not-valid!!!"},
        ],
        "staging_dir": str(tmp_path / "staging"),
        "db_path": str(tmp_path / "catalog.sqlite"),
    }
    result = await identify_business_data(payload, context)
    assert result["status"] == "failed"
    assert result["code"] == "NO_ACCEPTED_FILES"
    summary = result["upload_summary"]
    assert summary["accepted"] == 0
    assert summary["skipped"] == 2
    assert summary["files"][0]["reason"] != ""


@pytest.mark.asyncio
async def test_skill_accepts_files_and_returns_batch_with_mode(tmp_path):
    context = {"task_id": "TASK-OK-1"}
    payload = {
        "mode": "master_data",
        # G4（INT2 第五轮）：缺显式 product_code 且有 accepted 文件时技能返回
        # ``needs_product_code``（fail-closed，不发布空批次）——本用例验证的是
        # 「接受文件并回批次」，故显式给出产品编码；缺编码路径见
        # tests/test_int2_r5_master_data_upload.py。
        "product_code": "P-TEST-1",
        "files": [
            {"filename": "设备台账.json", "content_b64": base64.b64encode(b'{"records":[{"kind":"equipment"}]}').decode(), "content_type": "application/json"},
        ],
        "staging_dir": str(tmp_path / "staging"),
        "db_path": str(tmp_path / "catalog.sqlite"),
    }
    result = await identify_business_data(payload, context)
    assert result["status"] == "candidate_created"
    assert result["skill_mode"] == "master_data"
    assert result["upload_summary"]["accepted"] == 1
    assert result["batch"]["file_count"] >= 1


def test_batch_limit_constants_are_sane():
    assert MAX_FILE_BYTES >= 1 * 1024 * 1024
    assert MAX_BATCH_FILES >= 100


def test_json_serializable_summary():
    summary = UploadSummary(mode="directory")
    json.dumps(summary.as_dict(), ensure_ascii=False)
