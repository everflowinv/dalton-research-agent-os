"""Batch D: the shared research-product contract (STRL-shaped), one place.

The 2026-10-09 diagnosis found the products bound by three layers: number-free
Claims as the only material, a contract that banned views, watch prices and a
trade angle, and free-text 「材料缺少」 that nobody consumed.  The owner's
decisions of the same day reverse the second (views, watch prices and reasons
are allowed and recorded) and require the third to be a MaterialRequest.

This module is what every product drafter in batch D shares:

* the **material pack** interface (sibling batch C builds
  ``material_pack.build``, ``material_requests.open_request`` and
  ``redraft_policy``); imported by name, with :class:`MaterialPackUnavailable`
  naming exactly what is missing when the sibling module is not installed;
* the **tag vocabulary** of a pack (``P`` passage, ``N`` number, ``C`` claim,
  ``O`` our own view/model) resolved to citation entries the deliverable
  authority can check;
* the **number rule**: three admissible sources -- verbatim from a cited
  ``[P…]``/``[N…]`` tag, declared arithmetic over cited numbers, or
  「我们的估计」 bound to a model assumption ref;
* the **gap rule**: a gap is ``company_not_disclosed`` or ``not_acquired``;
  the latter, and every 「材料缺少」 in prose, must reference a MaterialRequest
  and say what was tried;
* the **placeholder rule**: no indirect placeholder (「〔数字见研判记录〕」) in
  product text.

Nothing here writes or calls a model.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping, Sequence
from types import ModuleType
from typing import Any

from .store import content_hash

PRODUCT_CONTRACT_VERSION = "product-contract:strl:1"

# ---------------------------------------------------------------------------
# the material pack (sibling batch C)
# ---------------------------------------------------------------------------


class MaterialPackUnavailable(RuntimeError):
    """A batch-C module this contract depends on is not installed."""


_SIBLING_MODULES = {
    "material_pack": "build(state_dir, company_ref, product, as_of=None, *, budget_chars=None)",
    "material_requests": "open_request(...)",
    "redraft_policy": "should_redraft(...) / evaluate_targets(...)",
}


def sibling_module(name: str) -> ModuleType:
    """Import ``dalton_core.<name>`` or say exactly which batch-C piece is absent."""

    if name not in _SIBLING_MODULES:
        raise ValueError(f"not a batch-C module: {name}")
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name not in {f"{__package__}.{name}", name}:
            raise
        raise MaterialPackUnavailable(
            f"dalton_core.{name} is not installed: batch C (共享材料包) provides "
            f"{name}.{_SIBLING_MODULES[name]}; the batch-D product contract drafts "
            "only from a material pack and cannot fall back to the Claim-only context"
        ) from None


def _field(pack: Any, name: str, default: Any = None) -> Any:
    if isinstance(pack, Mapping):
        return pack.get(name, default)
    return getattr(pack, name, default)


PACK_FIELDS = ("text", "tags", "json", "sufficiency", "budget", "recipe_version",
               "content_hash", "inputs")


def normalise_pack(pack: Any) -> dict[str, Any]:
    """A MaterialPack (batch C's dataclass, or a mapping of the same fields) as one dict.

    ``record`` is ``MaterialPack.record()`` -- the provenance block a product
    version stores under ``material_pack`` and ``redraft_policy`` reads.
    """

    if pack is None:
        raise MaterialPackUnavailable("no material pack was supplied")
    out = {name: _field(pack, name) for name in PACK_FIELDS}
    record = getattr(pack, "record", None)
    if callable(record):
        out["record"] = record()
    else:
        out["record"] = _field(pack, "record") or None
    for name in ("company_ref", "product", "as_of", "watermark"):
        out[name] = _field(pack, name)
    out["text"] = str(out["text"] or "")
    out["tags"] = dict(out["tags"] or {})
    out["json"] = dict(out["json"] or {})
    sufficiency = out["sufficiency"]
    if not isinstance(sufficiency, Mapping):
        sufficiency = {"status": _field(sufficiency, "status"),
                       "missing": _field(sufficiency, "missing", []),
                       "wait_until": _field(sufficiency, "wait_until")}
    out["sufficiency"] = {
        "status": str(sufficiency.get("status") or "unknown"),
        "missing": list(sufficiency.get("missing") or []),
        "wait_until": sufficiency.get("wait_until"),
    }
    if not out["content_hash"]:
        out["content_hash"] = content_hash({"text": out["text"], "tags": _plain(out["tags"])})
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def build_pack(state_dir: Any, company_ref: str, product: str, as_of: str | None = None,
               *, budget_chars: int | None = None) -> dict[str, Any]:
    """``material_pack.build`` through the sibling module, normalised.

    ``product`` is batch C's recipe name: ``initial_screen``, ``dossier``,
    ``debate_map``, ``investment_memo``, ``event_judgement``,
    ``earnings_preview``, ``industry_framework``, ``ask``.
    """

    module = sibling_module("material_pack")
    pack = module.build(state_dir, company_ref, product, as_of, budget_chars=budget_chars)
    return normalise_pack(pack)


def check_sufficiency(state_dir: Any, company_ref: str, product: str, as_of: str | None = None,
                      *, requester: Mapping[str, Any] | None = None, record: bool = True) -> dict[str, Any]:
    """``material_pack.sufficiency`` (batch C): ``drafting_allowed``, ``reason``,
    ``wait_until``, ``missing`` and the ``gap_request_refs`` it opened."""

    module = sibling_module("material_pack")
    return dict(module.sufficiency(state_dir, company_ref, product, as_of,
                                   requester=dict(requester or {"lane": product}), record=record))


def record_pack_build(state_dir: Any, pack: Mapping[str, Any], *, run_ref: str | None = None) -> Any:
    """Append the pack the lane drafted from to batch C's build ledger."""

    module = sibling_module("material_requests")
    record = pack.get("record")
    if not record:
        return None
    return module.record_pack_build(state_dir, record, run_ref=run_ref)


#: Statuses of ``sufficiency.status`` that allow a draft.
SUFFICIENT_STATUSES = frozenset({"sufficient", "ok", "ready", "complete"})


def pack_is_sufficient(pack: Mapping[str, Any]) -> bool:
    return str((pack.get("sufficiency") or {}).get("status") or "") in SUFFICIENT_STATUSES


def open_material_request(state_dir: Any, **fields: Any) -> str:
    """Open (or join) one MaterialRequest through batch C; return its ref.

    ``fields`` are ``open_request``'s own: ``requester`` ``{lane, product_ref,
    run_ref, section}``, ``subject``, ``need`` (``{"kind": "document",
    "doc_kinds": [...]}`` | ``{"kind": "data", "data_item": ...}`` |
    ``{"kind": "answer", "question": ...}``), ``period``, ``source_priority``,
    ``acceptance``, ``budget``, ``deadline``.  A ``None`` field is left to
    batch C's default.  The returned view is reduced to its ``id``.
    """

    module = sibling_module("material_requests")
    result = module.open_request(state_dir, **{key: value for key, value in fields.items()
                                               if value is not None})
    ref = request_ref_of(result)
    if not ref:
        raise MaterialPackUnavailable("material_requests.open_request returned no request ref")
    return ref


def request_ref_of(result: Any) -> str | None:
    if isinstance(result, str):
        return result or None
    for key in ("ref", "request_ref", "id", "request_id"):
        value = _field(result, key)
        if value:
            return str(value)
    return None


def should_redraft(**kwargs: Any) -> dict[str, Any]:
    """``redraft_policy.should_redraft`` normalised to ``{"redraft", "reasons"}``.

    Batch C owns the thresholds (owner decision 4: batched redrafts -- new
    required material version, consensus move over threshold, N new notes, or
    X days with new material).  ``True``/``False`` and mappings are accepted.
    """

    module = sibling_module("redraft_policy")
    result = module.should_redraft(**kwargs)
    if isinstance(result, bool):
        return {"redraft": result, "reasons": []}
    redraft = _field(result, "redraft", _field(result, "should_redraft", False))
    reasons = _field(result, "reasons", []) or []
    return {"redraft": bool(redraft), "reasons": [str(item) for item in reasons]}


# ---------------------------------------------------------------------------
# tags: P passage, N number, C claim, O ours
# ---------------------------------------------------------------------------

TAG_KINDS: Mapping[str, str] = {"P": "passage", "N": "number", "C": "claim", "O": "ours"}
PASSAGE_QUOTE_PRINT_CONTRACT = "passage-quote-print:hyphen-line-wrap:1"
NUMBER_PROVENANCE_CONTRACT = "pack-number-provenance:exact-valuation-parent:1"


def passage_quote_matches(quote: str, passage: str) -> bool:
    """Compare words in one original span; never rewrite its bytes or identity.

    A single layout newline inside a hyphenated token may be omitted in a
    quotation. Keep the literal hyphen, both neighbouring characters, and
    every digit/punctuation. Blank lines and a leading minus are not wraps.
    """
    def normal(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()
    wrapped = re.sub(r"(?<=\w)-\r?\n[ \t]*(?=\w)", "-", passage)
    return bool(quote.strip()) and (
        normal(quote) in normal(passage) or normal(quote) in normal(wrapped))
TAG_RE = re.compile(r"(?<![A-Za-z0-9])([PNCO])(\d{1,4})(?![A-Za-z0-9])")
BRACKET_TAG_RE = re.compile(r"\[([PNCO]\d{1,4})\]")


def tag_kind(tag: str) -> str | None:
    match = re.fullmatch(r"([PNCO])\d{1,4}", str(tag or "").strip())
    return TAG_KINDS[match.group(1)] if match else None


def tag_entry(pack: Mapping[str, Any], tag: str) -> dict[str, Any] | None:
    """What one tag points at, as a flat dict; ``None`` for an unknown tag.

    A tag's value is either a ref string or a mapping carrying ``ref`` and
    optionally ``text``, ``metric``, ``period``, ``unit``, ``source_kind``
    (``consensus``, ``sell_side_note``, ``transcript``, ``filing``, ``release``
    ...), ``source`` (the authority's display name) and ``cell`` (a computed
    cell citation).
    """

    tag = str(tag or "").strip()
    kind = tag_kind(tag)
    tags = pack.get("tags") or {}
    if kind is None or tag not in tags:
        return None
    value = tags[tag]
    entry: dict[str, Any] = {"tag": tag, "kind": kind}
    if isinstance(value, Mapping):
        entry.update({str(key): item for key, item in value.items()})
        entry["ref"] = str(value.get("ref") or value.get("passage_ref")
                           or value.get("claim_ref") or value.get("number_ref")
                           or value.get("our_ref") or "")
    else:
        # Batch C's shape: tag -> ref string; the text lives in ``pack.text``
        # (P/C/O blocks) or in ``pack.json["number_tags"]`` (N).
        entry["ref"] = str(value)
        if kind == "number":
            entry.update(_number_entry(pack, tag, entry["ref"]))
        else:
            entry.update(_block_entry(pack, tag))
            # The pack's passage metadata (issuer, document kind, house,
            # date) -- what the issuer-attribution check reads.
            meta = ((pack.get("json") or {}).get("passage_tags") or {}).get(tag) or {}
            for key in ("company_ref", "company_refs", "doc_kind", "house", "document_ref", "period"):
                if meta.get(key) and not entry.get(key):
                    entry[key] = list(meta[key]) if key == "company_refs" and isinstance(meta[key], list) else meta[key]
            if meta.get("date") and not entry.get("as_of"):
                entry["as_of"] = meta["date"]
            if meta.get("doc_kind") and not entry.get("source_kind") and DOC_KIND_SOURCE.get(meta["doc_kind"]):
                entry["source_kind"] = DOC_KIND_SOURCE[meta["doc_kind"]]
            if kind == "ours":
                entry.setdefault("source_kind", "ours")
            elif kind == "claim":
                entry.setdefault("source_kind", "claim")
    if not entry["ref"]:
        return None
    if isinstance(entry.get("company_refs"), list):
        entry["company_refs"] = list(entry["company_refs"])
    return entry


#: ``evidence_corpus.KINDS`` -> the source kinds a product contract counts.
DOC_KIND_SOURCE: Mapping[str, str] = {
    "transcript": "transcript", "sec_exhibit": "release", "sec_8k": "release",
    "sec_10k": "filing", "sec_10q": "filing", "sec_other": "filing",
    "broker_report": "sell_side_note", "sales_note": "sales_note", "expert_call": "expert_call",
    "company_wiki": "industry", "web_page": "web", "research_other": "industry",
    "prior_research": "ours",
}
_LABEL_SOURCE: dict[str, str] = {}


def _label_sources() -> Mapping[str, str]:
    if not _LABEL_SOURCE:
        try:
            from .evidence_corpus import KIND_LABELS
        except Exception:  # noqa: BLE001 - a label table is a nicety, not a dependency
            return {}
        _LABEL_SOURCE.update({label: DOC_KIND_SOURCE.get(kind, kind) for kind, label in KIND_LABELS.items()})
    return _LABEL_SOURCE


# Only a known renderer delimiter followed by its next passage block.
# This separates prompt structure; it does not prove any source metadata.
_RENDER_TRANSCRIPT_BOUNDARY = r"《[^\n》]{1,500}》(?:19|20)\d{2}-\d{2}-\d{2}：管理层发言[ \t]*\r?$(?=\n(?:[ \t]*\r?\n)*\[P\d{1,4}\])"
_BLOCK_HEAD_RE = re.compile(r"(?m)^\[(?P<tag>[PCO]\d{1,4})\](?P<head>[^\n]*)\n?")
_BROKER_RENDER_HEADER_RE = re.compile(
    r"(?m)^券商：(?P<house>[^\n]{1,256}?)（(?:偏多|偏空|中性|立场未识别)）· "
    r"(?P<date>(?:19|20)\d{2}-\d{2}-\d{2})(?:· [^\n]{0,2048})?\r?\n"
    r"《(?P<title>[^\n》]{1,500})》[ \t]*(?:\r?\n[ \t]*)*\Z")


def _without_next_broker_header(pack: Mapping[str, Any], tag: str, body: str,
                                next_block: re.Match[str] | None) -> str:
    """Separate an identified next report's renderer header, never source text.

    The current exact span bounds protect an original header-like suffix. The
    original/hash verifier still has to prove every byte of the remaining row.
    This parser cannot certify report metadata or repair a corrupt old pack.
    """
    if not tag.startswith("P") or next_block is None:
        return body
    next_tag = next_block.group("tag")
    tags = pack.get("tags") or {}
    meta = ((pack.get("json") or {}).get("passage_tags") or {}).get(next_tag) or {}
    header = _BROKER_RENDER_HEADER_RE.search(body)
    if not next_tag.startswith("P") or next_tag not in tags or not header or meta.get("doc_kind") != "broker_report":
        return body
    parts = [part.strip() for part in next_block.group("head").split("·") if part.strip()]
    if (header.group("house") != meta.get("house") or header.group("date") != meta.get("date")
            or header.group("house") not in parts or header.group("date") not in parts
            or f"《{header.group('title')}》" not in next_block.group("head")):
        return body
    from .evidence_index import parse_passage_ref, PassageRefInvalid
    try:
        current = parse_passage_ref(tags[tag])
        following = parse_passage_ref(tags[next_tag])
    except PassageRefInvalid:
        return body
    if following["document_ref"] != meta.get("document_ref"):
        return body
    candidate = body[:header.start()]
    span_length = current["end"] - current["start"]
    if candidate.strip() and len(candidate.strip()) <= span_length < len(body.strip()):
        return candidate
    return body


def _block_entry(pack: Mapping[str, Any], tag: str) -> dict[str, Any]:
    """The text, source kind and date of a ``[P…]``/``[C…]``/``[O…]`` block in ``pack.text``.

    A passage block is ``[P12] · 电话会纪要 · 发言：… · 2026-07-30 · 《title》``
    and then the passage text, up to the next block or section heading.
    """

    text = str(pack.get("text") or "")
    for match in _BLOCK_HEAD_RE.finditer(text):
        if match.group("tag") != tag:
            continue
        tail = text[match.end():]
        stop = re.search(r"(?m)^(?:\[[PNCO]\d{1,4}\]|## |" + _RENDER_TRANSCRIPT_BOUNDARY + r")", tail)
        body = tail[:stop.start()] if stop else tail
        next_block = _BLOCK_HEAD_RE.match(tail, stop.start()) if stop else None
        body = _without_next_broker_header(pack, tag, body, next_block)
        head = match.group("head")
        out: dict[str, Any] = {}
        parts = [part.strip() for part in head.split("·") if part.strip()]
        if tag.startswith("P"):
            out["text"] = body.strip()
            for part in parts:
                kind = _label_sources().get(part)
                if kind:
                    out["source_kind"] = kind
                if re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", part):
                    out["as_of"] = part
                if part.startswith("《") and part.endswith("》"):
                    out["title"] = part[1:-1]
        else:
            out["text"] = (head.strip(" ·:：") + ("\n" + body.strip() if body.strip() else "")).strip()
        return out
    return {}


def _number_entry(pack: Mapping[str, Any], tag: str, ref: str) -> dict[str, Any]:
    """An ``[N…]`` tag from ``pack.json["number_tags"]``: value, display, source kind, metric."""

    info = dict(((pack.get("json") or {}).get("number_tags") or {}).get(tag) or {})
    pointer = str(info.get("pointer") or (ref[len("pack-json:"):] if ref.startswith("pack-json:") else ""))
    parts = [part for part in pointer.split("/") if part]
    display = str(info.get("display") if info.get("display") is not None else info.get("value", ""))
    value = info.get("value")
    # The pack prints a percentage as ``[N15] 6.5%``: the sign is outside the
    # display, and a body that writes 6.5% is quoting the pack verbatim.
    shown = display
    after = re.search(rf"\[{re.escape(tag)}\]\s*{re.escape(display)}\s*(%|pp)",
                      str(pack.get("text") or ""))
    if after or any(re.search(r"(?:^|_)(?:percent|pct)$", part) for part in parts):
        shown = f"{display}{after.group(1) if after else '%'}"
    text = shown if value is None or str(value) == display else f"{shown}（{value}）"
    if parts[:1] == ["our_model"] and "theirs" in parts:
        # ``/our_model/ours_vs_consensus/theirs`` is the consensus figure the
        # pack compared us with, not ours.
        source_kind = "consensus" if any("consensus" in part for part in parts) else "sell_side_note"
    elif parts[:1] == ["our_model"] and parts[-1:] == ["diff_pct"]:
        source_kind = "comparison"
    elif parts[:1] == ["our_model"]:
        source_kind = "ours"
    elif parts[:1] == ["numbers"] and len(parts) > 1:
        source_kind = {"consensus": "consensus", "street": "sell_side_note", "market": "market",
                       "valuation": "valuation", "outlook": "guidance", "peers": "peers",
                       "fcf_bridge": "valuation"}.get(parts[1], parts[1])
    else:
        source_kind = parts[0] if parts else "number"
    # A valuation pointer is a derived vendor consensus multiple only when
    # its exact held parent and leaf bind that same value. Display wording,
    # labels or another vendor row cannot confer this capability.
    if (len(parts) == 5 and parts[:3] == ["numbers", "valuation", "forward_pe"]
            and parts[3].isdigit() and parts[4] in {"value", "forward_pe"}
            and ref == "pack-json:" + pointer):
        rows = ((pack.get("json") or {}).get("numbers") or {}).get("valuation", {}).get("forward_pe", [])
        index = int(parts[3])
        if isinstance(rows, list) and index < len(rows):
            parent = rows[index]
            if (isinstance(parent, Mapping) and parent.get("source") == "consensus_vendor"
                    and parent.get("status") == "available" and value is not None
                    and parent.get(parts[4]) == value and display == str(value)
                    and not info.get("transform")):
                source_kind = "consensus"
    out: dict[str, Any] = {"text": text, "value": value, "display": display, "pointer": pointer,
                           "source_kind": source_kind,
                           "metric": ".".join(part for part in parts[1:] if not part.isdigit())[:120]}
    if info.get("transform"):
        out["transform"] = info["transform"]
    period = _number_period(pack, parts)
    if period:
        out["period"] = period
    unit = {"bn": "十亿", "m美元": "百万美元"}.get(re.sub(r"[\d.,\s-]", "", display), "")
    if unit:
        out["unit"] = unit
    return out


def _number_period(pack: Mapping[str, Any], parts: Sequence[str]) -> str | None:
    """The label of the period a pointer indexes into, when the pack names one."""

    node: Any = pack.get("json") or {}
    label = None
    for part in parts:
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            break
        if isinstance(node, Mapping):
            label = node.get("label") or node.get("period") or label
    return str(label) if label else None


def tags_in(text: str) -> list[str]:
    return [match.group(1) + match.group(2) for match in TAG_RE.finditer(text or "")]


# ---------------------------------------------------------------------------
# the rules every product prompt carries
# ---------------------------------------------------------------------------

STANCES: tuple[str, ...] = ("看多", "看空", "中性", "观察")
WATCH_PRICE_UNAVAILABLE = "观察价：暂无法给出（{reason}）"
OUR_ESTIMATE_LABEL = "我们的估计"
#: Words that label a figure as ours in prose.
OUR_ESTIMATE_RE = re.compile(r"我们的估计|我们估计|我们预计|我们预测|我们测算|我们估算|我们假设|我们的预测|我们的假设")

NUMBER_SOURCE_RULES: tuple[str, ...] = (
    "数字只有三种合法来源，每个数字都要在 numbers 数组里标明是哪一种：",
    "(1) 逐字引用：数字逐字出现在所引 [P…] 段落或 [N…] 数字里（段落写 {\"tag\": \"P12\", \"quote\": \"原文中含该数字的片段\"}，数字写 {\"tag\": \"N3\"}）；",
    "(2) 声明算式：由已引用数字做四则运算得到，写出算式与结果（{\"derivation\": {\"expression\": \"14.39*20\", \"result\": \"287.8\"}, \"text\": \"…287.8…\"}），确定性复核；",
    "(3) 我们的估计：正文同一句里写明「我们的估计/我们预计」，并绑定模型假设（{\"estimate\": \"O2 或 assumption:<ref>\", \"text\": \"…\"}）。",
    "单位换算（百万→亿）也是算式，必须声明；不得四舍五入后冒充逐字引用。",
    "每个数字都写清指标、期间、单位与来源（例如「FY26 第四财季收入 187.2 亿美元（发布稿）」），按量级取整显示。",
)
VIEW_RULES: tuple[str, ...] = (
    f"必须表态：立场只能是 {'/'.join(STANCES)} 之一；必须给出观察价，"
    "或写「观察价：暂无法给出（原因）」并列出一致预期与卖方目标价区间作为参照。",
    "观察价 = 倍数 × EPS，写出算式；给出现价与隐含空间；倍数理由须覆盖历史分位、同业与业务属性。",
    "必须给出 FY+1、FY+2 收入与 EPS 的我们 vs consensus，差异 %，以及差异来自哪条假设。",
    "观点、观察价与交易角度都是允许且必须的；写成我们的判断，并标明证伪条件。",
)
GAP_RULES: tuple[str, ...] = (
    "缺口分两类：company_not_disclosed（公司不披露，写明查过哪份披露）与 not_acquired（系统没取到）。",
    "not_acquired 必须引用一条 MaterialRequest（material-request:… 或 MR-…）并写「已尝试：…」。",
    "正文里出现「材料缺少」必须绑定一条 MaterialRequest，否则整篇被拒；只保留仍影响结论的问题。",
    "禁止间接占位符：不写「〔数字见研判记录〕」「见附表」「待补」「XX」之类，数字要么写出来，要么按缺口规则处理。",
)

# ---------------------------------------------------------------------------
# gaps and 「材料缺少」
# ---------------------------------------------------------------------------

GAP_KINDS: tuple[str, ...] = ("company_not_disclosed", "not_acquired")
MATERIAL_REQUEST_REF_RE = re.compile(
    r"(?:material-request:[A-Za-z0-9._:/-]+|MR-[A-Za-z0-9][A-Za-z0-9._-]*)")
MATERIAL_MISSING_RE = re.compile(
    r"现有材料缺少|材料缺少|缺少材料|材料不足|资料缺少|缺乏材料|材料尚缺|材料中没有|缺来源")
TRIED_RE = re.compile(r"已尝试[:：]")


class ProductContractError(ValueError):
    """A product section breaks the shared contract."""


def validate_gap_item(item: Any, name: str = "gap_item") -> dict[str, Any]:
    """``{kind, need, request_ref?, tried?, period?}``, closed."""

    if not isinstance(item, Mapping):
        raise ProductContractError(f"{name} must be an object")
    allowed = {"kind", "need", "request_ref", "tried", "period"}
    if set(item) - allowed or not {"kind", "need"} <= set(item):
        raise ProductContractError(f"{name} must be {{kind, need[, request_ref, tried, period]}}")
    kind = str(item["kind"])
    if kind not in GAP_KINDS:
        raise ProductContractError(f"{name}.kind must be one of {list(GAP_KINDS)}")
    need = str(item["need"] or "").strip()
    if not need:
        raise ProductContractError(f"{name}.need must say what is missing")
    out: dict[str, Any] = {"kind": kind, "need": need[:300]}
    request_ref = str(item.get("request_ref") or "").strip()
    tried = str(item.get("tried") or "").strip()
    if kind == "not_acquired":
        if not MATERIAL_REQUEST_REF_RE.fullmatch(request_ref):
            raise ProductContractError(
                f"{name} is not_acquired and must reference a MaterialRequest "
                "(material-request:… or MR-…)")
        if not tried:
            raise ProductContractError(f"{name} is not_acquired and must say 已尝试：…")
    if request_ref:
        out["request_ref"] = request_ref[:200]
    if tried:
        out["tried"] = tried[:300]
    if item.get("period") is not None:
        out["period"] = str(item["period"])[:120]
    return out


_SENTENCE_RE = re.compile(r"[^。！？；!?\n]+[。！？；!?]?")


def sentences(text: str) -> list[str]:
    return [match.group(0).strip() for match in _SENTENCE_RE.finditer(text or "")
            if match.group(0).strip()]


def material_missing_findings(body: str, gap_items: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """Every 「材料缺少」 not bound to a MaterialRequest.

    A sentence is bound when it names a request ref itself, or when the
    section carries a ``not_acquired`` gap item with a request ref (one item
    binds one sentence).
    """

    budget = sum(1 for item in gap_items
                 if item.get("kind") == "not_acquired" and item.get("request_ref"))
    findings = []
    for sentence in sentences(body):
        if not MATERIAL_MISSING_RE.search(sentence):
            continue
        if MATERIAL_REQUEST_REF_RE.search(sentence):
            continue
        if budget > 0:
            budget -= 1
            continue
        findings.append({"code": "material_missing_without_request", "excerpt": sentence[:120]})
    return findings


def material_missing_count(text: str) -> int:
    return len(MATERIAL_MISSING_RE.findall(text or ""))


# ---------------------------------------------------------------------------
# indirect placeholders
# ---------------------------------------------------------------------------

#: Registry of indirect placeholders a product must never print.  Each entry
#: is (code, pattern); add a row when a new one is found in published text.
PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bracket_reference", re.compile(r"〔[^〔〕]{0,30}(?:见|详见|参见|ref|数字)[^〔〕]{0,30}〕")),
    ("see_elsewhere", re.compile(r"(?:数字|数据|详情|金额)(?:见|详见|参见)(?:研判记录|附表|附件|模型|下表|系统)")),
    ("template_slot", re.compile(r"\{\{[^{}]{0,40}\}\}|<\s*(?:placeholder|待填|数字)\s*>|\$\{[^{}]{0,40}\}")),
    ("todo_marker", re.compile(r"(?<![A-Za-z])(?:TBD|TODO|FIXME|N/A待补)(?![A-Za-z])|【待补[^】]{0,20}】|（待补充）|\(待补充\)")),
    ("x_figure", re.compile(r"(?<![A-Za-z0-9])(?:XX|xx|X{3,})(?:\.X+)?\s*(?:%|亿|万|美元|元|倍|x\b)")),
)


def placeholder_findings(text: str) -> list[dict[str, Any]]:
    """One finding per placeholder; overlapping registry matches count once."""

    findings: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    for code, pattern in PLACEHOLDER_PATTERNS:
        for match in pattern.finditer(text or ""):
            if any(start < match.end() and match.start() < end for start, end in spans):
                continue
            spans.append(match.span())
            findings.append({"code": f"indirect_placeholder:{code}", "matched": match.group(0),
                             "at": match.start()})
    return sorted(findings, key=lambda item: item["at"])


def contract_fingerprint() -> str:
    """Hash of the rules a product drafted under, so a changed rule is a changed hash."""

    from .material_request_citation import CONTRACT as request_citation_contract
    return content_hash({
        "version": PRODUCT_CONTRACT_VERSION,
        "number_rules": NUMBER_SOURCE_RULES, "view_rules": VIEW_RULES, "gap_rules": GAP_RULES,
        "stances": STANCES, "gap_kinds": GAP_KINDS,
        "material_missing": MATERIAL_MISSING_RE.pattern,
        "placeholders": [(code, pattern.pattern) for code, pattern in PLACEHOLDER_PATTERNS],
        "passage_quote_print": PASSAGE_QUOTE_PRINT_CONTRACT,
        "number_provenance": NUMBER_PROVENANCE_CONTRACT,
        "material_request_prose_citation": request_citation_contract,
    })


__all__ = [
    "BRACKET_TAG_RE", "GAP_KINDS", "GAP_RULES", "MATERIAL_MISSING_RE",
    "MATERIAL_REQUEST_REF_RE", "MaterialPackUnavailable", "NUMBER_SOURCE_RULES",
    "OUR_ESTIMATE_LABEL", "OUR_ESTIMATE_RE", "PACK_FIELDS", "PLACEHOLDER_PATTERNS",
    "PRODUCT_CONTRACT_VERSION", "ProductContractError", "STANCES", "SUFFICIENT_STATUSES",
    "TAG_KINDS", "TAG_RE", "TRIED_RE", "VIEW_RULES", "WATCH_PRICE_UNAVAILABLE",
    "DOC_KIND_SOURCE", "build_pack", "check_sufficiency", "contract_fingerprint",
    "material_missing_count", "record_pack_build",
    "material_missing_findings", "normalise_pack", "open_material_request",
    "pack_is_sufficient", "placeholder_findings", "request_ref_of", "sentences",
    "should_redraft", "sibling_module", "tag_entry", "tag_kind", "tags_in",
    "validate_gap_item",
]
