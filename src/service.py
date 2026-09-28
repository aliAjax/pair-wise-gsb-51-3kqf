"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, Conflict, ValidationError, text
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
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
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

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        stats: Dict[str, Any] = dict(self.repository.stats())
        stats["borrower_changes"] = self.repository.borrower_change_stats()
        return stats

    def request_borrower_change(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_initiate_borrower_change(actor.role):
            raise PermissionDenied("角色无权发起共同借款人变更")
        data = data or {}
        record = self.repository.get(record_id)
        if self.rules.is_settled(record):
            raise Conflict("贷款已结清，不能变更共同借款人")
        after_borrowers = self.rules.validate_borrowers(data, "after_borrowers")
        reason = text(data, "reason")
        before_borrowers = record["payload"].get("borrowers")
        if not isinstance(before_borrowers, list) or len(before_borrowers) != 2:
            raise Conflict("当前贷款缺少两名还款人记录，请先补录")
        if self.rules.borrowers_equal(before_borrowers, after_borrowers):
            raise ValidationError("变更后的还款人与责任比例与当前一致，无需发起变更")
        return self.repository.create_borrower_change(
            record_id=record_id,
            before_borrowers=before_borrowers,
            after_borrowers=after_borrowers,
            reason=reason,
            actor_id=actor.user_id,
            record_version=int(record["version"]),
        )

    def _load_pending_change(self, actor: Actor, change_id: int) -> Dict[str, Any]:
        change = self.repository.get_borrower_change(change_id)
        if change["state"] != "pending":
            raise Conflict("变更单已处理")
        return change

    def review_borrower_change(self, actor: Actor, change_id: int, data: Dict[str, Any], approved: bool) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_borrower_change(actor.role):
            raise PermissionDenied("角色无权复核共同借款人变更")
        change = self._load_pending_change(actor, change_id)
        # 职责分离：发起人不能自己批（对任何角色，包括admin，都按用户身份拦截）
        if change["created_by"] == actor.user_id:
            raise PermissionDenied("发起人不能复核自己发起的变更单")
        data = data or {}
        note = text(data, "review_note")
        return self.repository.review_borrower_change(
            change_id=change_id,
            reviewer_id=actor.user_id,
            approved=approved,
            note=note,
        )

    def cancel_borrower_change(self, actor: Actor, change_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        change = self._load_pending_change(actor, change_id)
        if actor.role != "admin" and change["created_by"] != actor.user_id:
            raise PermissionDenied("只能撤销本人发起的变更单")
        return self.repository.cancel_borrower_change(change_id, actor.user_id)

    def list_borrower_changes(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_borrower_changes(record_id)

    def get_borrower_change(self, actor: Actor, change_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_borrower_change(change_id)

    def collection_list(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        """催收名单：只反映已生效的还款人与责任比例。

        待确认的共同借款人变更单不改变名单，仅以pending_change_id提示存在在途变更；
        已结清贷款不进入催收名单。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=state, limit=limit)
        active = [record for record in records if not self.rules.is_settled(record)]
        pending = self.repository.pending_change_ids([int(record["id"]) for record in active])
        items = []
        for record in active:
            items.append({
                "record_id": record["id"],
                "reference": record["reference"],
                "state": record["state"],
                "version": record["version"],
                "borrowers": record["payload"].get("borrowers", []),
                "pending_change_id": pending.get(int(record["id"])),
            })
        return {"items": items}
