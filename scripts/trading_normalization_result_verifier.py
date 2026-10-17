"""Test-only ADR-066 oracle over the public terminal resource.

This module is deliberately local to the integration harness. It does not import
either application hasher and never uses Compute persistence as a hash oracle.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import jsonschema
import yaml

SPEC = "TRADING_NORMALIZATION_RESULT_HASH_V1"
SCHEMA = "TRADING_NORMALIZATION_RESULT_V1"
INPUT_SPEC = "TRADING_INPUT_SET_HASH_V1"
MAX_INT = 9007199254740991
COLLECTIONS = {
    "ARTIFACT_COVERAGES": ("artifactCoverages", "artifactRole", "artifactCoverageCount"),
    "CANONICAL_RECORDS": ("canonicalRecords", "recordId", "canonicalRecordCount"),
    "SOURCE_EVIDENCE_SNAPSHOTS": ("sourceEvidenceSnapshots", "snapshotId", "sourceEvidenceSnapshotCount"),
    "REVIEW_UNITS": ("reviewUnits", "reviewUnitId", "reviewUnitCount"),
    "CAPABILITIES": ("capability", None, "capabilityCount"),
    "RECONCILIATION_MATCHES": ("reconciliationMatches", "matchId", "reconciliationMatchCount"),
    "RECONCILIATION_ISSUES": ("reconciliationIssues", "issueId", "reconciliationIssueCount"),
}
SECTIONS = sorted([*COLLECTIONS, "QUALITY_REPORT", "RECONCILIATION_MANIFEST"])
DECIMAL_FIELDS = {"value", "amount", "actual", "expected", "absoluteDelta", "tolerance", "price", "quantity"}
TIME_FIELDS = {"earliestObservedAt", "latestObservedAt", "occurredAt", "openedAt", "closedAt"}


class IntegrityFailure(Exception):
    def __init__(self, code: str, section: str = "", expected: object = "", actual: object = ""):
        super().__init__(code)
        self.code, self.section = code, section
        self.expected, self.actual = str(expected)[:12], str(actual)[:12]


def fail(code: str, section: str = "", expected: object = "", actual: object = "") -> None:
    raise IntegrityFailure(code, section, expected, actual)


def utf16(value: str) -> bytes:
    return value.encode("utf-16-be")


def canonical(value: object) -> bytes:
    """The V1 JCS subset: safe integers, UTF-16 key ordering, UTF-8 strings."""
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int) and 0 <= value <= MAX_INT:
        return str(value).encode("ascii")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if isinstance(value, list):
        return b"[" + b",".join(map(canonical, value)) + b"]"
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return b"{" + b",".join(canonical(k) + b":" + canonical(value[k])
                              for k in sorted(value, key=utf16)) + b"}"
    fail("RESULT_SCHEMA_UNSUPPORTED")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def parse_bytes(body: bytes) -> dict:
    if body.startswith(b"\xef\xbb\xbf"):
        fail("RESULT_SCHEMA_UNSUPPORTED")

    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                fail("RESULT_SCHEMA_UNSUPPORTED")
            result[key] = value
        return result

    def no_float(_):
        fail("RESULT_SCHEMA_UNSUPPORTED")

    def integer(raw):
        n = int(raw)
        if n < 0 or n > MAX_INT:
            fail("RESULT_SCHEMA_UNSUPPORTED")
        return n

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs,
                           parse_float=no_float, parse_constant=no_float, parse_int=integer)
    except (UnicodeError, ValueError):
        fail("RESULT_SCHEMA_UNSUPPORTED")
    if not isinstance(value, dict):
        fail("RESULT_SCHEMA_UNSUPPORTED")
    return value


def decimal_string(value: str) -> str:
    try:
        n = Decimal(value)
        if not n.is_finite():
            raise InvalidOperation
        return "0" if n == 0 else format(n.normalize(), "f")
    except InvalidOperation:
        fail("RESULT_SCHEMA_UNSUPPORTED")


def timestamp(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError
        utc = dt.astimezone(timezone.utc)
    except ValueError:
        fail("RESULT_SCHEMA_UNSUPPORTED")
    fraction = ("." + f"{utc.microsecond:06d}".rstrip("0")) if utc.microsecond else ""
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


def normalize(value: object, field: str = "") -> object:
    if field == "allowlistedFields":
        return value
    if isinstance(value, dict):
        return {key: normalize(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, str) and field in DECIMAL_FIELDS:
        return decimal_string(value)
    if isinstance(value, str) and field in TIME_FIELDS:
        return timestamp(value)
    return value


def unique(items: list, key: str | None, section: str) -> list:
    if not isinstance(items, list):
        fail("RESULT_SCHEMA_UNSUPPORTED", section)
    found = {}
    for item in items:
        identity = item if key is None else item.get(key) if isinstance(item, dict) else None
        if not isinstance(identity, str) or not identity:
            fail("RESULT_ITEM_CONFLICT", section)
        if identity in found and canonical(found[identity]) != canonical(item):
            fail("RESULT_ITEM_CONFLICT", section)
        found[identity] = item
    return [found[k] for k in sorted(found, key=utf16)]


def shape(payload: dict) -> tuple[dict, dict]:
    """Build section values from public DTOs without trusting declared hashes/counts."""
    p = normalize(copy.deepcopy(payload))
    manifest = p["reconciliationManifest"]
    for section, (source, identity, _) in COLLECTIONS.items():
        owner = manifest if section in {"REVIEW_UNITS", "RECONCILIATION_MATCHES", "RECONCILIATION_ISSUES"} else p
        owner[source] = unique(owner[source], identity, section)
    manifest["artifactCoverages"] = unique(manifest["artifactCoverages"], "artifactRole", "RECONCILIATION_MANIFEST")
    manifest["capability"] = unique(manifest["capability"], None, "RECONCILIATION_MANIFEST")
    for evidence in p["sourceEvidenceSnapshots"]:
        evidence["allowlistedFields"] = unique(evidence["allowlistedFields"], "name", "SOURCE_EVIDENCE_SNAPSHOTS")
    for unit in manifest["reviewUnits"]:
        unit["allocations"] = unique(unit["allocations"], "allocationId", "REVIEW_UNITS")
        for name in ("capability", "exclusionReasons"):
            unit[name] = unique(unit[name], None, "REVIEW_UNITS")
    for match in manifest["reconciliationMatches"]:
        for name in ("candidateReviewUnitIds", "evidenceRecordIds"):
            match[name] = unique(match[name], None, "RECONCILIATION_MATCHES")
        match["deltas"] = unique(match["deltas"], "field", "RECONCILIATION_MATCHES")
    for issue in manifest["reconciliationIssues"]:
        issue["evidenceRecordIds"] = unique(issue["evidenceRecordIds"], None, "RECONCILIATION_ISSUES")
    quality = p["qualityReport"]
    issues = {}
    for issue in quality["validationIssues"]:
        identity = canonical([issue.get(k) for k in ("artifactRole", "sourceRowNumber", "code", "field", "severity")])
        if identity in issues and canonical(issues[identity]) != canonical(issue):
            fail("RESULT_ITEM_CONFLICT", "QUALITY_REPORT")
        issues[identity] = issue
    quality["validationIssues"] = [issues[k] for k in sorted(issues)]
    sections = {}
    for section, (source, _, _) in COLLECTIONS.items():
        sections[section] = manifest[source] if section in {"REVIEW_UNITS", "RECONCILIATION_MATCHES", "RECONCILIATION_ISSUES"} else p[source]
    sections["CANONICAL_RECORDS"] = [{k: v for k, v in item.items() if k != "recordHash"} for item in p["canonicalRecords"]]
    sections["SOURCE_EVIDENCE_SNAPSHOTS"] = [{k: v for k, v in item.items() if k != "capturedAt"} for item in p["sourceEvidenceSnapshots"]]
    sections["QUALITY_REPORT"] = quality
    sections["RECONCILIATION_MANIFEST"] = {k: v for k, v in manifest.items() if k != "manifestHash"}
    return p, sections


def counts(p: dict, sections: dict) -> dict:
    units = sections["REVIEW_UNITS"]
    allocations = unique([a for u in units for a in u["allocations"]], "allocationId", "REVIEW_UNITS")
    result = {count: len(sections[section]) for section, (_, _, count) in COLLECTIONS.items()}
    result.update(positionAllocationCount=len(allocations),
                  excludedReviewUnitCount=sum(bool(u["exclusionReasons"]) for u in units),
                  validationIssueCount=len(p["qualityReport"]["validationIssues"]))
    if result["artifactCoverageCount"] != 2:
        fail("RESULT_COUNT_MISMATCH", "ARTIFACT_COVERAGES", 2, result["artifactCoverageCount"])
    return result


def references(p: dict, artifact_ids: dict | None = None,
               artifact_hashes: dict | None = None) -> None:
    m = p["reconciliationManifest"]
    records = {r["recordId"] for r in p["canonicalRecords"]}
    evidence = {e["snapshotId"] for e in p["sourceEvidenceSnapshots"]}
    units = {u["reviewUnitId"] for u in m["reviewUnits"]}
    if artifact_hashes is not None and m["sourceArtifactHashesByRole"] != artifact_hashes:
        fail("EVIDENCE_REFERENCE_INVALID", "SOURCE_EVIDENCE_SNAPSHOTS")
    for e in p["sourceEvidenceSnapshots"]:
        role = e["artifactRole"]
        if role not in m["sourceArtifactHashesByRole"] or not isinstance(e["sourceRowNumber"], int) or e["sourceRowNumber"] < 1:
            fail("EVIDENCE_REFERENCE_INVALID", "SOURCE_EVIDENCE_SNAPSHOTS")
        if artifact_ids and e["coreArtifactId"] != artifact_ids.get(role):
            fail("EVIDENCE_REFERENCE_INVALID", "SOURCE_EVIDENCE_SNAPSHOTS")
    for r in p["canonicalRecords"]:
        if r["sourceEvidenceSnapshotId"] not in evidence:
            fail("EVIDENCE_REFERENCE_INVALID", "CANONICAL_RECORDS")
    for u in m["reviewUnits"]:
        for a in u["allocations"]:
            if a["recordId"] not in records or a["reviewUnitId"] != u["reviewUnitId"]:
                fail("EVIDENCE_REFERENCE_INVALID", "REVIEW_UNITS")
    for match in m["reconciliationMatches"]:
        if match["summaryRecordId"] not in records or (match.get("reviewUnitId") and match["reviewUnitId"] not in units):
            fail("EVIDENCE_REFERENCE_INVALID", "RECONCILIATION_MATCHES")
        if any(x not in records for x in match["evidenceRecordIds"]) or any(x not in units for x in match["candidateReviewUnitIds"]):
            fail("EVIDENCE_REFERENCE_INVALID", "RECONCILIATION_MATCHES")
    for issue in m["reconciliationIssues"]:
        if (issue.get("reviewUnitId") and issue["reviewUnitId"] not in units) or (issue.get("summaryRecordId") and issue["summaryRecordId"] not in records):
            fail("EVIDENCE_REFERENCE_INVALID", "RECONCILIATION_ISSUES")
        if any(x not in records for x in issue["evidenceRecordIds"]):
            fail("EVIDENCE_REFERENCE_INVALID", "RECONCILIATION_ISSUES")


def verify(payload: dict, descriptor: dict | None = None, artifact_ids: dict | None = None,
           artifact_hashes: dict | None = None) -> dict:
    if payload.get("resultSchemaVersion") != SCHEMA or payload.get("resultHashSpecificationVersion") != SPEC or payload.get("hashSpecificationVersion") != INPUT_SPEC:
        fail("RESULT_SCHEMA_UNSUPPORTED")
    p, sections = shape(payload)
    actual_counts = counts(p, sections)
    if actual_counts != payload.get("counts") or actual_counts != p["reconciliationManifest"].get("counts"):
        fail("RESULT_COUNT_MISMATCH", expected=payload.get("counts"), actual=actual_counts)
    references(p, artifact_ids, artifact_hashes)
    m = p["reconciliationManifest"]
    if m["artifactCoverages"] != p["artifactCoverages"] or m["capability"] != p["capability"] or m["inputSetHash"] != p["inputSetHash"] or m["versionSetHash"] != p["targetVersionSet"]["versionSetHash"]:
        fail("RESULT_ITEM_CONFLICT", "RECONCILIATION_MANIFEST")
    independent_manifest_hash = digest(sections["RECONCILIATION_MANIFEST"])
    if m.get("manifestHash") != independent_manifest_hash:
        fail("RECONCILIATION_MANIFEST_HASH_MISMATCH", "RECONCILIATION_MANIFEST",
             m.get("manifestHash"), independent_manifest_hash)
    declared = payload.get("sectionDigests")
    if not isinstance(declared, list) or [d.get("sectionType") for d in declared] != SECTIONS:
        fail("RESULT_SECTION_HASH_MISMATCH")
    hashes = {}
    for d in declared:
        section = d["sectionType"]
        n = 1 if section in {"QUALITY_REPORT", "RECONCILIATION_MANIFEST"} else len(sections[section])
        if d.get("itemCount") != n:
            fail("RESULT_COUNT_MISMATCH", section, d.get("itemCount"), n)
        if d.get("hashSpecificationVersion") != SPEC:
            fail("RESULT_SCHEMA_UNSUPPORTED", section)
        actual = digest(sections[section])
        if d.get("sectionHash") != actual:
            fail("RESULT_SECTION_HASH_MISMATCH", section, d.get("sectionHash"), actual)
        hashes[section] = actual
    if m.get("canonicalRecordSetHash") != hashes["CANONICAL_RECORDS"] or m.get("qualityReportHash") != hashes["QUALITY_REPORT"] or p.get("ledgerContentHash") != hashes["CANONICAL_RECORDS"]:
        fail("RESULT_SECTION_HASH_MISMATCH")
    if m.get("manifestHashSpecificationVersion") != SPEC:
        fail("RESULT_SCHEMA_UNSUPPORTED", "RECONCILIATION_MANIFEST")
    if m.get("manifestHash") != hashes["RECONCILIATION_MANIFEST"]:
        fail("RECONCILIATION_MANIFEST_HASH_MISMATCH", "RECONCILIATION_MANIFEST", m.get("manifestHash"), hashes["RECONCILIATION_MANIFEST"])
    expected_manifest = {
        "resultHashSpecificationVersion": SPEC, "resultSchemaVersion": SCHEMA,
        "inputSetHash": p["inputSetHash"], "inputHashSpecificationVersion": p["hashSpecificationVersion"],
        "targetVersionSet": p["targetVersionSet"], "counts": actual_counts,
        "sectionDigests": declared, "reconciliationManifestHash": hashes["RECONCILIATION_MANIFEST"],
    }
    if p.get("resultDigestManifest") != expected_manifest:
        fail("RESULT_HASH_MISMATCH")
    actual_result = digest(expected_manifest)
    if p.get("resultHash") != actual_result:
        fail("RESULT_HASH_MISMATCH", expected=p.get("resultHash"), actual=actual_result)
    if descriptor is not None:
        for key in ("jobId", "coreLogicalJobId", "coreNormalizationRunId", "inputSetHash", "targetVersionSet", "resultSchemaVersion", "resultHashSpecificationVersion", "counts", "sectionDigests", "resultHash", "deletionGeneration", "attemptToken"):
            if descriptor.get(key) != payload.get(key):
                fail("RESULT_HASH_MISMATCH")
        if descriptor.get("reconciliationManifestHash") != m["manifestHash"] or descriptor.get("transportMode") != "SINGLE_RESOURCE":
            fail("RESULT_HASH_MISMATCH")
    return {"counts": actual_counts, "sectionHashes": hashes, "manifestHash": hashes["RECONCILIATION_MANIFEST"], "resultHash": actual_result}


def negative_scenarios(body: bytes, descriptor: dict, payload: dict, artifact_ids: dict) -> dict:
    """Each copied mutation must be rejected with the named safe category."""
    passed = {}

    def reject(name: str, expected: set[str], change, *, transport: bool = False):
        try:
            if transport:
                change()
            else:
                altered = copy.deepcopy(payload)
                change(altered)
                verify(altered, artifact_ids=artifact_ids)
        except IntegrityFailure as error:
            if error.code not in expected:
                fail("TAMPER_WRONG_FAILURE", name, expected, error.code)
            passed[name] = error.code
        else:
            fail("TAMPER_NOT_DETECTED", name)

    changed = bytearray(body)
    changed[-1] = changed[-1] ^ 1
    reject("transport_bytes", {"RESULT_TRANSPORT_HASH_MISMATCH"},
           lambda: verify_transport(bytes(changed), descriptor,
                                    "application/vnd.refinvest.trading-normalization+json; charset=utf-8", "identity"),
           transport=True)
    reject("missing_canonical_record", {"RESULT_COUNT_MISMATCH"},
           lambda p: p["canonicalRecords"].pop())

    def modify_record(p):
        item = p["canonicalRecords"][0]
        item["side"] = "SELL" if item.get("side") != "SELL" else "BUY"
    reject("modified_canonical_record", {"RESULT_SECTION_HASH_MISMATCH"}, modify_record)
    reject("missing_evidence", {"RESULT_COUNT_MISMATCH", "EVIDENCE_REFERENCE_INVALID"},
           lambda p: p["sourceEvidenceSnapshots"].pop())
    reject("invalid_evidence_reference", {"EVIDENCE_REFERENCE_INVALID"},
           lambda p: p["canonicalRecords"][0].update(sourceEvidenceSnapshotId="missing-synthetic-evidence"))
    reject("modified_manifest", {"RECONCILIATION_MANIFEST_HASH_MISMATCH"},
           lambda p: p["reconciliationManifest"].update(executionInputHash="0" * 64))
    reject("modified_section_digest", {"RESULT_HASH_MISMATCH"},
           lambda p: p["resultDigestManifest"]["sectionDigests"][0].update(sectionHash="0" * 64))
    reject("unsupported_result_hash_version", {"RESULT_SCHEMA_UNSUPPORTED"},
           lambda p: p.update(resultHashSpecificationVersion="UNSUPPORTED"))

    def conflict(p):
        item = copy.deepcopy(p["canonicalRecords"][0])
        item["side"] = "SELL" if item.get("side") != "SELL" else "BUY"
        p["canonicalRecords"].append(item)
    reject("same_identity_different_payload", {"RESULT_ITEM_CONFLICT"}, conflict)
    return passed


def pagination_independence(payload: dict, artifact_ids: dict) -> dict:
    """V1 is a single resource; exercise item arrival order and replay semantics."""
    baseline = verify(payload, artifact_ids=artifact_ids)
    reordered = copy.deepcopy(payload)
    for name in ("artifactCoverages", "canonicalRecords", "sourceEvidenceSnapshots", "capability"):
        reordered[name].reverse()
    m = reordered["reconciliationManifest"]
    for name in ("artifactCoverages", "reviewUnits", "reconciliationMatches", "reconciliationIssues", "capability"):
        m[name].reverse()
    for unit in m["reviewUnits"]:
        unit["allocations"].reverse()
    if verify(reordered, artifact_ids=artifact_ids) != baseline:
        fail("RETRIEVAL_ORDER_DRIFT")
    replay = copy.deepcopy(payload)
    replay["canonicalRecords"].append(copy.deepcopy(replay["canonicalRecords"][0]))
    if verify(replay, artifact_ids=artifact_ids) != baseline:
        fail("DUPLICATE_PAGE_DRIFT")
    missing = copy.deepcopy(payload)
    missing["canonicalRecords"].pop()
    try:
        verify(missing, artifact_ids=artifact_ids)
    except IntegrityFailure as error:
        if error.code != "RESULT_COUNT_MISMATCH":
            raise
    else:
        fail("MISSING_PAGE_NOT_DETECTED")
    return {"retrievalOrder": "PASS", "duplicateItemReplay": "PASS", "missingLogicalPageItem": "PASS"}


def collect_v1_resource(resources: list[tuple[str, bytes]], payload_id: str) -> bytes:
    """V1's sole logical page has identity (payloadId, pageIndex=0)."""
    selected = None
    for identity, body in resources:
        if identity != payload_id:
            fail("RESULT_ITEM_CONFLICT", "logical-page-0")
        if selected is not None and selected != body:
            fail("RESULT_ITEM_CONFLICT", "logical-page-0")
        selected = body
    if selected is None:
        fail("RESULT_COUNT_MISMATCH", "logical-page-0")
    return selected


def verify_transport(body: bytes, descriptor: dict, content_type: str, encoding: str) -> dict:
    base_type, _, parameters = content_type.lower().partition(";")
    if (encoding.lower() != "identity" or
            base_type.strip() != descriptor.get("mediaType", "").lower() or
            base_type.strip() != "application/vnd.refinvest.trading-normalization+json" or
            parameters.strip() != "charset=utf-8"):
        fail("RESULT_SCHEMA_UNSUPPORTED")
    actual = hashlib.sha256(body).hexdigest()
    if actual != descriptor.get("payloadSha256") or len(body) != descriptor.get("byteSize"):
        fail("RESULT_TRANSPORT_HASH_MISMATCH", expected=descriptor.get("payloadSha256"), actual=actual)
    return parse_bytes(body)


def schema_validate(context: Path, payload: dict, descriptor: dict) -> None:
    document = yaml.safe_load((context / "openapi/compute-api.yaml").read_text(encoding="utf-8"))
    resolver = jsonschema.RefResolver.from_schema(document)
    for name, value in (("NormalizationTerminalPayload", payload), ("NormalizationTerminalResultDescriptor", descriptor)):
        jsonschema.validate(value, document["components"]["schemas"][name], resolver=resolver)


def self_test(context: Path) -> dict:
    vector = json.loads((context / "fixtures/trading-normalization/result-integrity-v1.json").read_text(encoding="utf-8"))
    payload = vector["terminalPayload"]
    if vector["specificationVersion"] != SPEC or vector["resultSchemaVersion"] != SCHEMA:
        fail("RESULT_SCHEMA_UNSUPPORTED")
    _, sections = shape(payload)
    for name in SECTIONS:
        expected = vector["sectionVectors"][name]
        body = canonical(sections[name])
        if body != expected["canonicalJson"].encode("utf-8") or body.hex() != expected["canonicalUtf8Hex"] or hashlib.sha256(body).hexdigest() != expected["expectedSha256"]:
            fail("RESULT_SECTION_HASH_MISMATCH", name, expected["expectedSha256"], hashlib.sha256(body).hexdigest())
    for value, expected in ((sections["RECONCILIATION_MANIFEST"], vector["manifestVector"]),
                            (payload["resultDigestManifest"], vector["resultDigestManifestVector"])):
        body = canonical(value)
        if body != expected["canonicalJson"].encode("utf-8") or body.hex() != expected["canonicalUtf8Hex"] or hashlib.sha256(body).hexdigest() != expected["expectedSha256"]:
            fail("RESULT_HASH_MISMATCH", expected=expected["expectedSha256"], actual=hashlib.sha256(body).hexdigest())
    result = verify(payload)
    if result["resultHash"] != vector["resultDigestManifestVector"]["expectedSha256"]:
        fail("RESULT_HASH_MISMATCH")
    wire = json.loads((context / "fixtures/trading-normalization/wire-v1.json").read_text(encoding="utf-8"))
    schema_validate(context, payload, wire["completedDescriptor"])
    return result
