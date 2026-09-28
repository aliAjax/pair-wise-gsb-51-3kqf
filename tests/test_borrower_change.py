import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {
    'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0,
    'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9,
    'borrowers': [{'person_id': 'P-1001', 'name': '张三', 'share': 60.0},
                  {'person_id': 'P-1002', 'name': '李四', 'share': 40.0}],
}
NEW_BORROWERS = [{'person_id': 'P-1001', 'name': '张三', 'share': 100.0},
                 {'person_id': 'P-1003', 'name': '王五', 'share': 0.0}]
FLOW = [('assess', 'intake_officer', {'assessment_note': '收入波动'}),
        ('approve', 'underwriter', {'exception_approved': False}),
        ('activate', 'servicer', {'borrower_ack': True})]

CS1 = Actor('svc-1', 'servicer')
CS2 = Actor('svc-2', 'servicer')


class BorrowerChangeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        record = self.service.create(Actor('creator', 'intake_officer'), 'MORT-28001', CREATE_DATA)
        for action, role, data in FLOW:
            record = self.service.act(Actor('operator', role), record['id'], record['version'], action, data)
        self.record = record

    def tearDown(self):
        self.temp.cleanup()

    def _initiate(self, borrowers=NEW_BORROWERS, actor=CS1, reason='离异，李四退出还款'):
        return self.service.initiate_borrower_change(
            actor, self.record['id'], self.record['version'],
            {'borrowers': borrowers, 'reason': reason},
        )

    def test_create_requires_two_borrowers_summing_100(self):
        bad = dict(CREATE_DATA)
        bad['borrowers'] = [{'person_id': 'P-2001', 'name': '赵六', 'share': 70.0},
                            {'person_id': 'P-2002', 'name': '钱七', 'share': 20.0}]
        with self.assertRaises(ValidationError):
            self.service.create(Actor('creator', 'intake_officer'), 'MORT-28002', bad)
        bad['borrowers'] = [{'person_id': 'P-2001', 'name': '赵六', 'share': 100.0}]
        with self.assertRaises(ValidationError):
            self.service.create(Actor('creator', 'intake_officer'), 'MORT-28002', bad)

    def test_initiate_and_confirm_flow(self):
        change = self._initiate()
        self.assertEqual(change['status'], 'pending')
        self.assertEqual(change['requested_by'], 'svc-1')
        self.assertIsNone(change['reviewed_by'])
        # 确认前催收名单仍按旧责任人与份额
        collections = self.service.collections(CS1)
        target = collections[0]['collection_targets']
        self.assertEqual([t['person_id'] for t in target], ['P-1001', 'P-1002'])
        self.assertEqual(target[0]['responsibility_amount'], 4200.0)
        self.assertEqual(target[1]['responsibility_amount'], 2800.0)
        # 发起人不能自己批
        with self.assertRaises(PermissionDenied):
            self.service.review_borrower_change(CS1, change['id'], True, {'review_note': '自批无效'})
        confirmed = self.service.review_borrower_change(CS2, change['id'], True, {'review_note': '材料齐全'})
        self.assertEqual(confirmed['status'], 'confirmed')
        self.assertEqual(confirmed['reviewed_by'], 'svc-2')
        # 确认后按新名单生效
        record = self.service.get_record(CS1, self.record['id'])
        self.assertEqual([b['person_id'] for b in record['payload']['borrowers']], ['P-1001', 'P-1003'])
        self.assertIsNone(record['pending_borrower_change'])
        target = self.service.collections(CS1)[0]['collection_targets']
        self.assertEqual([t['person_id'] for t in target], ['P-1001', 'P-1003'])
        self.assertEqual(target[0]['responsibility_amount'], 7000.0)
        self.assertEqual(target[1]['responsibility_amount'], 0.0)

    def test_only_one_pending_change_allowed(self):
        self._initiate()
        with self.assertRaises(Conflict):
            self._initiate(actor=CS2)
        # 驳回后可以重新发起
        change = self.service.borrower_changes(CS1, self.record['id'])[0]
        self.service.review_borrower_change(CS2, change['id'], False, {'review_note': '材料不足'})
        second = self._initiate()
        self.assertEqual(second['status'], 'pending')
        changes = self.service.borrower_changes(CS1, self.record['id'])
        self.assertEqual([c['status'] for c in changes], ['rejected', 'pending'])

    def test_other_roles_cannot_initiate_or_review(self):
        with self.assertRaises(PermissionDenied):
            self.service.initiate_borrower_change(
                Actor('uw', 'underwriter'), self.record['id'], self.record['version'],
                {'borrowers': NEW_BORROWERS, 'reason': 'x'},
            )

    def test_identical_change_rejected(self):
        same = [dict(b) for b in CREATE_DATA['borrowers']]
        with self.assertRaises(Conflict):
            self._initiate(borrowers=same)

    def test_settle_blocked_while_pending_and_forbidden_after_settle(self):
        change = self._initiate()
        with self.assertRaises(Conflict):
            self.service.act(CS1, self.record['id'], self.record['version'], 'settle', {'settle_note': '提前结清'})
        self.service.review_borrower_change(CS2, change['id'], True, {'review_note': 'ok'})
        settled = self.service.act(CS1, self.record['id'], self.record['version'] + 1, 'settle', {'settle_note': '结清'})
        self.assertEqual(settled['state'], 'settled')
        self.assertEqual(self.service.collections(CS1), [])
        with self.assertRaises(Conflict):
            self.service.initiate_borrower_change(
                CS1, self.record['id'], settled['version'],
                {'borrowers': NEW_BORROWERS, 'reason': '已结清不应变更'},
            )

    def test_audit_timeline_shows_before_after_and_reason(self):
        change = self._initiate()
        self.service.review_borrower_change(CS2, change['id'], True, {'review_note': '确认'})
        timeline = self.service.timeline(CS1, self.record['id'])
        actions = [event['action'] for event in timeline]
        self.assertIn('borrower_change_requested', actions)
        confirmed = next(event for event in timeline if event['action'] == 'borrower_change_confirmed')
        self.assertEqual(confirmed['details']['reason'], '离异，李四退出还款')
        self.assertEqual(confirmed['details']['old_borrowers'][1]['person_id'], 'P-1002')
        self.assertEqual(confirmed['details']['new_borrowers'][1]['person_id'], 'P-1003')

    def test_stats_include_changes_and_responsibility(self):
        change = self._initiate()
        self.service.review_borrower_change(CS2, change['id'], True, {'review_note': '确认'})
        stats = self.service.stats(CS1)
        self.assertEqual(stats['borrower_changes'], {'confirmed': 1})
        persons = {item['person_id']: item for item in stats['responsibilities']}
        self.assertNotIn('P-1002', persons)
        self.assertEqual(persons['P-1001']['outstanding_responsibility'], 7000.0)
        self.assertEqual(persons['P-1003']['outstanding_responsibility'], 0.0)
