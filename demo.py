import json
import tempfile
from pathlib import Path

import evidence
from service import normalize_notice

ROOT = Path(__file__).parent
FIXTURES = ROOT / 'fixtures'

# 提取配置：每次提取都会记录该配置的摘要（config_digest）。
EXTRACTION_CONFIG = {
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

HEADERS = {
    'list': {'content-type': 'application/json; charset=utf-8'},
    'detail': {'content-type': 'text/html; charset=utf-8'},
}


def evidence_chain_demo():
    spec = json.loads((FIXTURES / 'evidence.json').read_text(encoding='utf-8'))
    pages_bytes = (FIXTURES / 'pages.json').read_bytes()
    html_bytes = (FIXTURES / 'notices.html').read_bytes()
    store = evidence.EvidenceStore()

    # 批次一：列表页 + 详情页都采到，逐字段留痕并定位。
    batch = 'batch-2026-11-11'
    list_cap = store.add_capture(batch_id=batch, source='a', format='json', body=pages_bytes,
                                 headers=HEADERS['list'], fetched_at='2026-11-11T08:05:00+08:00',
                                 config=EXTRACTION_CONFIG)
    detail_cap = store.add_capture(batch_id=batch, source='b', format='html', body=html_bytes,
                                   headers=HEADERS['detail'], fetched_at='2026-11-11T08:06:00+08:00',
                                   config=EXTRACTION_CONFIG)
    locators = {item['id']: item['locator'] for item in spec['inputs']}
    store.extract(list_cap.capture_id, input_id='list-1', field='list_title',
                  locator=locators['list-1'])
    store.extract(detail_cap.capture_id, input_id='detail-1', field='subject',
                  locator=locators['detail-1'])
    store.extract(detail_cap.capture_id, input_id='detail-1', field='decision_date',
                  locator=EXTRACTION_CONFIG['fields']['decision_date']['locator'])
    conclusion = store.build_derived(
        'conclusion',
        rule=spec['derived']['conclusion']['rule'],
        depends_on=spec['derived']['conclusion']['depends_on'],
        batch_pin=[batch])
    assert conclusion.status == 'ok', conclusion.reasons

    # 批次二（更晚到达）：只采到列表页，详情页缺失。
    # 结论必须标“证据不足”，不得用批次一的详情页或更晚的页面悄悄补齐。
    later = 'batch-2026-11-12'
    later_list = store.add_capture(batch_id=later, source='a', format='json', body=pages_bytes,
                                   headers=HEADERS['list'], fetched_at='2026-11-12T09:05:00+08:00',
                                   config=EXTRACTION_CONFIG)
    store.extract(later_list.capture_id, input_id='list-1', field='list_title',
                  locator=locators['list-1'])
    starved = store.build_derived(
        'conclusion-2026-11-12',
        rule=spec['derived']['conclusion']['rule'],
        depends_on=spec['derived']['conclusion']['depends_on'],
        batch_pin=[later])
    assert starved.status == 'insufficient_evidence' and starved.value is None

    redaction_rules = [
        {'format': EXTRACTION_CONFIG['redaction'][item['field']]['format'],
         'locator': EXTRACTION_CONFIG['redaction'][item['field']]['locator'],
         'field': item['field'], 'reason': item['reason']}
        for item in spec['redaction']
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        internal = evidence.export_package(store, 'conclusion', tmp / 'internal-package')
        review = evidence.export_package(store, 'conclusion', tmp / 'review-package',
                                         kind='review', redaction_rules=redaction_rules)
        internal_report = evidence.verify_package(internal)
        review_report = evidence.verify_package(review)
        assert internal_report.package_valid and internal_report.proof_status == 'ok'
        assert review_report.package_valid and review_report.proof_status == 'ok'

        detail_raw_name = f'raw/{detail_cap.capture_id}.bin'
        internal_raw = (internal / detail_raw_name).read_bytes()
        review_raw = (review / detail_raw_name).read_text(encoding='utf-8')
        redactions = json.loads((review / 'redaction.json').read_text(encoding='utf-8'))['redactions']

        # 导出中断：目标目录不存在，只剩 *.tmp 半成品，复核判为 incomplete-export。
        try:
            evidence.export_package(store, 'conclusion', tmp / 'interrupted-package',
                                    _fail_after=2)
            raise AssertionError('应当抛出 ExportInterrupted')
        except evidence.ExportInterrupted:
            pass
        assert not (tmp / 'interrupted-package').exists()
        interrupted_dest = evidence.verify_package(tmp / 'interrupted-package')
        interrupted_tmp = evidence.verify_package(
            tmp / ('interrupted-package' + evidence.TMP_SUFFIX))

        return {
            'batch_pin': list(conclusion.batch_pin),
            'conclusion': conclusion.to_dict(),
            'insufficient_without_detail': starved.to_dict(),
            'internal_package': internal_report.to_dict(),
            'review_package': review_report.to_dict(),
            'redactions': redactions,
            'internal_raw_keeps_contact': b'fictional@example.invalid' in internal_raw,
            'review_raw_masks_contact': (
                'fictional@example.invalid' not in review_raw
                and '[REDACTED:contact]' in review_raw),
            'interrupted_export': {
                'destination': interrupted_dest.to_dict(),
                'leftover_tmp': interrupted_tmp.to_dict(),
            },
        }


def run():
    pages = json.loads((FIXTURES / 'pages.json').read_text(encoding='utf-8'))
    notices = [normalize_notice(item) for page in pages['rounds'] for item in page['items']]
    assert len(notices) == 2 and notices[0]['notice_id'] == notices[1]['notice_id']
    assert notices[0]['title'] != notices[1]['title']
    evidence_spec = json.loads((FIXTURES / 'evidence.json').read_text(encoding='utf-8'))
    assert len(evidence_spec['derived']['conclusion']['depends_on']) == 2
    chain = evidence_chain_demo()
    return {
        'normalized': notices,
        'html_fixture': 'fixtures/notices.html',
        'evidence_scenario': evidence_spec,
        'evidence_chain': chain,
    }


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
