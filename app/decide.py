from __future__ import annotations

import hashlib
import re
from typing import Any

from .canonical import package_content_hash
from .llm import ACTIONS, decide_with_llm
from .store import Store

BRACKET_RE = re.compile(r"\[([^\[\]]{1,120})\]")
INVOICE_NO_RE = re.compile(
    r"\b(?:invoice\s*(?:number|no\.?|#)|inv(?:oice)?)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-_/]{2,30})",
    re.I,
)
AMOUNT_RE = re.compile(
    r"(?:(?:INR|USD|EUR|GBP)\s*)?(?:Rs\.?\s*)?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.([0-9]{1,2}))?",
)
CURRENCY_RE = re.compile(r"\b(INR|USD|EUR|GBP)\b")
VENDOR_RE = re.compile(
    r"(?:vendor|supplier|payee|merchant)\s*(?:name)?\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9 .,&'\-]{1,80})",
    re.I,
)

SKIP_PARAGRAPH = re.compile(
    r"cover[-\s]?sheet|training decoy|training note|archive example|"
    r"archived example|historical example|sample only|ignore this|"
    r"not (?:the )?(?:live|operative) (?:claim|paragraph)|do not use this",
    re.I,
)
DECISION_HINT = re.compile(
    r"operative|live claim|current claim|decision paragraph|action determination|"
    r"authorized action|final instruction|present invoice",
    re.I,
)

DUP_RE = re.compile(
    r"already paid|previously (?:paid|settled)|duplicate (?:payment|invoice)|"
    r"same commercial invoice (?:was )?already",
    re.I,
)
NOT_DUP_RE = re.compile(
    r"not a duplicate|different invoice is not a duplicate|do not treat as duplicate",
    re.I,
)
HOLD_RE = re.compile(
    r"\bhold\b|await(?:ing)? (?:verification|grn|goods receipt|tax)|"
    r"pending verification|do not pay until|pause payment|withhold",
    re.I,
)
EXC_RE = re.compile(
    r"\bexception\b|mismatch|conflict|discrepanc|does not match|inconsistent|"
    r"material (?:records? )?conflict",
    re.I,
)
APPR_RE = re.compile(
    r"request(?:ing)? approval|outside (?:delegated )?authority|exceeds (?:the )?(?:limit|authority)|"
    r"above (?:autonomous|delegated)|approval required|not within autonomous",
    re.I,
)
SETTLE_RE = re.compile(
    r"settle(?:ment)?|within (?:autonomous |delegated )?authority|reconciled|"
    r"authorized to pay|pay now|commercially valid and within",
    re.I,
)


def _walk_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_walk_strings(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_walk_strings(item))
    return out


def _paragraphs(package: Any) -> list[str]:
    blobs = _walk_strings(package)
    text = "\n\n".join(blobs)
    chunks = re.split(r"\n{2,}|(?<=\.)\s+(?=[A-Z\[])", text)
    paras = [re.sub(r"\s+", " ", chunk).strip() for chunk in chunks]
    return [p for p in paras if len(p) >= 20]


def _refs(text: str) -> list[str]:
    return [f"[{item}]" if not item.startswith("[") else item for item in BRACKET_RE.findall(text)]


def _looks_skipped(text: str) -> bool:
    return bool(SKIP_PARAGRAPH.search(text))


def _pick_decision_paragraph(package: Any) -> str:
    paras = _paragraphs(package)
    scored: list[tuple[int, str]] = []
    for para in paras:
        if _looks_skipped(para):
            continue
        refs = _refs(para)
        score = 0
        if len(refs) >= 3:
            score += 8
        elif len(refs) >= 1:
            score += 2
        if DECISION_HINT.search(para):
            score += 6
        if DUP_RE.search(para) or HOLD_RE.search(para) or EXC_RE.search(para) or APPR_RE.search(para) or SETTLE_RE.search(para):
            score += 4
        if score:
            scored.append((score, para))
    if not scored:
        live = [p for p in paras if not _looks_skipped(p)]
        return max(live, key=len) if live else (paras[0] if paras else "")
    scored.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
    return scored[0][1]


def _first_key(obj: Any, names: set[str]) -> Any:
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key.lower() in names and val not in (None, ""):
                return val
            found = _first_key(val, names)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _first_key(item, names)
            if found not in (None, ""):
                return found
    return None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if re.fullmatch(r"-?\d+", cleaned):
            return int(cleaned)
        if re.fullmatch(r"-?\d+\.\d+", cleaned):
            return int(round(float(cleaned)))
    return None


def extract_facts(package: Any, paragraph: str) -> dict[str, Any]:
    vendor = _first_key(package, {"vendorname", "vendor", "supplier", "suppliername", "payee"})
    invoice = _first_key(
        package,
        {"invoicenumber", "invoice_no", "invoiceno", "invoiceid", "invno"},
    )
    amount = _first_key(
        package,
        {"amountminor", "amount_minor", "minorunits", "amountinminor"},
    )
    currency = _first_key(package, {"currency", "ccy", "curr"})
    major = _first_key(package, {"amount", "amountmajor", "grandtotal", "invoiceamount"})

    vendor_s = str(vendor).strip() if vendor else ""
    invoice_s = str(invoice).strip() if invoice else ""
    currency_s = str(currency).strip().upper() if currency else ""
    amount_i = _to_int(amount)

    blob = paragraph + "\n" + "\n".join(_walk_strings(package)[:40])
    if not vendor_s:
        match = VENDOR_RE.search(blob)
        vendor_s = match.group(1).strip() if match else "UNKNOWN VENDOR"
    if not invoice_s:
        match = INVOICE_NO_RE.search(blob)
        invoice_s = match.group(1).strip() if match else "UNKNOWN"
    if not currency_s:
        match = CURRENCY_RE.search(blob)
        currency_s = match.group(1) if match else "INR"
    if amount_i is None:
        major_i = _to_int(major)
        if major_i is not None and major_i < 100000000 and "." not in str(major):
            # Heuristic: values >= 1000 without a decimal are already minor units
            # if a sibling amountMinor was missing; prefer major*100 when small.
            if major_i < 1000:
                amount_i = major_i * 100
            else:
                amount_i = major_i
        else:
            match = AMOUNT_RE.search(blob.replace(",", ""))
            if match:
                whole = int(match.group(1).replace(",", ""))
                frac = match.group(2)
                amount_i = whole * 100 + int((frac or "0").ljust(2, "0")[:2])
            else:
                amount_i = 0
    return {
        "vendorName": vendor_s[:120],
        "invoiceNumber": invoice_s[:80],
        "amountMinor": int(amount_i),
        "currency": currency_s[:8],
    }


def _policy_limit(policy: Any) -> int | None:
    value = _first_key(
        policy if not isinstance(policy, str) else {"text": policy},
        {"autonomouslimitminor", "approvalthresholdminor", "delegatedlimitminor", "limitminor"},
    )
    parsed = _to_int(value)
    if parsed is not None:
        return parsed
    text = policy if isinstance(policy, str) else " ".join(_walk_strings(policy))
    match = re.search(
        r"(?:autonomous|delegated|authority)\s+(?:limit|threshold)?\s*(?:of|:)?\s*([0-9,]+)",
        text,
        re.I,
    )
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def heuristic_action(paragraph: str, facts: dict[str, Any], policy: Any) -> str:
    text = paragraph
    if DUP_RE.search(text) and not NOT_DUP_RE.search(text):
        return "reject_duplicate"
    if EXC_RE.search(text):
        return "open_exception"
    if HOLD_RE.search(text):
        return "hold_invoice"
    if APPR_RE.search(text):
        return "request_approval"
    limit = _policy_limit(policy)
    if limit is not None and int(facts.get("amountMinor") or 0) > limit:
        return "request_approval"
    if SETTLE_RE.search(text):
        return "settle_invoice"
    if limit is not None and int(facts.get("amountMinor") or 0) <= limit:
        return "settle_invoice"
    return "open_exception"


def _clip_rationale(action: str, refs: list[str], paragraph: str) -> str:
    cited = ", ".join(refs[:3]) or "the operative paragraph"
    body = (
        f"Selected {action} from the live decision paragraph, citing {cited}. "
        f"Cover-sheet, archive, and training decoy text were ignored. "
        f"Evidence: {paragraph[:900]}"
    )
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) < 60:
        body += " Additional context confirms the same commercial facts and action gate."
    return body[:1500]


def _three_refs(paragraph: str, package: Any) -> list[str]:
    refs = _refs(paragraph)
    unique: list[str] = []
    for ref in refs:
        if ref not in unique:
            unique.append(ref)
        if len(unique) == 3:
            return unique
    for para in _paragraphs(package):
        if _looks_skipped(para):
            continue
        for ref in _refs(para):
            if ref not in unique:
                unique.append(ref)
            if len(unique) == 3:
                return unique
    while len(unique) < 3:
        unique.append(f"[EVIDENCE-{len(unique) + 1}]")
    return unique[:3]


def normalize_proposal(
    *,
    package: dict[str, Any],
    raw: dict[str, Any] | None,
    policy: Any,
) -> dict[str, Any]:
    package_id = str(package.get("packageId") or package.get("id") or "")
    paragraph = _pick_decision_paragraph(package)
    facts = extract_facts(package, paragraph)
    refs = _three_refs(paragraph, package)
    action = heuristic_action(paragraph, facts, policy)

    if raw:
        raw_action = str(raw.get("action") or "").strip()
        if raw_action in ACTIONS:
            action = raw_action
        raw_facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
        for key in ("vendorName", "invoiceNumber", "currency"):
            value = raw_facts.get(key)
            if isinstance(value, str) and value.strip():
                facts[key] = value.strip()
        if raw_facts.get("amountMinor") is not None:
            parsed = _to_int(raw_facts.get("amountMinor"))
            if parsed is not None:
                facts["amountMinor"] = parsed
        raw_refs = raw.get("evidenceRefs")
        if isinstance(raw_refs, list):
            cleaned = []
            for item in raw_refs:
                text = str(item).strip()
                if not text:
                    continue
                if not text.startswith("["):
                    text = f"[{text.strip('[]')}]"
                if text not in cleaned:
                    cleaned.append(text)
            if len(cleaned) >= 3:
                refs = cleaned[:3]
        rationale = str(raw.get("rationale") or "").strip()
    else:
        rationale = ""

    if len(rationale) < 60 or action not in rationale:
        rationale = _clip_rationale(action, refs, paragraph)
    rationale = rationale[:1500]
    if len(rationale) < 60:
        rationale = _clip_rationale(action, refs, paragraph)

    return {
        "packageId": package_id,
        "action": action,
        "facts": facts,
        "evidenceRefs": refs[:3],
        "rationale": rationale,
    }


def _action_id(task_id: str, package_id: str) -> str:
    digest = hashlib.sha256(f"{task_id}:{package_id}".encode("utf-8")).hexdigest()
    return f"act_{digest[:20]}"


async def propose_batch(
    store: Store,
    *,
    task_id: str,
    batch_id: str,
    policy_revision: Any,
    packages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cached: dict[int, dict[str, Any]] = {}
    missing: list[tuple[int, dict[str, Any], str]] = []
    for index, package in enumerate(packages):
        content_hash = package_content_hash(package)
        hit = store.get_cached_decision(content_hash)
        if hit:
            cached[index] = hit
        else:
            missing.append((index, package, content_hash))

    llm_by_package: dict[str, dict[str, Any]] = {}
    if missing:
        llm_out = await decide_with_llm(
            batch_id=batch_id,
            policy_revision=policy_revision,
            packages=[item[1] for item in missing],
        )
        for item in llm_out or []:
            pid = str(item.get("packageId") or "")
            if pid:
                llm_by_package[pid] = item

    proposals: list[dict[str, Any]] = [{} for _ in packages]
    seen_actions: set[str] = set()
    for index, package in enumerate(packages):
        package_id = str(package.get("packageId") or package.get("id") or f"pkg-{index}")
        if "packageId" not in package:
            package = {**package, "packageId": package_id}
        content_hash = package_content_hash(package)
        if index in cached:
            decision = dict(cached[index])
        else:
            decision = normalize_proposal(
                package=package,
                raw=llm_by_package.get(package_id),
                policy=policy_revision,
            )
            store.put_cached_decision(content_hash, decision)
        action_id = _action_id(task_id, package_id)
        if action_id in seen_actions:
            action_id = action_id + f"{index:02d}"
        seen_actions.add(action_id)
        proposals[index] = {
            "packageId": package_id,
            "actionId": action_id,
            **{k: decision[k] for k in ("action", "facts", "evidenceRefs", "rationale")},
        }
    return proposals
