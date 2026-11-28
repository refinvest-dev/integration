"""One-shot list decoder probes applied only after a real successful Go response."""
import json
import threading

from owner_query_body_fault import BodyFaults

LIST_LIMIT = 2 * 1024 * 1024
MUTATIONS = frozenset(("longmax", "overflow_total", "duplicate_field", "unknown_field", "missing_field",
                       "wrong_echo", "duplicate_id", "unsorted", "invalid_utf8"))


def mutated_page(payload, mutation):
    if mutation not in MUTATIONS or len(payload) > LIST_LIMIT:
        raise ValueError("Unknown or oversized list probe")
    page = json.loads(payload)
    if mutation in ("longmax", "overflow_total"):
        page["total"] = 9223372036854775807 + int(mutation == "overflow_total")
    elif mutation == "unknown_field":
        page["unexpected"] = True
    elif mutation == "missing_field":
        del page["items"][0]["venue"]
    elif mutation == "wrong_echo":
        page["page"] += 1
    elif mutation == "duplicate_id":
        page["items"][1]["id"] = page["items"][0]["id"]
    elif mutation == "unsorted":
        page["items"][0], page["items"][1] = page["items"][1], page["items"][0]
    raw = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if mutation == "duplicate_field":
        raw = b'{"total":0,' + raw[1:]
    if mutation == "invalid_utf8":
        raw = b"\xff" + raw
    if len(raw) > LIST_LIMIT:
        raise ValueError("Probe exceeds fixture limit")
    return raw


class PageMutation:
    protocol = "HTTP/1.0"

    def __init__(self, mutation):
        if mutation not in MUTATIONS:
            raise ValueError("Unknown list probe")
        self.kind = mutation
        self.release = threading.Event()
        self.finished = threading.Event()

    def headers(self, payload):
        return {"Content-Length": str(len(mutated_page(payload, self.kind))), "Connection": "close"}

    def write(self, handler, payload):
        try:
            handler.wfile.write(mutated_page(payload, self.kind))
            handler.wfile.flush()
        finally:
            self.finished.set()


class ListFaults(BodyFaults):
    def __init__(self):
        super().__init__(LIST_LIMIT)

    def mutate(self, mutation):
        fault = PageMutation(mutation)
        with self._lock:
            if self._pending is not None or len(self.records) >= 24:
                raise ValueError("List fault pending or fixture budget exhausted")
            self.records.append(fault)
            self._pending = fault
        return fault
