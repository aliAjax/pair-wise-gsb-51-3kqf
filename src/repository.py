"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .rules import SETTLED_STATE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS borrower_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    old_borrowers TEXT NOT NULL,
                    new_borrowers TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    requested_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_note TEXT DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_borrower_change_open
                    ON borrower_changes(record_id) WHERE status = 'pending';
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_borrower_change_record ON borrower_changes(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _change_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["old_borrowers"] = json.loads(item["old_borrowers"])
        item["new_borrowers"] = json.loads(item["new_borrowers"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def create_borrower_change(self, record_id: int, expected_version: int, old_borrowers: List[Dict[str, Any]],
                               new_borrowers: List[Dict[str, Any]], reason: str, actor_id: str,
                               details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    raise NotFound("记录不存在")
                if int(row["version"]) != int(expected_version):
                    connection.rollback()
                    raise Conflict("版本冲突，请刷新后重试")
                cursor = connection.execute(
                    "INSERT INTO borrower_changes(record_id,status,old_borrowers,new_borrowers,reason,expected_version,"
                    "requested_by,requested_at) VALUES(?,?,?,?,?,?,?,?)",
                    (record_id, "pending",
                     json.dumps(old_borrowers, ensure_ascii=False, sort_keys=True),
                     json.dumps(new_borrowers, ensure_ascii=False, sort_keys=True),
                     reason, int(expected_version), actor_id, now),
                )
                change_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "borrower_change_requested", actor_id, int(row["version"]),
                     json.dumps(dict(details, change_id=change_id), ensure_ascii=False, sort_keys=True), now),
                )
                result = connection.execute("SELECT * FROM borrower_changes WHERE id=?", (change_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("该贷款已有待确认的共同借款人变更单") from exc
        return self._change_row(result)

    def pending_borrower_change(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM borrower_changes WHERE record_id=? AND status='pending' ORDER BY id LIMIT 1",
                (record_id,),
            ).fetchone()
        return self._change_row(row) if row is not None else None

    def get_borrower_change(self, change_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM borrower_changes WHERE id=?", (change_id,)).fetchone()
        if row is None:
            raise NotFound("变更单不存在")
        return self._change_row(row)

    def borrower_change_status_counts(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM borrower_changes GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def list_borrower_changes(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM borrower_changes WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        return [self._change_row(row) for row in rows]

    def _finalize_borrower_change(self, connection: sqlite3.Connection, change: Dict[str, Any], status: str,
                                  actor_id: str, note: str) -> None:
        now = _now()
        record_id = int(change["record_id"])
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        version = int(row["version"])
        details: Dict[str, Any] = {
            "change_id": change["id"],
            "reason": change["reason"],
            "review_note": note,
            "old_borrowers": change["old_borrowers"],
            "new_borrowers": change["new_borrowers"],
        }
        if status == "confirmed":
            state_row = connection.execute("SELECT state,payload FROM records WHERE id=?", (record_id,)).fetchone()
            if str(state_row["state"]) == SETTLED_STATE:
                raise Conflict("贷款已结清，变更单不能生效")
            payload = json.loads(state_row["payload"])
            payload["borrowers"] = change["new_borrowers"]
            payload["borrower_id"] = change["new_borrowers"][0]["person_id"]
            version += 1
            connection.execute(
                "UPDATE records SET version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            action_name = "borrower_change_confirmed"
        else:
            action_name = "borrower_change_rejected"
        connection.execute(
            "UPDATE borrower_changes SET status=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
            (status, actor_id, now, note, change["id"]),
        )
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action_name, actor_id, version,
             json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )

    def review_borrower_change(self, change_id: int, approve: bool, actor_id: str, note: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM borrower_changes WHERE id=?", (change_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("变更单不存在")
            change = self._change_row(row)
            if change["status"] != "pending":
                connection.rollback()
                raise Conflict("变更单已处理，不能重复确认")
            self._finalize_borrower_change(connection, change, "confirmed" if approve else "rejected", actor_id, note)
            result = connection.execute("SELECT * FROM borrower_changes WHERE id=?", (change_id,)).fetchone()
            connection.commit()
        return self._change_row(result)

    def collection_list(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE state IN ('active','defaulted') ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def responsibility_stats(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM records WHERE state != ?", (SETTLED_STATE,)
            ).fetchall()
        totals: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["payload"])
            payment = float(payload.get("monthly_payment", 0) or 0)
            for borrower in payload.get("borrowers", []):
                entry = totals.setdefault(borrower["person_id"], {"person_id": borrower["person_id"], "name": borrower["name"], "loans": 0, "share_sum": 0.0, "outstanding_responsibility": 0.0})
                entry["loans"] += 1
                entry["share_sum"] += float(borrower["share"])
                entry["outstanding_responsibility"] += payment * float(borrower["share"]) / 100.0
        result = list(totals.values())
        for entry in result:
            entry["share_sum"] = round(entry["share_sum"], 2)
            entry["outstanding_responsibility"] = round(entry["outstanding_responsibility"], 2)
        result.sort(key=lambda item: item["person_id"])
        return result

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
