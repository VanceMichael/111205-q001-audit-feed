import unittest
from decimal import Decimal
from service import normalize_notice

class AuditFeedTests(unittest.TestCase):
    def test_normalizes_notice(self):
        result = normalize_notice({'source': 'A', 'notice_id': ' 42 ', 'title': ' Notice ', 'published_at': '2026-11-11'})
        self.assertEqual(result['notice_id'], '42')

    def test_rejects_missing_identity(self):
        with self.assertRaises(ValueError):
            normalize_notice({'source': 'A'})


if __name__ == '__main__':
    unittest.main()
