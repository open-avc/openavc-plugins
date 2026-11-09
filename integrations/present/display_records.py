"""What a configured display holds, and the rules every door that writes one
follows.

A display is a dict in the plugin's ``displays`` config: ``id`` (a state-key
and URL segment), ``label``, ``kind`` (``browser`` or ``stream``),
``display_key`` (the secret in its Display URL), ``stream_key`` once it is a
stream display (the secret in its output path), and an optional
``local_output`` (one of this server's video outputs; a browser display only,
and one display per output).

Three doors create or change records under these rules, each in its own
manner. The load at start repairs what it can, skips what it cannot, and says
whether anything changed so the plugin saves once. The add and edit routes
refuse instead, with the HTTP status and the sentence the plugin page shows
(the statuses are part of the rule, hence HTTPException here). The plugin
writes three things itself: a regenerated display key and a regenerated
stream key (both minted here; the plugin refuses a stream key to a browser
display), and a renamed id, written only once the display's running state
has moved off the old one.
What a record's kind does to the running outputs, state keys and kiosk
windows stays with the plugin.
"""

import re
import secrets

from fastapi import HTTPException

KINDS = ("browser", "stream")

# Display ids become state-key segments and URL path segments, so keep them to
# a portable, URL-safe character set.
ID_RE = re.compile(r"[A-Za-z0-9_-]+")

# Display ids that would collide with the routing "auto" value, the sidecar's
# path prefixes (in/, out/), or the guest URL segments.
RESERVED_IDS = {
    "auto", "connect", "display", "displays", "status", "whep", "whip", "in", "out",
}


def window_allowed(kind):
    """Whether a display of ``kind`` may be shown on one of this server's
    video outputs (a local kiosk window): browser displays only."""
    return kind == "browser"


def no_such_display(display_id):
    return f"No display with id '{display_id}'."


def find(records, display_id):
    for d in records:
        if d.get("id") == display_id:
            return d
    return None


def slugify(label):
    slug = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
    return slug or "display"


def unique_id(records, label):
    """An id derived from ``label`` that no record and no reserved word holds."""
    base = slugify(label)
    did = base
    n = 2
    while find(records, did) or did in RESERVED_IDS:
        did = f"{base}_{n}"
        n += 1
    return did


def new_display_key():
    return secrets.token_urlsafe(24)


def new_stream_key():
    # Deliberately short: decoder UIs are often typed with a remote.
    # Guessing runs against MediaMTX directly (no platform rate limiter),
    # but 8 URL-safe chars is far beyond LAN brute-force reach.
    return secrets.token_urlsafe(6)


# ──── The load: repair, never refuse ────


def load_into(records, configured):
    """Append to ``records`` each saved display the load keeps; True if it changed any.

    A record without a usable id is skipped. One kept gets a display key when
    it has none (a hand-edited project file, an older plugin version), the
    browser kind when its kind is not one of ``KINDS``, a stream key when it
    is a stream display without one, and loses a local output it may not
    have: on a stream display, or on a second display claiming the same
    output (the first keeps it). The records are the saved dicts, changed in
    place, appended as they are read: a record that raises (an id that is not
    text, an output id that cannot be compared) leaves the ones before it in
    ``records``.
    """
    changed = False
    for display in configured:
        if not _loadable(display):
            continue
        changed = _repair(display) or changed
        records.append(display)
    return _one_window_per_output(records) or changed


def _loadable(display):
    if not isinstance(display, dict):
        return False
    display_id = (display.get("id") or "").strip()
    return bool(ID_RE.fullmatch(display_id)) and display_id not in RESERVED_IDS


def _repair(display):
    changed = False
    if not display.get("display_key"):
        display["display_key"] = new_display_key()
        changed = True
    if display.get("kind") not in KINDS:
        display["kind"] = "browser"
        changed = True
    changed = _mint_stream_key(display) or changed
    # local_output is a browser-display feature (hand-edited files can put it
    # anywhere). The load drops only a value it would act on; the edit route
    # drops the field whenever the kind is not browser.
    if display.get("local_output") and not window_allowed(display["kind"]):
        display.pop("local_output", None)
        changed = True
    return changed


def _one_window_per_output(records):
    """One host output can only hold one window: the first claim keeps it."""
    changed = False
    seen_outputs = set()
    for display in records:
        local_output = display.get("local_output")
        if not local_output:
            continue
        if local_output in seen_outputs:
            display.pop("local_output", None)
            changed = True
        else:
            seen_outputs.add(local_output)
    return changed


def _mint_stream_key(display):
    """A stream display has a stream key; mint one when it has none (True if minted)."""
    if display["kind"] == "stream" and not display.get("stream_key"):
        display["stream_key"] = new_stream_key()
        return True
    return False


# ──── The add and edit routes: refuse, never repair ────


def check_id(display_id):
    if not ID_RE.fullmatch(display_id or ""):
        raise HTTPException(
            422,
            "Display ID may contain only letters, numbers, hyphens, and underscores.",
        )
    if display_id in RESERVED_IDS:
        raise HTTPException(422, f"'{display_id}' is a reserved name; pick another display ID.")


def check_kind(kind):
    """The kind as stored (trimmed, lower case, browser when blank), or a 422."""
    kind = (kind or "browser").strip().lower()
    if kind not in KINDS:
        named = " or ".join(f"'{k}'" for k in KINDS)
        raise HTTPException(422, f"Display kind must be {named}.")
    return kind


def check_local_output(records, output_id, kind, exclude_id=None):
    """422/409 when a local_output assignment isn't allowed."""
    if not window_allowed(kind):
        raise HTTPException(
            422,
            "Only a browser display can be shown on one of this "
            "server's video outputs.",
        )
    for display in records:
        if display.get("id") == exclude_id:
            continue
        if display.get("local_output") == output_id:
            label = display.get("label") or display["id"]
            raise HTTPException(
                409, f"That output is already showing display '{label}'."
            )


def _required_label(data):
    label = (data.label or "").strip()
    if not label:
        raise HTTPException(422, "Display name is required.")
    return label


def _check_free_id(records, display_id, current=None):
    """A valid id that no other record holds (``current`` is the record's own)."""
    check_id(display_id)
    if display_id != current and find(records, display_id):
        raise HTTPException(409, f"A display with id '{display_id}' already exists.")


def new_record(records, data):
    """The record the add route creates from ``data`` (a DisplayIn), or a refusal.

    Checked in this order: the label, the kind, the id (derived from the label
    when blank), then the local output. Nothing is added to ``records`` here.
    """
    label = _required_label(data)
    kind = check_kind(data.kind)
    display_id = (data.display_id or "").strip() or unique_id(records, label)
    _check_free_id(records, display_id)
    local_output = (data.local_output or "").strip()
    if local_output:
        check_local_output(records, local_output, kind)
    display = {
        "id": display_id,
        "label": label,
        "kind": kind,
        "display_key": new_display_key(),
    }
    _mint_stream_key(display)
    if local_output:
        display["local_output"] = local_output
    return display


def apply_edit(records, display, data):
    """Apply the edit route's ``data`` to ``display`` in place.

    Returns the id the display is to take; the id itself is not changed
    here, because a rename first moves the display's running state off the
    old id. The label,
    kind and id are refused before anything changes. A refused local output
    is checked only after the label, kind and stream key were written, so it
    leaves those changes on the record in memory (unsaved).
    """
    display_id = display["id"]
    label = _required_label(data)
    kind = check_kind(data.kind) if data.kind is not None else display.get("kind", "browser")
    new_id = (data.display_id or "").strip() or display_id
    _check_free_id(records, new_id, current=display_id)
    display["label"] = label
    display["kind"] = kind
    _mint_stream_key(display)
    if not window_allowed(kind):
        # Local windows are a browser-display feature; a kind change
        # drops the assignment rather than carrying it invisibly.
        display.pop("local_output", None)
    if data.local_output is not None:
        _assign_local_output(records, display, data.local_output.strip(), kind)
    return new_id


def _assign_local_output(records, display, local_output, kind):
    """An edit's local output: set when allowed, cleared when blank."""
    if local_output:
        check_local_output(records, local_output, kind, exclude_id=display["id"])
        display["local_output"] = local_output
    else:
        display.pop("local_output", None)
