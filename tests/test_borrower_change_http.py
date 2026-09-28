import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from app import build_service
from src.http_api import create_server
from src.domain import Actor


CREATE_DATA = {
    'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0,
    'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9,
    'borrowers': [
        {'person_id': 'P1001', 'name': '张三', 'share_pct': 60},
        {'person_id': 'P1002', 'name': '李四', 'share_pct': 40},
    ],
}
FLOW = [
    ('assess', 'intake_officer', {'assessment_note': '收入波动'}),
    ('approve', 'underwriter', {'exception_approved': False}),
    ('activate', 'servicer', {'borrower_ack': True}),
]


class BorrowerChangeHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "http-test.db"))
        import socket
        self.server = create_server("127.0.0.1", 0, service, Path(__file__).resolve().parent.parent / "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.service = service
        record = service.create(Actor("creator", "intake_officer"), "MORT-77001", CREATE_DATA)
        for action, role, data in FLOW:
            record = service.act(Actor("operator", role), record["id"], record["version"], action, data)
        self.record_id = record["id"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _request(self, method: str, path: str, user: str, role: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=data, method=method,
            headers={"X-User-Id": user, "X-Role": role, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_change_request_confirm_collection_flow(self):
        payload = {
            "data": {
                "after_borrowers": [
                    {"person_id": "P1001", "name": "张三", "share_pct": 50},
                    {"person_id": "P3003", "name": "王五", "share_pct": 50},
                ],
                "reason": "夫妻离异，共同还款人变更",
            }
        }
        status, change = self._request("POST", "/api/records/%s/borrower-changes" % self.record_id, "cs01", "intake_officer", payload)
        self.assertEqual(status, 201)
        change_id = change["id"]

        # 催收角色无权发起
        status, body = self._request("POST", "/api/records/%s/borrower-changes" % self.record_id, "sv01", "servicer", payload)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "permission_denied")

        # 发起人不能自己批
        status, body = self._request("POST", "/api/borrower-changes/%s/confirm" % change_id, "cs01", "intake_officer",
                                     {"data": {"review_note": "自批"}})
        self.assertEqual(status, 403)

        # 另一复核人确认
        status, confirmed = self._request("POST", "/api/borrower-changes/%s/confirm" % change_id, "uw01", "underwriter",
                                          {"data": {"review_note": "材料核对一致"}})
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["state"], "confirmed")

        # 详情可查变更前后责任人和原因
        status, detail = self._request("GET", "/api/borrower-changes/%s" % change_id, "cs01", "intake_officer")
        self.assertEqual(status, 200)
        self.assertEqual(detail["before_borrowers"][1]["person_id"], "P1002")
        self.assertEqual(detail["after_borrowers"][1]["person_id"], "P3003")

        # 催收名单已按新名单生效
        status, roster = self._request("GET", "/api/collection", "sv01", "servicer")
        self.assertEqual(status, 200)
        entry = next(item for item in roster["items"] if item["record_id"] == self.record_id)
        self.assertEqual([b["person_id"] for b in entry["borrowers"]], ["P1001", "P3003"])
        self.assertIsNone(entry["pending_change_id"])

        # 审计时间线包含申请与确认事件
        status, timeline = self._request("GET", "/api/records/%s/audit" % self.record_id, "cs01", "intake_officer")
        self.assertEqual(status, 200)
        self.assertIn("borrower_change_requested", [event["action"] for event in timeline["items"]])
        self.assertIn("borrower_change_confirmed", [event["action"] for event in timeline["items"]])

        # 统计含变更单状态分布
        status, stats = self._request("GET", "/api/stats", "cs01", "intake_officer")
        self.assertEqual(status, 200)
        self.assertEqual(stats["borrower_changes"]["confirmed"], 1)


if __name__ == "__main__":
    unittest.main()
