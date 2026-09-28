"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "submitted"
SETTLED_STATE = "settled"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'assess': {'intake_officer'}, 'approve': {'underwriter'}, 'activate': {'servicer'}, 'cure': {'servicer'}, 'default': {'servicer'}, 'settle': {'servicer'}}
TRANSITIONS = {'assess': {'submitted': 'assessed'}, 'approve': {'assessed': 'approved'}, 'activate': {'approved': 'active'}, 'cure': {'active': 'cured'}, 'default': {'active': 'defaulted'}, 'settle': {'active': SETTLED_STATE, 'cured': SETTLED_STATE}}
BORROWER_COUNT = 2
BORROWER_CHANGE_INITIATE_ROLES = {'servicer'}
BORROWER_CHANGE_REVIEW_ROLES = {'servicer'}
COLLECTION_STATES = ('active', 'defaulted')


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_initiate_borrower_change(self, role: str) -> bool:
        return role == "admin" or role in BORROWER_CHANGE_INITIATE_ROLES

    def role_can_review_borrower_change(self, role: str) -> bool:
        return role == "admin" or role in BORROWER_CHANGE_REVIEW_ROLES

    def is_settled(self, record: Dict[str, Any]) -> bool:
        return record["state"] == SETTLED_STATE

    @staticmethod
    def validate_borrowers(payload: Dict[str, Any], key: str = "borrowers") -> List[Dict[str, Any]]:
        """每笔贷款固定记录两名还款人，责任比例（0-100）合计必须达到100%。"""
        value = payload.get(key)
        if not isinstance(value, list) or len(value) != BORROWER_COUNT:
            raise ValidationError("贷款必须记录两名还款人")
        borrowers: List[Dict[str, Any]] = []
        person_ids = set()
        total = 0.0
        for index in range(BORROWER_COUNT):
            item = value[index]
            if not isinstance(item, dict):
                raise ValidationError("还款人信息必须是对象")
            person_id = text(item, "person_id")
            name = text(item, "name")
            share = number(item, "share", 0, 100)
            if person_id in person_ids:
                raise ValidationError("两名还款人不能是同一人")
            person_ids.add(person_id)
            total += share
            borrowers.append({
                "person_id": person_id,
                "name": name,
                "share": round(share, 2),
                "kind": "primary" if index == 0 else "secondary",
            })
        if abs(round(total, 2) - 100.0) > 0.01:
            raise ValidationError("两名还款人责任比例合计必须达到100%")
        return borrowers

    def prepare_borrower_change(self, record: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        if self.is_settled(record):
            raise Conflict("贷款已结清，不允许变更共同借款人")
        new_borrowers = self.validate_borrowers(data)
        current = record["payload"].get("borrowers", [])
        current_key = [(item["person_id"], item["name"], item["share"]) for item in current]
        new_key = [(item["person_id"], item["name"], item["share"]) for item in new_borrowers]
        if current_key == new_key:
            raise Conflict("变更后还款人名单与当前一致，无需发起变更")
        reason = text(data or {}, "reason")
        return {"borrowers": new_borrowers, "reason": reason}

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        income = number(p, "monthly_income", 1)
        number(p, "monthly_expenses", 0)
        payment = number(p, "monthly_payment", 0)
        number(p, "arrears", 0)
        number(p, "hardship_factor", 0, 1)
        choice(p, "program_type", ["deferral", "reduction", "restructure"])
        integer(p, "requested_months", 1, 24)
        if p["monthly_expenses"] >= income:
            raise ValidationError("支出不能达到或超过收入")
        p["borrowers"] = self.validate_borrowers(p)
        p["borrower_id"] = p["borrowers"][0]["person_id"]
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        income = float(p["monthly_income"])
        disposable = income - float(p["monthly_expenses"])
        ratio = float(p["monthly_payment"]) / income
        months = min(int(p["requested_months"]), 12)
        if p["program_type"] == "deferral":
            proposed = 0.0
        elif p["program_type"] == "reduction":
            proposed = max(0.0, float(p["monthly_payment"]) - disposable * 0.4)
        else:
            proposed = max(float(p["monthly_payment"]) * 0.7, disposable * 0.25)
        p["disposable_income"] = round(disposable, 2)
        p["housing_ratio"] = round(ratio, 3)
        p["eligible_months"] = months
        p["proposed_payment"] = round(proposed, 2)
        p["risk_score"] = round(min(100.0, ratio * 60 + float(p["hardship_factor"]) * 40), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "approved", "assessed"} and item["payload"].get("borrower_id") == payload.get("borrower_id"):
                raise Conflict("该借款人已有处理中纾困申请")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "assess":
            changes["assessment_note"] = text(data, "assessment_note")
            changes["eligibility"] = bool(float(p["housing_ratio"]) <= 0.8 and float(p["arrears"]) <= float(p["monthly_payment"]) * 6)
            summary = "偿付能力评估完成"
        elif action == "approve":
            exception = boolean(data, "exception_approved")
            if not p.get("eligibility") and not exception:
                raise ValidationError("不符合纾困资格且无例外批准")
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = float(p["proposed_payment"])
            changes["exception_approved"] = exception
            summary = "纾困方案批准"
        elif action == "activate":
            if not boolean(data, "borrower_ack"):
                raise ValidationError("借款人尚未确认方案")
            changes["borrower_ack"] = True
            summary = "纾困方案生效"
        elif action == "cure":
            if not boolean(data, "arrears_cleared"):
                raise ValidationError("欠款尚未清偿")
            changes["arrears_cleared"] = True
            summary = "贷款恢复正常"
        elif action == "default":
            changes["default_reason"] = text(data, "default_reason")
            summary = "纾困方案违约"
        elif action == "settle":
            changes["settle_note"] = text(data, "settle_note")
            summary = "贷款已结清"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
