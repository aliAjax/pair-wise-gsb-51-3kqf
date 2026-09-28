import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.rules import DomainRules


CREATE_DATA = {
    'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0,
    'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9,
    'borrowers': [
        {'person_id': 'P1001', 'name': '张三', 'share_pct': 60},
        {'person_id': 'P1002', 'name': '李四', 'share_pct': 40},
    ],
}
NEW_BORROWERS = [
    {'person_id': 'P1001', 'name': '张三', 'share_pct': 50},
    {'person_id': 'P3003', 'name': '王五', 'share_pct': 50},
]
INITIATOR = Actor("cs01", "intake_officer")
REVIEWER = Actor("uw01", "underwriter")
SERVICER = Actor("sv01", "servicer")
FLOW = [
    ('assess', 'intake_officer', {'assessment_note': '收入波动'}),
    ('approve', 'underwriter', {'exception_approved': False}),
    ('activate', 'servicer', {'borrower_ack': True}),
]


class BorrowerValidationTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_shares_must_total_100(self):
        bad = [
            {'person_id': 'P1001', 'name': '张三', 'share_pct': 60},
            {'person_id': 'P1002', 'name': '李四', 'share_pct': 30},
        ]
        with self.assertRaises(ValidationError):
            self.rules.validate_borrowers({'borrowers': bad})

    def test_must_have_two_distinct_borrowers(self):
        duplicate = [
            {'person_id': 'P1001', 'name': '张三', 'share_pct': 60},
            {'person_id': 'P1001', 'name': '张三', 'share_pct': 40},
        ]
        with self.assertRaises(ValidationError):
            self.rules.validate_borrowers({'borrowers': duplicate})
        single = [{'person_id': 'P1001', 'name': '张三', 'share_pct': 100}]
        with self.assertRaises(ValidationError):
            self.rules.validate_borrowers({'borrowers': single})

    def test_share_must_be_positive(self):
        zero_share = [
            {'person_id': 'P1001', 'name': '张三', 'share_pct': 100},
            {'person_id': 'P1002', 'name': '李四', 'share_pct': 0},
        ]
        with self.assertRaises(ValidationError):
            self.rules.validate_borrowers({'borrowers': zero_share})

    def test_create_without_borrowers_rejected(self):
        data = dict(CREATE_DATA)
        del data['borrowers']
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(data)


class BorrowerChangeWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-51001", CREATE_DATA)
        for action, role, data in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        self.record_id = record["id"]

    def tearDown(self):
        self.temp.cleanup()

    def _active_record(self):
        return self.service.get_record(REVIEWER, self.record_id)

    def test_request_confirm_and_collection_takes_effect(self):
        # 确认前催收名单仍为旧还款人
        roster = self.service.collection_list(REVIEWER)["items"]
        entry = next(item for item in roster if item["record_id"] == self.record_id)
        self.assertEqual([b["person_id"] for b in entry["borrowers"]], ["P1001", "P1002"])
        self.assertIsNone(entry["pending_change_id"])

        change = self.service.request_borrower_change(
            INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '离异，还款人由李四变更为王五'},
        )
        self.assertEqual(change["state"], "pending")
        self.assertEqual(change["created_by"], "cs01")
        self.assertIsNone(change["record_version_after"])

        # 待确认期间：贷款记录未变（payload/version均不动），但催收名单提示在途变更单
        record = self._active_record()
        self.assertEqual([b["person_id"] for b in record["payload"]["borrowers"]], ["P1001", "P1002"])
        self.assertEqual(record["version"], change["record_version_at_create"])
        entry = next(item for item in self.service.collection_list(REVIEWER)["items"]
                     if item["record_id"] == self.record_id)
        self.assertEqual([b["person_id"] for b in entry["borrowers"]], ["P1001", "P1002"])
        self.assertEqual(entry["pending_change_id"], change["id"])

        confirmed = self.service.review_borrower_change(
            REVIEWER, change["id"], {'review_note': '离婚协议与身份材料核对一致'}, approved=True,
        )
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["reviewed_by"], "uw01")

        # 确认后贷款版本+1，催收名单按新名单生效
        record = self._active_record()
        self.assertEqual(record["version"], change["record_version_at_create"] + 1)
        self.assertEqual([b["person_id"] for b in record["payload"]["borrowers"]], ["P1001", "P3003"])
        entry = next(item for item in self.service.collection_list(REVIEWER)["items"]
                     if item["record_id"] == self.record_id)
        self.assertEqual([b["person_id"] for b in entry["borrowers"]], ["P1001", "P3003"])
        self.assertEqual(sum(b["share_pct"] for b in entry["borrowers"]), 100.0)
        self.assertIsNone(entry["pending_change_id"])

    def test_initiator_cannot_approve_own_change_even_as_admin(self):
        change = self.service.request_borrower_change(
            INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '离异更换'},
        )
        with self.assertRaises(PermissionDenied):
            self.service.review_borrower_change(
                INITIATOR, change["id"], {'review_note': '自批'}, approved=True,
            )
        admin = Actor("cs01", "admin")
        with self.assertRaises(PermissionDenied):
            self.service.review_borrower_change(
                admin, change["id"], {'review_note': '同一用户换admin角色自批'}, approved=True,
            )
        # 变更单仍为待确认，名单未动
        self.assertEqual(self.service.get_borrower_change(REVIEWER, change["id"])["state"], "pending")

    def test_servicer_cannot_review(self):
        change = self.service.request_borrower_change(
            INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '离异更换'},
        )
        with self.assertRaises(PermissionDenied):
            self.service.review_borrower_change(
                SERVICER, change["id"], {'review_note': '催收无权复核'}, approved=True,
            )

    def test_only_one_pending_change_per_record(self):
        self.service.request_borrower_change(
            INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '离异更换'},
        )
        other = Actor("cs02", "intake_officer")
        with self.assertRaises(Conflict):
            self.service.request_borrower_change(
                other, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '重复发起'},
            )

    def test_reject_keeps_collection_unchanged(self):
        change = self.service.request_borrower_change(
            INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '材料待补'},
        )
        rejected = self.service.review_borrower_change(
            REVIEWER, change["id"], {'review_note': '缺少法律文书'}, approved=False,
        )
        self.assertEqual(rejected["state"], "rejected")
        record = self._active_record()
        self.assertEqual([b["person_id"] for b in record["payload"]["borrowers"]], ["P1001", "P1002"])
        self.assertEqual(record["version"], change["record_version_at_create"])
        # 驳回后可重新发起
        new_change = self.service.request_borrower_change(
            INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '材料补齐'},
        )
        self.assertEqual(new_change["state"], "pending")

    def test_cancel_by_initiator_allows_new_request(self):
        change = self.service.request_borrower_change(
            INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '客户撤回'},
        )
        canceled = self.service.cancel_borrower_change(INITIATOR, change["id"])
        self.assertEqual(canceled["state"], "canceled")
        other = Actor("cs02", "intake_officer")
        with self.assertRaises(PermissionDenied):
            self.service.cancel_borrower_change(other, self.service.request_borrower_change(
                INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '再次发起'},
            )["id"])

    def test_noop_change_rejected(self):
        same = [dict(b) for b in CREATE_DATA["borrowers"]]
        with self.assertRaises(ValidationError):
            self.service.request_borrower_change(
                INITIATOR, self.record_id, {'after_borrowers': same, 'reason': '无实质变化'},
            )

    def test_timeline_and_change_detail_show_before_after_and_reason(self):
        change = self.service.request_borrower_change(
            INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '离异，李四退出还款'},
        )
        self.service.review_borrower_change(
            REVIEWER, change["id"], {'review_note': '材料齐全'}, approved=True,
        )
        timeline = self.service.timeline(INITIATOR, self.record_id)
        actions = [event["action"] for event in timeline]
        self.assertIn("borrower_change_requested", actions)
        self.assertIn("borrower_change_confirmed", actions)
        confirmed_event = next(event for event in timeline if event["action"] == "borrower_change_confirmed")
        self.assertEqual(
            [b["person_id"] for b in confirmed_event["details"]["before_borrowers"]], ["P1001", "P1002"],
        )
        self.assertEqual(
            [b["person_id"] for b in confirmed_event["details"]["after_borrowers"]], ["P1001", "P3003"],
        )
        self.assertEqual(confirmed_event["details"]["reason"], "离异，李四退出还款")

        detail = self.service.get_borrower_change(REVIEWER, change["id"])
        self.assertEqual(detail["reason"], "离异，李四退出还款")
        self.assertEqual(detail["before_borrowers"], CREATE_DATA["borrowers"])
        self.assertEqual(detail["after_borrowers"], NEW_BORROWERS)
        listed = self.service.list_borrower_changes(REVIEWER, self.record_id)
        self.assertEqual([item["id"] for item in listed], [change["id"]])

    def test_stats_include_change_counts(self):
        change = self.service.request_borrower_change(
            INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '离异更换'},
        )
        stats = self.service.stats(REVIEWER)
        self.assertEqual(stats["borrower_changes"]["pending"], 1)
        self.service.review_borrower_change(
            REVIEWER, change["id"], {'review_note': '通过'}, approved=True,
        )
        stats = self.service.stats(REVIEWER)
        self.assertEqual(stats["borrower_changes"]["pending"], 0)
        self.assertEqual(stats["borrower_changes"]["confirmed"], 1)

    def test_settled_loan_rejects_change_and_pending_confirmation(self):
        change = self.service.request_borrower_change(
            INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '离异更换'},
        )
        record = self._active_record()
        record = self.service.act(SERVICER, record["id"], record["version"], "settle", {})
        self.assertEqual(record["state"], "settled")
        with self.assertRaises(Conflict):
            self.service.request_borrower_change(
                INITIATOR, self.record_id, {'after_borrowers': NEW_BORROWERS, 'reason': '结清后申请'},
            )
        with self.assertRaises(Conflict):
            self.service.review_borrower_change(
                REVIEWER, change["id"], {'review_note': '贷款已结清'}, approved=True,
            )
        # 结清贷款不出现在催收名单；旧待办只能撤销
        self.assertEqual(self.service.collection_list(REVIEWER)["items"], [])
        canceled = self.service.cancel_borrower_change(INITIATOR, change["id"])
        self.assertEqual(canceled["state"], "canceled")
