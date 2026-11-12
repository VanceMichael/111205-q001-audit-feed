import json
from pathlib import Path
from service import normalize_notice


def run():
    pages = json.loads((Path(__file__).parent / 'fixtures/pages.json').read_text())
    notices = [normalize_notice(item) for page in pages['rounds'] for item in page['items']]
    assert len(notices) == 2 and notices[0]['notice_id'] == notices[1]['notice_id']
    assert notices[0]['title'] != notices[1]['title']
    evidence = json.loads((Path(__file__).parent / 'fixtures/evidence.json').read_text())
    assert len(evidence['derived']['conclusion']['depends_on']) == 2
    return {'normalized': notices, 'html_fixture': 'fixtures/notices.html', 'evidence_scenario': evidence}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
