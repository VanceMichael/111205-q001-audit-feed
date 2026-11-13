"""处罚公告采集的可离线复核证据链。

在现有公告规范化入口（service.normalize_notice）旁提供一条证据链：

* 抓取留痕：每次提取保存原始响应字节、响应头、抓取时刻与提取配置摘要；
* 字段定位：结构化字段记录 JSON 指针或 HTML 元素定位，并保留该位置的原始片段；
* 派生结论：把列表页、详情页、附件说明等输入依赖连成闭包；缺页或主体名称
  互相矛盾时标记“证据不足”，不用其他抓取批次的页面悄悄补齐旧结论；
* 证据包导出：按一次复核导出独立证据包，批次钉选、清单摘要、原子落盘；
  审阅副本遮盖联系人信息并记录遮盖说明，内部完整证据不被审阅导出覆盖；
* 离线复核：在断网环境校验文件摘要、依赖闭包与字段定位，并重新算出展示值。

失败的可观察结果（verify_package 报告 failures[].code）：

* tampered-file      文件摘要与清单不一致（篡改文件）
* missing-dependency 清单或结论引用的文件缺失（缺失依赖）
* stale-locator      定位器在原始响应上重放失败或片段不一致（定位失效）
* incomplete-export  目录缺少 manifest.json（导出中断留下的半成品）
* derived-mismatch   按留痕重算的展示值与记录结论不一致
* redaction-mismatch 审阅副本中被遮盖的位置与遮盖记录不符
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from dataclasses import field as _dc_field
from html.parser import HTMLParser
from pathlib import Path

PACKAGE_FORMAT = 'evidence-package/1'
TMP_SUFFIX = '.tmp'
REDACT_MARKER_TEMPLATE = '[REDACTED:{field}]'


class LocatorError(ValueError):
    """定位器无法解析或无法在文档上命中目标。"""


class ExportInterrupted(RuntimeError):
    """导出被中断：目标路径不会留下貌似完整的包，只可能有 *.tmp 残留。"""


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _short(data: bytes) -> str:
    return _digest(data)[:16]


# ---------------------------------------------------------------------------
# 字段定位：JSON 指针（RFC 6901）与 HTML 元素（CSS 子集）
# ---------------------------------------------------------------------------

def _resolve_pointer(doc, pointer: str):
    if pointer == '':
        return doc
    if not pointer.startswith('/'):
        raise LocatorError(f'非法 JSON 指针: {pointer!r}')
    node = doc
    for raw_token in pointer.split('/')[1:]:
        token = raw_token.replace('~1', '/').replace('~0', '~')
        if isinstance(node, dict):
            if token not in node:
                raise LocatorError(f'JSON 指针未命中 {pointer!r}: 缺少键 {token!r}')
            node = node[token]
        elif isinstance(node, list):
            if not token.isdigit() or int(token) >= len(node):
                raise LocatorError(f'JSON 指针未命中 {pointer!r}: 下标 {token!r} 越界')
            node = node[int(token)]
        else:
            raise LocatorError(f'JSON 指针穿过标量: {pointer!r}')
    return node


def locate_json(document: str, pointer: str):
    """按 JSON 指针定位，返回 (展示值, 原始片段)。片段为该值的规范 JSON 序列化。"""
    try:
        doc = json.loads(document)
    except json.JSONDecodeError as exc:
        raise LocatorError(f'JSON 解析失败: {exc}') from exc
    value = _resolve_pointer(doc, pointer)
    fragment = json.dumps(value, ensure_ascii=False, sort_keys=True)
    display = value if isinstance(value, str) else fragment
    return display, fragment


_VOID_TAGS = {
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
}


class _Element:
    __slots__ = ('tag', 'attrs', 'children', 'parent', 'start', 'inner_start', 'inner_end', 'end')

    def __init__(self, tag, attrs, parent, start, inner_start):
        self.tag = tag
        self.attrs = attrs
        self.children = []
        self.parent = parent
        self.start = start
        self.inner_start = inner_start
        self.inner_end = None
        self.end = None

    def text(self) -> str:
        parts = []

        def walk(node):
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    walk(child)

        walk(self)
        return ''.join(parts)


class _DOM(HTMLParser):
    """记录每个元素在原文中的字节区间，便于切出原始片段。"""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self._line_starts = [0]
        for match in re.finditer('\n', source):
            self._line_starts.append(match.end())
        self.root = _Element('#document', {}, None, 0, 0)
        self._stack = [self.root]
        self._sealed = False

    def _offset(self) -> int:
        lineno, col = self.getpos()
        return self._line_starts[lineno - 1] + col

    def handle_starttag(self, tag, attrs):
        start = self._offset()
        node = _Element(tag.lower(), dict(attrs), self._stack[-1],
                        start, start + len(self.get_starttag_text()))
        self._stack[-1].children.append(node)
        if tag.lower() in _VOID_TAGS:
            node.inner_end = node.end = node.inner_start
        else:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        start = self._offset()
        node = _Element(tag.lower(), dict(attrs), self._stack[-1],
                        start, start + len(self.get_starttag_text()))
        node.inner_end = node.end = node.inner_start
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        start = self._offset()
        gt = self.source.find('>', start)
        end = len(self.source) if gt == -1 else gt + 1
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                node = self._stack[i]
                node.inner_end = start
                node.end = end
                del self._stack[i:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)

    def handle_entityref(self, name):
        self._stack[-1].children.append(f'&{name};')

    def handle_charref(self, name):
        self._stack[-1].children.append(f'&#{name};')

    def close(self):
        if self._sealed:
            return
        super().close()
        for node in self._stack[1:]:
            if node.end is None:
                node.inner_end = node.end = len(self.source)
        self._stack = [self.root]
        self._sealed = True


_COMPOUND_RE = re.compile(r'^(?P<tag>\*|[a-zA-Z][\w-]*)?(?P<mods>(?:[.#][\w-]+|\[[^\[\]]*\])*)$')
_ATTR_RE = re.compile(r"""^\[([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\]\s"']+)))?\]$""")
_MOD_RE = re.compile(r'[.#][\w-]+|\[[^\[\]]*\]')


def _parse_compound(token: str) -> dict:
    match = _COMPOUND_RE.match(token)
    if not match:
        raise LocatorError(f'不支持的选择器片段: {token!r}')
    tag = match.group('tag')
    compound = {'tag': None if tag in (None, '*') else tag.lower(),
                'id': None, 'classes': [], 'attrs': {}}
    for mod in _MOD_RE.findall(match.group('mods')):
        if mod.startswith('.'):
            compound['classes'].append(mod[1:])
        elif mod.startswith('#'):
            compound['id'] = mod[1:]
        else:
            attr = _ATTR_RE.match(mod)
            if not attr:
                raise LocatorError(f'不支持的属性选择器: {mod!r}')
            value = attr.group(2) if attr.group(2) is not None else attr.group(3)
            if value is None:
                value = attr.group(4)
            compound['attrs'][attr.group(1)] = value
    return compound


def _split_attr_suffix(selector: str):
    """`li[data-id="42"] time@datetime` → (`li[data-id="42"] time`, 'datetime')。"""
    at = selector.rfind('@')
    if at == -1:
        return selector, None
    attr = selector[at + 1:]
    head = selector[:at]
    if (re.fullmatch(r'[A-Za-z_][\w-]*', attr) and head.strip()
            and head.count('[') == head.count(']')):
        return head, attr
    return selector, None


def _iter_elements(node):
    for child in node.children:
        if isinstance(child, _Element):
            yield child
            yield from _iter_elements(child)


def _matches(element, compound) -> bool:
    if compound['tag'] and element.tag != compound['tag']:
        return False
    if compound['id'] and element.attrs.get('id') != compound['id']:
        return False
    classes = (element.attrs.get('class') or '').split()
    if any(cls not in classes for cls in compound['classes']):
        return False
    for name, value in compound['attrs'].items():
        if name not in element.attrs:
            return False
        if value is not None and element.attrs[name] != value:
            return False
    return True


def find_elements(source: str, selector: str):
    """支持标签、.class、#id、[attr]、[attr="v"]、后代组合子及末尾 @属性。

    返回 (命中元素列表, 属性名或 None)。命中多个时调用方取第一个，
    复核端用同一定位器重放会得到同一结果。
    """
    dom = _DOM(source)
    dom.feed(source)
    dom.close()
    selector, attr = _split_attr_suffix(selector.strip())
    compounds = [_parse_compound(token) for token in selector.split()]
    if not compounds:
        raise LocatorError('空选择器')
    results = []
    for element in _iter_elements(dom.root):
        if not _matches(element, compounds[-1]):
            continue
        node = element.parent
        ok = True
        for idx in range(len(compounds) - 2, -1, -1):
            found = False
            while node is not None:
                if _matches(node, compounds[idx]):
                    found = True
                    node = node.parent
                    break
                node = node.parent
            if not found:
                ok = False
                break
        if ok:
            results.append(element)
    return results, attr


def locate_html(document: str, selector: str):
    """按 CSS 子集定位第一个命中元素，返回 (展示值, 原始片段)。

    展示值默认为元素文本（去首尾空白）；选择器以 `@属性` 结尾时取该属性值。
    原始片段始终是元素在原文中的完整切片（含标签）。
    """
    elements, attr = find_elements(document, selector)
    if not elements:
        raise LocatorError(f'选择器未命中: {selector!r}')
    element = elements[0]
    fragment = document[element.start:element.end]
    if attr is not None:
        if attr not in element.attrs:
            raise LocatorError(f'元素缺少属性 {attr!r}: {selector!r}')
        value = element.attrs[attr]
    else:
        value = element.text().strip()
    return value, fragment


# ---------------------------------------------------------------------------
# 审阅副本遮盖
# ---------------------------------------------------------------------------

def redact_html(document: str, selector: str, marker: str):
    """把命中元素的内部内容替换为遮盖标记，返回 (新文档, 遮盖记录)。"""
    elements, attr = find_elements(document, selector)
    if attr is not None:
        raise LocatorError('遮盖仅支持元素内容，不支持属性定位')
    if not elements:
        raise LocatorError(f'遮盖定位未命中: {selector!r}')
    spans = []
    for element in elements:
        if element.inner_start is None or element.inner_end is None:
            continue
        spans.append((element.inner_start, element.inner_end,
                      document[element.start:element.end]))
    out = document
    log = []
    for start, end, fragment in sorted(spans, reverse=True):
        out = out[:start] + marker + out[end:]
        log.append({'redacted_fragment_sha256': _digest(fragment.encode('utf-8')),
                    'marker': marker})
    log.reverse()
    return out, log


def redact_json(document: str, pointer: str, marker: str):
    """把 JSON 指针指向的成员替换为遮盖标记，返回 (新文档, 遮盖记录)。"""
    try:
        doc = json.loads(document)
    except json.JSONDecodeError as exc:
        raise LocatorError(f'JSON 解析失败: {exc}') from exc
    if not pointer.startswith('/') or pointer == '/':
        raise LocatorError(f'遮盖需要指向具体成员的 JSON 指针: {pointer!r}')
    parent_pointer, _, last = pointer.rpartition('/')
    parent = _resolve_pointer(doc, parent_pointer)
    token = last.replace('~1', '/').replace('~0', '~')
    if isinstance(parent, dict):
        if token not in parent:
            raise LocatorError(f'遮盖定位未命中: {pointer!r}')
        old = parent[token]
        parent[token] = marker
    elif isinstance(parent, list):
        if not token.isdigit() or int(token) >= len(parent):
            raise LocatorError(f'遮盖定位未命中: {pointer!r}')
        old = parent[int(token)]
        parent[int(token)] = marker
    else:
        raise LocatorError(f'遮盖定位穿过标量: {pointer!r}')
    fragment = json.dumps(old, ensure_ascii=False, sort_keys=True)
    out = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2)
    return out, [{'redacted_fragment_sha256': _digest(fragment.encode('utf-8')),
                  'marker': marker}]


# ---------------------------------------------------------------------------
# 留痕记录与证据存储
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Capture:
    """一次抓取的留痕：原始响应字节、响应头、抓取时刻、提取配置摘要。"""

    capture_id: str
    batch_id: str
    source: str
    format: str  # 'json' | 'html'
    fetched_at: str
    headers: dict
    body: bytes
    config_digest: str

    def meta_dict(self) -> dict:
        return {
            'capture_id': self.capture_id,
            'batch_id': self.batch_id,
            'source': self.source,
            'format': self.format,
            'fetched_at': self.fetched_at,
            'headers': self.headers,
            'config_digest': self.config_digest,
            'body_sha256': _digest(self.body),
        }


@dataclass(frozen=True)
class FieldEvidence:
    """一个结构化字段的证据：定位器、展示值与该位置的原始片段。"""

    evidence_id: str
    capture_id: str
    batch_id: str
    input_id: str
    field: str
    locator_kind: str  # 'json-pointer' | 'css'
    locator: str
    value: str
    raw_fragment: str
    config_digest: str

    def to_dict(self) -> dict:
        return {
            'evidence_id': self.evidence_id,
            'capture_id': self.capture_id,
            'batch_id': self.batch_id,
            'input_id': self.input_id,
            'field': self.field,
            'locator_kind': self.locator_kind,
            'locator': self.locator,
            'value': self.value,
            'raw_fragment': self.raw_fragment,
            'config_digest': self.config_digest,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'FieldEvidence':
        return cls(
            evidence_id=data['evidence_id'],
            capture_id=data['capture_id'],
            batch_id=data['batch_id'],
            input_id=data['input_id'],
            field=data['field'],
            locator_kind=data['locator_kind'],
            locator=data['locator'],
            value=data['value'],
            raw_fragment=data['raw_fragment'],
            config_digest=data['config_digest'],
        )


@dataclass(frozen=True)
class DerivedRecord:
    """派生结论：输入依赖闭包、状态（ok / insufficient_evidence）与展示值。"""

    derived_id: str
    rule: str
    status: str
    reasons: tuple
    value: object  # dict | None
    depends_on: tuple
    evidence_refs: tuple
    batch_pin: tuple
    config_digests: tuple

    def to_dict(self) -> dict:
        return {
            'derived_id': self.derived_id,
            'rule': self.rule,
            'status': self.status,
            'reasons': list(self.reasons),
            'value': self.value,
            'depends_on': list(self.depends_on),
            'evidence_refs': list(self.evidence_refs),
            'batch_pin': list(self.batch_pin),
            'config_digests': list(self.config_digests),
        }


def _rule_compare_subject_and_date(fields):
    """合并各输入的处罚主体与决定日期；角色缺失或取值互相矛盾时证据不足。"""
    reasons = []
    subjects = sorted({f.value for f in fields if f.field == 'subject'})
    dates = sorted({f.value for f in fields if f.field == 'decision_date'})
    if not subjects:
        reasons.append('missing-role:subject')
    if not dates:
        reasons.append('missing-role:decision_date')
    if len(subjects) > 1:
        reasons.append('conflicting-subject:' + '|'.join(subjects))
    if len(dates) > 1:
        reasons.append('conflicting-decision_date:' + '|'.join(dates))
    if reasons:
        return 'insufficient_evidence', reasons, None
    value = {'subject': subjects[0], 'decision_date': dates[0]}
    for ev in sorted(fields, key=lambda f: f.evidence_id):
        if ev.field not in value:
            value[ev.field] = ev.value  # 例如 list_title，作为上下文保留
    return 'ok', [], value


RULES = {'compare-subject-and-date': _rule_compare_subject_and_date}


def _resolve_derived(rule: str, depends_on, fields):
    """构建与复核共用的派生求值：先查输入闭包，再按规则合并。"""
    if rule not in RULES:
        raise ValueError(f'未知派生规则: {rule}')
    reasons = []
    present = {f.input_id for f in fields}
    for input_id in depends_on:
        if input_id not in present:
            reasons.append(f'missing-input:{input_id}')
    if reasons:
        return 'insufficient_evidence', reasons, None
    return RULES[rule](fields)


class EvidenceStore:
    """只增不改的证据存储；抓取批次相互隔离，相同取值不会跨批次合并。"""

    def __init__(self):
        self._captures = {}
        self._fields = {}
        self._derived = {}

    def add_capture(self, *, batch_id, source, format, body, headers, fetched_at, config):
        if format not in ('json', 'html'):
            raise ValueError(f'不支持的响应格式: {format!r}')
        body = bytes(body)
        config_digest = _short(_canonical(config).encode('utf-8'))
        capture_id = 'cap-' + _short(_canonical({
            'batch_id': batch_id,
            'source': source,
            'fetched_at': fetched_at,
            'body_sha256': _digest(body),
            'config_digest': config_digest,
        }).encode('utf-8'))
        capture = Capture(capture_id, batch_id, source, format, fetched_at,
                          dict(headers), body, config_digest)
        self._captures[capture_id] = capture
        return capture

    def extract(self, capture_id, *, input_id, field, locator):
        """按定位器从抓取原文中提取字段，保留该位置的原始片段。"""
        if capture_id not in self._captures:
            raise ValueError(f'未知抓取: {capture_id}')
        capture = self._captures[capture_id]
        text = capture.body.decode('utf-8')
        if capture.format == 'json':
            kind = 'json-pointer'
            value, fragment = locate_json(text, locator)
        else:
            kind = 'css'
            value, fragment = locate_html(text, locator)
        evidence_id = 'fld-' + _short(_canonical({
            'capture_id': capture_id,
            'field': field,
            'locator': locator,
            'value': value,
        }).encode('utf-8'))
        evidence = FieldEvidence(evidence_id, capture_id, capture.batch_id, input_id,
                                 field, kind, locator, value, fragment,
                                 capture.config_digest)
        self._fields[evidence_id] = evidence
        return evidence

    def build_derived(self, derived_id, *, rule, depends_on, batch_pin):
        """在钉选批次内求值派生结论；批次外的证据即使取值相同也不并入。"""
        pin = tuple(sorted(batch_pin))
        fields = [f for f in self._fields.values()
                  if f.input_id in depends_on and f.batch_id in pin]
        status, reasons, value = _resolve_derived(rule, tuple(depends_on), fields)
        record = DerivedRecord(
            derived_id=derived_id,
            rule=rule,
            status=status,
            reasons=tuple(reasons),
            value=value,
            depends_on=tuple(depends_on),
            evidence_refs=tuple(sorted(f.evidence_id for f in fields)),
            batch_pin=pin,
            config_digests=tuple(sorted({f.config_digest for f in fields})),
        )
        self._derived[derived_id] = record
        return record

    def get_capture(self, capture_id):
        if capture_id not in self._captures:
            raise ValueError(f'未知抓取: {capture_id}')
        return self._captures[capture_id]

    def get_field(self, evidence_id):
        if evidence_id not in self._fields:
            raise ValueError(f'未知字段证据: {evidence_id}')
        return self._fields[evidence_id]

    def get_derived(self, derived_id):
        if derived_id not in self._derived:
            raise ValueError(f'未知派生结论: {derived_id}')
        return self._derived[derived_id]


# ---------------------------------------------------------------------------
# 证据包导出与离线复核
# ---------------------------------------------------------------------------

def export_package(store, derived_id, dest, *, kind='internal', redaction_rules=(),
                   on_selected=None, _fail_after=None):
    """把一次复核所需的证据闭包导出为独立证据包。

    - 批次钉选：导出开始时对选中抓抓取快照，之后到达的新采集不会串入；
    - 原子落盘：先写 `<dest>.tmp` 过渡目录，manifest.json 最后落盘，
      然后整体改名；中断只可能留下 *.tmp 残留，不会出现貌似完整的包；
    - 不覆盖：目标路径已存在时拒绝写入，审阅导出因此无法覆盖内部完整证据；
    - 审阅副本：按 redaction_rules 遮盖原文并记录遮盖说明（redaction.json）。

    `_fail_after` 与 `on_selected` 供测试注入中断与并发采集。
    """
    if kind not in ('internal', 'review'):
        raise ValueError(f'未知证据包类型: {kind!r}')
    derived = store.get_derived(derived_id)
    evidence = [store.get_field(eid) for eid in derived.evidence_refs]
    capture_ids = sorted({e.capture_id for e in evidence})
    captures = {cid: store.get_capture(cid) for cid in capture_ids}  # 钉选快照
    if on_selected is not None:
        on_selected()

    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(f'目标已存在，拒绝覆盖: {dest}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + TMP_SUFFIX)
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / 'raw').mkdir(parents=True)
    (tmp / 'fields').mkdir()

    files = {}
    state = {'written': 0}

    def _write(rel, data):
        if _fail_after is not None and state['written'] >= _fail_after:
            raise ExportInterrupted(f'导出在写入 {rel} 前中断')
        payload = data if isinstance(data, bytes) else data.encode('utf-8')
        target = tmp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        files[rel] = _digest(payload)
        state['written'] += 1

    redactions = []
    for cid in capture_ids:
        capture = captures[cid]
        body = capture.body
        if kind == 'review':
            text = body.decode('utf-8')
            for rule in redaction_rules:
                if rule.get('format') not in (None, capture.format):
                    continue
                marker = REDACT_MARKER_TEMPLATE.format(field=rule['field'])
                if capture.format == 'html':
                    text, entries = redact_html(text, rule['locator'], marker)
                else:
                    text, entries = redact_json(text, rule['locator'], marker)
                for entry in entries:
                    entry.update({
                        'capture_id': cid,
                        'field': rule['field'],
                        'locator': rule['locator'],
                        'locator_kind': 'css' if capture.format == 'html' else 'json-pointer',
                        'reason': rule['reason'],
                    })
                redactions.extend(entries)
            body = text.encode('utf-8')
        _write(f'raw/{cid}.bin', body)
        meta = capture.meta_dict()
        if kind == 'review':
            meta['redacted'] = True
        _write(f'raw/{cid}.headers.json', _canonical(meta))
    for ev in evidence:
        _write(f'fields/{ev.evidence_id}.json', _canonical(ev.to_dict()))
    _write('derived.json', _canonical(derived.to_dict()))
    if kind == 'review':
        _write('redaction.json', _canonical({'redactions': redactions}))
    manifest = {
        'package_format': PACKAGE_FORMAT,
        'kind': kind,
        'derived_id': derived.derived_id,
        'batch_pin': list(derived.batch_pin),
        'created_at': max((captures[cid].fetched_at for cid in capture_ids), default=None),
        'files': dict(files),
    }
    # 清单最后落盘：缺少 manifest 的目录一律视为未完成导出。
    _write('manifest.json', _canonical(manifest))
    os.rename(tmp, dest)
    return dest


def reseal_package(path):
    """就地重算清单摘要（修复工具/测试用）。

    文件被替换后摘要重新一致，但字段定位重放会暴露内容与留痕的矛盾。
    """
    path = Path(path)
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    files = {}
    for rel in sorted(manifest['files']):
        member = path / rel
        if member.is_file():
            files[rel] = _digest(member.read_bytes())
    manifest['files'] = files
    (path / 'manifest.json').write_text(_canonical(manifest), encoding='utf-8')
    return manifest


@dataclass
class VerificationReport:
    package: str
    kind: object
    package_valid: bool
    proof_status: object
    recomputed_status: object
    failures: list = _dc_field(default_factory=list)
    checks: dict = _dc_field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'package': self.package,
            'kind': self.kind,
            'package_valid': self.package_valid,
            'proof_status': self.proof_status,
            'recomputed_status': self.recomputed_status,
            'failures': self.failures,
            'checks': self.checks,
        }


def verify_package(path) -> VerificationReport:
    """离线复核证据包：文件摘要 → 依赖闭包 → 字段定位重放 → 重算展示值。

    成功的证明：package_valid 为 True 且 proof_status 为 'ok'；
    证据不足的证明包体可以完好（package_valid=True），但 proof_status
    为 'insufficient_evidence'，两者不会混淆。
    """
    path = Path(path)
    checks = {name: 'not-run' for name in
              ('manifest', 'digests', 'closure', 'locators', 'derived', 'redaction')}
    failures = []

    def fail(code, detail):
        entry = {'code': code, 'detail': detail}
        if entry not in failures:
            failures.append(entry)

    manifest_path = path / 'manifest.json'
    if not path.is_dir() or not manifest_path.is_file():
        fail('incomplete-export', f'缺少 manifest.json: {path.name}')
        return VerificationReport(path.name, None, False, None, None, failures, checks)
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        checks['manifest'] = 'fail'
        fail('tampered-file', f'manifest.json 无法解析: {exc}')
        return VerificationReport(path.name, None, False, None, None, failures, checks)
    checks['manifest'] = 'pass'
    kind = manifest.get('kind')
    listed = manifest.get('files', {})

    digest_ok = True
    for rel, digest in sorted(listed.items()):
        member = path / rel
        if not member.is_file():
            fail('missing-dependency', rel)
            digest_ok = False
        elif _digest(member.read_bytes()) != digest:
            fail('tampered-file', rel)
            digest_ok = False
    checks['digests'] = 'pass' if digest_ok else 'fail'

    derived = None
    recomputed_status = None
    derived_path = path / 'derived.json'
    if not derived_path.is_file():
        fail('missing-dependency', 'derived.json')
        checks['closure'] = 'fail'
    else:
        derived = json.loads(derived_path.read_text(encoding='utf-8'))
        fields = []
        closure_ok = True
        for eid in derived.get('evidence_refs', []):
            rel = f'fields/{eid}.json'
            member = path / rel
            if rel not in listed or not member.is_file():
                fail('missing-dependency', rel)
                closure_ok = False
                continue
            fields.append(FieldEvidence.from_dict(
                json.loads(member.read_text(encoding='utf-8'))))
        for ev in fields:
            rel = f'raw/{ev.capture_id}.bin'
            if rel not in listed or not (path / rel).is_file():
                fail('missing-dependency', rel)
                closure_ok = False
        checks['closure'] = 'pass' if closure_ok else 'fail'

        locators_ok = True
        usable = []
        for ev in fields:
            raw_path = path / 'raw' / f'{ev.capture_id}.bin'
            if not raw_path.is_file():
                locators_ok = False
                continue  # 已记 missing-dependency
            text = raw_path.read_bytes().decode('utf-8')
            try:
                if ev.locator_kind == 'json-pointer':
                    value, fragment = locate_json(text, ev.locator)
                else:
                    value, fragment = locate_html(text, ev.locator)
            except LocatorError as exc:
                fail('stale-locator', f'{ev.evidence_id}: {exc}')
                locators_ok = False
                continue
            if value != ev.value or fragment != ev.raw_fragment:
                fail('stale-locator', f'{ev.evidence_id}: 重放定位与留痕不一致')
                locators_ok = False
                continue
            usable.append(ev)
        checks['locators'] = 'pass' if locators_ok else 'fail'

        try:
            status, _reasons, value = _resolve_derived(
                derived.get('rule'), tuple(derived.get('depends_on', ())), usable)
        except ValueError as exc:
            fail('unknown-rule', str(exc))
            status, value = None, None
        recomputed_status = status
        if status != derived.get('status') or value != derived.get('value'):
            fail('derived-mismatch', '按留痕重算的展示值与记录结论不一致')
            checks['derived'] = 'fail'
        else:
            checks['derived'] = 'pass'

    if kind == 'review':
        redaction_ok = True
        redaction_path = path / 'redaction.json'
        if not redaction_path.is_file():
            fail('missing-dependency', 'redaction.json')
            redaction_ok = False
        else:
            entries = json.loads(redaction_path.read_text(encoding='utf-8')).get('redactions', [])
            for entry in entries:
                raw_path = path / 'raw' / f"{entry.get('capture_id')}.bin"
                located = None
                if raw_path.is_file():
                    text = raw_path.read_bytes().decode('utf-8')
                    try:
                        if entry.get('locator_kind') == 'json-pointer':
                            located, _ = locate_json(text, entry.get('locator', ''))
                        else:
                            located, _ = locate_html(text, entry.get('locator', ''))
                    except LocatorError:
                        located = None
                if located != entry.get('marker'):
                    fail('redaction-mismatch',
                         f"{entry.get('capture_id')}:{entry.get('locator')}")
                    redaction_ok = False
        checks['redaction'] = 'pass' if redaction_ok else 'fail'
    else:
        checks['redaction'] = 'not-applicable'

    return VerificationReport(
        package=path.name,
        kind=kind,
        package_valid=not failures,
        proof_status=derived.get('status') if derived else None,
        recomputed_status=recomputed_status,
        failures=failures,
        checks=checks,
    )
