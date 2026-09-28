"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


CHANGE_COLUMNS = [
    "id", "record_id", "state", "version",
    "before_borrowers", "after_borrowers", "reason",
    "created_by", "reviewed_by", "review_note",
    "record_version_at_create", "record_version_after",
    "created_at", "updated_at",
]



def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


SETTLED_DB_STATES = {"settled"}


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
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    before_borrowers TEXT NOT NULL,
                    after_borrowers TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT '',
                    record_version_at_create INTEGER NOT NULL,
                    record_version_after INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_changes_record ON borrower_changes(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_changes_one_pending
                    ON borrower_changes(record_id) WHERE state = 'pending';
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
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

    @staticmethod
    def _change_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = {column: row[column] for column in CHANGE_COLUMNS}
        item["before_borrowers"] = json.loads(item["before_borrowers"])
        item["after_borrowers"] = json.loads(item["after_borrowers"])
        return item

    def create_borrower_change(
        self,
        record_id: int,
        before_borrowers: List[Dict[str, Any]],
        after_borrowers: List[Dict[str, Any]],
        reason: str,
        actor_id: str,
        record_version: int,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM records WHERE id=?", (record_id,)).fetchone() is None:
                connection.rollback()
                raise NotFound("记录不存在")
            pending = connection.execute(
                "SELECT id FROM borrower_changes WHERE record_id=? AND state='pending'",
                (record_id,),
            ).fetchone()
            if pending is not None:
                connection.rollback()
                raise Conflict("该贷款已有待确认的共同借款人变更单")
            try:
                cursor = connection.execute(
                    "INSERT INTO borrower_changes(record_id,state,version,before_borrowers,after_borrowers,reason,created_by,record_version_at_create,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        record_id, "pending", 1,
                        json.dumps(before_borrowers, ensure_ascii=False, sort_keys=True),
                        json.dumps(after_borrowers, ensure_ascii=False, sort_keys=True),
                        reason, actor_id, record_version, now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("该贷款已有待确认的共同借款人变更单") from exc
            change_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    record_id, "borrower_change_requested", actor_id, record_version,
                    json.dumps({
                        "change_id": change_id,
                        "summary": "共同借款人变更申请待确认",
                        "before_borrowers": before_borrowers,
                        "after_borrowers": after_borrowers,
                        "reason": reason,
                    }, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM borrower_changes WHERE id=?", (change_id,)).fetchone()
            connection.commit()
        return self._change_row(row)

    def get_borrower_change(self, change_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM borrower_changes WHERE id=?", (change_id,)).fetchone()
        if row is None:
            raise NotFound("变更单不存在")
        return self._change_row(row)

    def list_borrower_changes(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM borrower_changes WHERE record_id=? ORDER BY id",
                (record_id,),
            ).fetchall()
        return [self._change_row(row) for row in rows]

    def pending_change_ids(self, record_ids: List[int]) -> Dict[int, int]:
        if not record_ids:
            return {}
        placeholders = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, record_id FROM borrower_changes WHERE state='pending' AND record_id IN (%s)" % placeholders,
                record_ids,
            ).fetchall()
        return {int(row["record_id"]): int(row["id"]) for row in rows}

    def review_borrower_change(
        self,
        change_id: int,
        reviewer_id: str,
        approved: bool,
        note: str,
    ) -> Dict[str, Any]:
        new_state = "confirmed" if approved else "rejected"
        summary = "共同借款人变更已确认并生效" if approved else "共同借款人变更被驳回"
        action = "borrower_change_confirmed" if approved else "borrower_change_rejected"
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM borrower_changes WHERE id=?", (change_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("变更单不存在")
            change = self._change_row(row)
            if change["state"] != "pending":
                connection.rollback()
                raise Conflict("变更单已处理，不能重复%s" % ("确认" if approved else "驳回"))
            record = connection.execute("SELECT * FROM records WHERE id=?", (change["record_id"],)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            payload = json.loads(record["payload"])
            record_version_after: Optional[int] = None
            if approved:
                if record["state"] in SETTLED_DB_STATES:
                    connection.rollback()
                    raise Conflict("贷款已结清，不能确认变更")
                before_borrowers = payload.get("borrowers")
                if before_borrowers != change["before_borrowers"]:
                    connection.rollback()
                    raise Conflict("当前还款人已与变更单不一致，请刷新后重新发起")
                payload["borrowers"] = change["after_borrowers"]
                record_version_after = int(record["version"]) + 1
                connection.execute(
                    "UPDATE records SET payload=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                    (
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        record_version_after, reviewer_id, now, change["record_id"],
                    ),
                )
            connection.execute(
                "UPDATE borrower_changes SET state=?,version=version+1,reviewed_by=?,review_note=?,record_version_after=?,updated_at=? WHERE id=?",
                (new_state, reviewer_id, note, record_version_after, now, change_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    change["record_id"], action, reviewer_id,
                    record_version_after if record_version_after is not None else int(record["version"]),
                    json.dumps({
                        "change_id": change_id,
                        "summary": summary,
                        "review_note": note,
                        "before_borrowers": change["before_borrowers"],
                        "after_borrowers": change["after_borrowers"],
                        "reason": change["reason"],
                    }, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            result = connection.execute("SELECT * FROM borrower_changes WHERE id=?", (change_id,)).fetchone()
            connection.commit()
        return self._change_row(result)

    def cancel_borrower_change(self, change_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM borrower_changes WHERE id=?", (change_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("变更单不存在")
            change = self._change_row(row)
            if change["state"] != "pending":
                connection.rollback()
                raise Conflict("变更单已处理，不能撤销")
            record_version = connection.execute(
                "SELECT version FROM records WHERE id=?", (change["record_id"],)
            ).fetchone()["version"]
            connection.execute(
                "UPDATE borrower_changes SET state='canceled',version=version+1,updated_at=? WHERE id=?",
                (now, change_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    change["record_id"], "borrower_change_canceled", actor_id, int(record_version),
                    json.dumps({"change_id": change_id, "summary": "共同借款人变更申请已撤销"}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            result = connection.execute("SELECT * FROM borrower_changes WHERE id=?", (change_id,)).fetchone()
            connection.commit()
        return self._change_row(result)

    def borrower_change_stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM borrower_changes GROUP BY state").fetchall()
        result = {"pending": 0, "confirmed": 0, "rejected": 0, "canceled": 0}
        for row in rows:
            result[str(row["state"])] = int(row["total"])
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
