"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        record["pending_borrower_change"] = self.repository.pending_borrower_change(record_id)
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if action == "settle" and self.repository.pending_borrower_change(record_id) is not None:
            raise Conflict("存在待确认的共同借款人变更单，确认或驳回后才能结清贷款")
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def initiate_borrower_change(self, actor: Actor, record_id: int, expected_version: int,
                                 data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_initiate_borrower_change(actor.role):
            raise PermissionDenied("角色无权发起共同借款人变更")
        record = self.repository.get(record_id)
        prepared = self.rules.prepare_borrower_change(record, data or {})
        if self.repository.pending_borrower_change(record_id) is not None:
            raise Conflict("该贷款已有待确认的共同借款人变更单，每笔贷款未结清时只允许保留一张变更单")
        return self.repository.create_borrower_change(
            record_id=record_id,
            expected_version=int(expected_version),
            old_borrowers=record["payload"]["borrowers"],
            new_borrowers=prepared["borrowers"],
            reason=prepared["reason"],
            actor_id=actor.user_id,
            details={
                "reason": prepared["reason"],
                "old_borrowers": record["payload"]["borrowers"],
                "new_borrowers": prepared["borrowers"],
            },
        )

    def review_borrower_change(self, actor: Actor, change_id: int, approve: bool,
                               data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_borrower_change(actor.role):
            raise PermissionDenied("角色无权复核共同借款人变更")
        note = text({"review_note": (data or {}).get("review_note", "")}, "review_note")
        change = self._get_pending_change(change_id)
        if change["requested_by"] == actor.user_id:
            raise PermissionDenied("发起人不能复核自己发起的变更，必须由另一名复核人确认")
        return self.repository.review_borrower_change(change_id, approve, actor.user_id, note)

    def _get_pending_change(self, change_id: int) -> Dict[str, Any]:
        change = self.repository.get_borrower_change(change_id)
        if change["status"] != "pending":
            raise Conflict("变更单已处理，不能重复确认")
        return change

    def borrower_changes(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_borrower_changes(record_id)

    def collections(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        """催收名单：确认前仍按变更前责任人与份额出名单。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        items = self.repository.collection_list(limit=limit)
        result = []
        for item in items:
            payment = float(item["payload"].get("monthly_payment", 0) or 0)
            targets = [
                {
                    "person_id": borrower["person_id"],
                    "name": borrower["name"],
                    "share": borrower["share"],
                    "responsibility_amount": round(payment * float(borrower["share"]) / 100.0, 2),
                }
                for borrower in item["payload"].get("borrowers", [])
            ]
            result.append({
                "id": item["id"],
                "reference": item["reference"],
                "state": item["state"],
                "payment": payment,
                "collection_targets": targets,
                "pending_borrower_change": self.repository.pending_borrower_change(item["id"]),
            })
        return result

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return {
            "states": self.repository.stats(),
            "borrower_changes": self.repository.borrower_change_status_counts(),
            "responsibilities": self.repository.responsibility_stats(),
        }
