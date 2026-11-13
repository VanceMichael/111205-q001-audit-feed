import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import demo
import evidence

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures'
PAGES = FIXTURES / 'pages.json'
NOTICES = FIXTURES / 'notices.html'

CONFIG = {
    'name': 'penalty-notice-extraction',
    'version': '2026-11-11',
    'fields': {
        'list_title': {'locator': '/rounds/0/items/0/title'},
        'subject': {'locator': 'li[data-id="42"] a'},
        'decision_date': {'locator': 'li[data-id="42"] time@datetime'},
    },
    'redaction': {'contact': {'format': 'html', 'locator': 'span.contact'}},
    'derived_rule': 'compare-subject-and-date',
}

LIST_LOCATOR = '/rounds/0/items/0/title'
SUBJECT_LOCATOR = 'li[data-id="42"] a'
DATE_LOCATOR = 'li[data-id="42"] time@datetime'
REDACTION_RULES = [
    {'format': 'html', 'locator': 'span.contact', 'field': 'contact',
     'reason': 'reviewer-copy-only'},
]
CONTACT = b'fictional@example.invalid'


def _codes(report):
    return {failure['code'] for failure in report.failures}


def _is_successful_proof(report):
    return report.package_valid and report.proof_status == 'ok'


def _store_with_batch(batch='batch-1', html_body=None):
    """完整批次：列表页 + 详情页，逐字段留痕。"""
    store = evidence.EvidenceStore()
    list_cap = store.add_capture(batch_id=batch, source='a', format='json',
                                 body=PAGES.read_bytes(),
                                 headers={'content-type': 'application/json'},
                                 fetched_at='2026-11-11T08:05:00+08:00', config=CONFIG)
    detail_cap = store.add_capture(batch_id=batch, source='b', format='html',
                                   body=html_body if html_body is not None else NOTICES.read_bytes(),
                                   headers={'content-type': 'text/html'},
                                   fetched_at='2026-11-11T08:06:00+08:00', config=CONFIG)
    fields = (
        store.extract(list_cap.capture_id, input_id='list-1', field='list_title',
                      locator=LIST_LOCATOR),
        store.extract(detail_cap.capture_id, input_id='detail-1', field='subject',
                      locator=SUBJECT_LOCATOR),
        store.extract(detail_cap.capture_id, input_id='detail-1', field='decision_date',
                      locator=DATE_LOCATOR),
    )
    return store, list_cap, detail_cap, fields


class ExtractionProvenanceTests(unittest.TestCase):
    def test_capture_records_raw_bytes_headers_moment_and_config_digest(self):
        _, list_cap, _, _ = _store_with_batch()
        self.assertEqual(list_cap.body, PAGES.read_bytes())
        self.assertEqual(list_cap.headers['content-type'], 'application/json')
        self.assertEqual(list_cap.fetched_at, '2026-11-11T08:05:00+08:00')
        self.assertTrue(list_cap.config_digest)
        meta = list_cap.meta_dict()
        self.assertEqual(meta['body_sha256'], hashlib.sha256(PAGES.read_bytes()).hexdigest())

    def test_fields_locate_json_pointer_and_html_element_with_raw_fragment(self):
        _, _, _, (f_title, f_subject, f_date) = _store_with_batch()
        self.assertEqual(f_title.locator_kind, 'json-pointer')
        self.assertEqual(f_title.locator, LIST_LOCATOR)
        self.assertEqual(f_title.value, ' 调查公告 ')
        self.assertEqual(json.loads(f_title.raw_fragment), ' 调查公告 ')
        self.assertEqual(f_subject.locator_kind, 'css')
        self.assertEqual(f_subject.value, '合成处罚公告')
        self.assertIn('<a href="/notice/42">', f_subject.raw_fragment)
        self.assertIn('合成处罚公告', f_subject.raw_fragment)
        self.assertEqual(f_date.value, '2026-11-11T08:00:00-05:00')
        self.assertIn('datetime="2026-11-11T08:00:00-05:00"', f_date.raw_fragment)


class DerivedEvidenceTests(unittest.TestCase):
    def test_conclusion_links_list_and_detail_inputs(self):
        store, _, _, fields = _store_with_batch()
        rec = store.build_derived('conclusion', rule='compare-subject-and-date',
                                  depends_on=['list-1', 'detail-1'], batch_pin=['batch-1'])
        self.assertEqual(rec.status, 'ok')
        self.assertEqual(rec.value['subject'], '合成处罚公告')
        self.assertEqual(rec.value['decision_date'], '2026-11-11T08:00:00-05:00')
        self.assertEqual(set(rec.evidence_refs), {f.evidence_id for f in fields})

    def test_missing_detail_marks_insufficient_without_backfill(self):
        store, _, _, _ = _store_with_batch(batch='batch-1')
        # 更晚的批次只采到列表页（缺详情页）。
        later_list = store.add_capture(batch_id='batch-2', source='a', format='json',
                                       body=PAGES.read_bytes(), headers={},
                                       fetched_at='2026-11-12T09:05:00+08:00', config=CONFIG)
        store.extract(later_list.capture_id, input_id='list-1', field='list_title',
                      locator=LIST_LOCATOR)
        rec = store.build_derived('conclusion-2', rule='compare-subject-and-date',
                                  depends_on=['list-1', 'detail-1'], batch_pin=['batch-2'])
        self.assertEqual(rec.status, 'insufficient_evidence')
        self.assertIn('missing-input:detail-1', rec.reasons)
        self.assertIsNone(rec.value)
        # batch-1 的详情证据仍在库里，但不得被悄悄并入 batch-2 的结论。
        self.assertTrue(rec.evidence_refs)
        self.assertTrue(all(store.get_field(ref).batch_id == 'batch-2'
                            for ref in rec.evidence_refs))

    def test_conflicting_subject_names_mark_insufficient(self):
        revised = NOTICES.read_text(encoding='utf-8').replace(
            '合成处罚公告</a>', '合成处罚公告（修订）</a>').encode('utf-8')
        store, _, _, _ = _store_with_batch(batch='batch-3', html_body=revised)
        attachment = store.add_capture(
            batch_id='batch-3', source='b', format='html',
            body='<div class="attachment"><span class="subject">合成处罚公告</span></div>'.encode('utf-8'),
            headers={}, fetched_at='2026-11-11T08:07:00+08:00', config=CONFIG)
        store.extract(attachment.capture_id, input_id='attachment-1', field='subject',
                      locator='span.subject')
        rec = store.build_derived('conclusion-3', rule='compare-subject-and-date',
                                  depends_on=['detail-1', 'attachment-1'],
                                  batch_pin=['batch-3'])
        self.assertEqual(rec.status, 'insufficient_evidence')
        self.assertTrue(any(r.startswith('conflicting-subject:') for r in rec.reasons))
        self.assertIsNone(rec.value)

    def test_same_value_from_different_batches_is_not_merged(self):
        store, _, _, fields = _store_with_batch(batch='batch-1')
        later_list = store.add_capture(batch_id='batch-2', source='a', format='json',
                                       body=PAGES.read_bytes(), headers={},
                                       fetched_at='2026-11-12T09:05:00+08:00', config=CONFIG)
        later_field = store.extract(later_list.capture_id, input_id='list-1',
                                    field='list_title', locator=LIST_LOCATOR)
        # 取值相同，但证据身份不同：不能混成一份证明。
        self.assertEqual(fields[0].value, later_field.value)
        self.assertNotEqual(fields[0].evidence_id, later_field.evidence_id)
        rec = store.build_derived('conclusion', rule='compare-subject-and-date',
                                  depends_on=['list-1', 'detail-1'], batch_pin=['batch-1'])
        self.assertNotIn(later_field.evidence_id, rec.evidence_refs)
        self.assertTrue(all(store.get_field(ref).batch_id == 'batch-1'
                            for ref in rec.evidence_refs))


class PackageExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store, self.list_cap, self.detail_cap, self.fields = _store_with_batch()
        self.rec = self.store.build_derived(
            'conclusion', rule='compare-subject-and-date',
            depends_on=['list-1', 'detail-1'], batch_pin=['batch-1'])

    def _export(self, name='pkg', **kwargs):
        return evidence.export_package(self.store, 'conclusion', self.tmp / name, **kwargs)

    def test_export_verifies_offline_and_recomputes_display_value(self):
        pkg = self._export()
        report = evidence.verify_package(pkg)
        self.assertTrue(report.package_valid, report.failures)
        self.assertEqual(report.proof_status, 'ok')
        self.assertEqual(report.recomputed_status, 'ok')
        for check in ('digests', 'closure', 'locators', 'derived'):
            self.assertEqual(report.checks[check], 'pass')
        derived = json.loads((pkg / 'derived.json').read_text(encoding='utf-8'))
        self.assertEqual(derived['value']['subject'], '合成处罚公告')
        manifest = json.loads((pkg / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['batch_pin'], ['batch-1'])
        for rel, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((pkg / rel).read_bytes()).hexdigest(), digest)

    def test_new_capture_during_export_does_not_leak_into_selected_batch(self):
        def late_arrival():
            cap = self.store.add_capture(batch_id='batch-9', source='b', format='html',
                                         body=b'<ul></ul>', headers={},
                                         fetched_at='2026-11-13T00:00:00+08:00', config=CONFIG)
            self.store.extract(cap.capture_id, input_id='detail-1', field='subject',
                               locator='ul')

        pkg = self._export(on_selected=late_arrival)
        manifest = json.loads((pkg / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['batch_pin'], ['batch-1'])
        raw_ids = {p.name[:-len('.bin')] for p in (pkg / 'raw').glob('*.bin')}
        self.assertEqual(raw_ids, {self.list_cap.capture_id, self.detail_cap.capture_id})
        self.assertTrue(evidence.verify_package(pkg).package_valid)

    def test_tampered_raw_file_is_detected(self):
        pkg = self._export()
        target = pkg / 'raw' / f'{self.detail_cap.capture_id}.bin'
        target.write_bytes(target.read_bytes().replace(CONTACT, CONTACT + b'!'))
        report = evidence.verify_package(pkg)
        self.assertFalse(report.package_valid)
        self.assertIn('tampered-file', _codes(report))

    def test_missing_dependency_is_detected(self):
        pkg = self._export()
        (pkg / 'fields' / f'{self.fields[1].evidence_id}.json').unlink()
        report = evidence.verify_package(pkg)
        self.assertFalse(report.package_valid)
        self.assertIn('missing-dependency', _codes(report))

    def test_stale_locator_is_detected_after_page_redesign(self):
        pkg = self._export()
        redesigned = NOTICES.read_text(encoding='utf-8').replace(
            '合成处罚公告</a>', '合成处罚公告（修订）</a>')
        (pkg / 'raw' / f'{self.detail_cap.capture_id}.bin').write_text(
            redesigned, encoding='utf-8')
        # 重新封账让摘要一致：暴露的是定位与留痕的矛盾，而不是摘要不符。
        evidence.reseal_package(pkg)
        report = evidence.verify_package(pkg)
        self.assertFalse(report.package_valid)
        self.assertIn('stale-locator', _codes(report))
        self.assertNotIn('tampered-file', _codes(report))

    def test_interrupted_export_leaves_no_complete_looking_package(self):
        dest = self.tmp / 'interrupted'
        with self.assertRaises(evidence.ExportInterrupted):
            evidence.export_package(self.store, 'conclusion', dest, _fail_after=2)
        self.assertFalse(dest.exists())
        leftover = self.tmp / ('interrupted' + evidence.TMP_SUFFIX)
        self.assertTrue(leftover.is_dir())
        for path in (dest, leftover):
            report = evidence.verify_package(path)
            self.assertFalse(report.package_valid)
            self.assertIn('incomplete-export', _codes(report))


class ReviewCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store, _, self.detail_cap, _ = _store_with_batch()
        self.store.build_derived('conclusion', rule='compare-subject-and-date',
                                 depends_on=['list-1', 'detail-1'], batch_pin=['batch-1'])

    def test_review_copy_masks_contact_and_explains_redaction(self):
        pkg = evidence.export_package(self.store, 'conclusion', self.tmp / 'review',
                                      kind='review', redaction_rules=REDACTION_RULES)
        raw = (pkg / 'raw' / f'{self.detail_cap.capture_id}.bin').read_text(encoding='utf-8')
        self.assertNotIn(CONTACT.decode(), raw)
        self.assertIn('[REDACTED:contact]', raw)
        log = json.loads((pkg / 'redaction.json').read_text(encoding='utf-8'))['redactions']
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]['field'], 'contact')
        self.assertEqual(log[0]['reason'], 'reviewer-copy-only')
        self.assertEqual(log[0]['locator'], 'span.contact')
        self.assertIn('redacted_fragment_sha256', log[0])
        report = evidence.verify_package(pkg)
        self.assertTrue(report.package_valid, report.failures)
        self.assertEqual(report.checks['redaction'], 'pass')

    def test_review_export_never_overwrites_internal_evidence(self):
        internal = evidence.export_package(self.store, 'conclusion', self.tmp / 'pkg')
        with self.assertRaises(FileExistsError):
            evidence.export_package(self.store, 'conclusion', self.tmp / 'pkg',
                                    kind='review', redaction_rules=REDACTION_RULES)
        raw_name = f'raw/{self.detail_cap.capture_id}.bin'
        self.assertIn(CONTACT, (internal / raw_name).read_bytes())
        self.assertTrue(evidence.verify_package(internal).package_valid)
        review = evidence.export_package(self.store, 'conclusion', self.tmp / 'pkg-review',
                                         kind='review', redaction_rules=REDACTION_RULES)
        self.assertNotIn(CONTACT, (review / raw_name).read_bytes())
        self.assertIn(CONTACT, (internal / raw_name).read_bytes())


class ProofSeparationTests(unittest.TestCase):
    def test_successful_and_failed_proofs_are_not_confused(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        store, _, detail_cap, _ = _store_with_batch()
        store.build_derived('conclusion-ok', rule='compare-subject-and-date',
                            depends_on=['list-1', 'detail-1'], batch_pin=['batch-1'])
        ok_pkg = evidence.export_package(store, 'conclusion-ok', tmp / 'ok')
        ok_report = evidence.verify_package(ok_pkg)
        self.assertTrue(_is_successful_proof(ok_report))

        # 失败的证明①：证据不足（缺详情页），包体本身完好。
        later_list = store.add_capture(batch_id='batch-2', source='a', format='json',
                                       body=PAGES.read_bytes(), headers={},
                                       fetched_at='2026-11-12T09:05:00+08:00', config=CONFIG)
        store.extract(later_list.capture_id, input_id='list-1', field='list_title',
                      locator=LIST_LOCATOR)
        store.build_derived('conclusion-weak', rule='compare-subject-and-date',
                            depends_on=['list-1', 'detail-1'], batch_pin=['batch-2'])
        weak_report = evidence.verify_package(
            evidence.export_package(store, 'conclusion-weak', tmp / 'weak'))
        self.assertTrue(weak_report.package_valid)
        self.assertEqual(weak_report.proof_status, 'insufficient_evidence')
        self.assertFalse(_is_successful_proof(weak_report))

        # 失败的证明②：包被篡改。
        target = ok_pkg / 'raw' / f'{detail_cap.capture_id}.bin'
        target.write_bytes(target.read_bytes() + b' ')
        tampered_report = evidence.verify_package(ok_pkg)
        self.assertFalse(_is_successful_proof(tampered_report))

        outcomes = {
            (ok_report.package_valid, ok_report.proof_status),
            (weak_report.package_valid, weak_report.proof_status),
            (tampered_report.package_valid, tampered_report.proof_status),
        }
        self.assertEqual(len(outcomes), 3)


class DemoIntegrationTests(unittest.TestCase):
    def test_demo_evidence_chain_summary(self):
        chain = demo.run()['evidence_chain']
        self.assertTrue(chain['internal_package']['package_valid'])
        self.assertEqual(chain['internal_package']['proof_status'], 'ok')
        self.assertTrue(chain['review_package']['package_valid'])
        self.assertTrue(chain['internal_raw_keeps_contact'])
        self.assertTrue(chain['review_raw_masks_contact'])
        self.assertEqual(chain['redactions'][0]['field'], 'contact')
        self.assertEqual(chain['insufficient_without_detail']['status'],
                         'insufficient_evidence')
        self.assertIn('incomplete-export',
                      {f['code'] for f in chain['interrupted_export']['leftover_tmp']['failures']})


if __name__ == '__main__':
    unittest.main()
