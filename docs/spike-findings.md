# Phase-0 Spike Findings

**Date:** 2026-06-14  
**anki package version:** 25.9.2  
**Python:** 3.11 (python:3.11-slim container)  
**Collection under test:** backup snapshot with 3741 revlog entries, ~5000 cards  
**Method:** All experiments ran against `/tmp/anki-spike/collection.anki2` (a copy of the static backup). The live collection was never touched.

---

## Unknown 1 — Deck-scoped review queue

**VERDICT: PASS**

### Question
Does `col.decks.select(deck_id)` correctly scope `col.sched.get_queued_cards()` to only that deck (and its subdecks)?

### Answer: YES

`col.decks.select(deck_id)` sets the scheduler's active deck and `col.sched.get_queued_cards(fetch_limit=N)` respects that scope completely. Every card returned had a `did` matching the selected deck (or a child deck when a parent was selected).

### Exact working call sequence

```python
from anki.collection import Collection

col = Collection("/path/to/collection.anki2")

# 1. Resolve a deck id
deck_id = col.decks.id_for_name("Latvian (ChatGPT)::Vocab & Sentences")
# or pick from:
# all_decks = col.decks.all_names_and_ids()  # returns list of DeckNameId(id, name)

# 2. Select the deck (scopes the scheduler)
col.decks.select(deck_id)

# 3. Confirm selection
assert col.decks.get_current_id() == deck_id

# 4. Fetch queued cards (idempotent — same card returned until answered)
queued = col.sched.get_queued_cards(fetch_limit=1)
if queued and queued.cards:
    qc = queued.cards[0]
    card = col.get_card(qc.card.id)
    # qc.card.id, qc.states (for describe_next_states), qc.top_deck_id

col.close()
```

### Observed output

Testing deck `'Latvian (ChatGPT)::Vocab & Sentences'` (id=1764258264301):
```
col.decks.get_current_id() after select: 1764258264301  # matches
get_queued_cards returned 5 cards:
  card id=1770179274771 deck='Latvian (ChatGPT)::Vocab & Sentences'
  card id=1779283191178 deck='Latvian (ChatGPT)::Vocab & Sentences'
  ... (all 5 cards in correct deck)
```

Idempotency:
```
First call  card id: 1770179274771
Second call card id: 1770179274771
IDEMPOTENT: True
```

### Subdeck inclusion

When a parent deck is selected, `get_queued_cards` returns cards from all descendant decks (confirmed with `'Latvian (ChatGPT)'` parent — cards surfaced from `'Latvian (ChatGPT)::Vocab & Sentences'` child, all confirmed `in_tree=True`).

`col.decks.children(deck_id)` returns a `list[tuple[str, int]]` — each element is `(child_deck_name, child_deck_id)`. Use `{cid for _, cid in col.decks.children(deck_id)}` to build the child id set.

### Caveat

`col.sched.counts_for_deck_today()` does **not** exist in anki 25.9.2. Use raw SQL or `col.db.all("SELECT did, COUNT(*) FROM cards GROUP BY did")` to count cards per deck. The scheduler's `counts()` method returns aggregate counts for the currently selected deck.

---

## Unknown 2 — Enable FSRS + optimize weights from existing revlog

**VERDICT: PASS**

### Question
What is the correct API to enable FSRS and compute optimized parameters from the collection's existing revlog in anki 25.9.2?

### Answer

FSRS is enabled via `col.set_config("fsrs", True)`. Weight optimization uses `col._backend.compute_fsrs_params(...)` directly — there is **no** `col.compute_fsrs_params()` wrapper. The optimized params are applied via `col.decks.update_config(cfg_dict)`.

### Exact working call sequence

```python
from anki.collection import Collection

col = Collection("/path/to/collection.anki2")

# Step 1: Enable FSRS
col.set_config("fsrs", True)
assert col.get_config("fsrs") is True

# Step 2: Compute optimized FSRS parameters from revlog
# Signature (anki 25.9.2):
#   backend.compute_fsrs_params(
#       *, search: str, current_params: Iterable[float],
#       ignore_revlogs_before_ms: int, num_of_relearning_steps: int,
#       health_check: bool
#   ) -> ComputeFsrsParamsResponse
#
# Returns ComputeFsrsParamsResponse with fields:
#   .params       — repeated float (21 values for FSRS-6)
#   .fsrs_items   — int (number of review items used)
#   .health_check_passed — bool

result = col._backend.compute_fsrs_params(
    search="",                    # empty = use all cards
    current_params=[],            # empty = start from defaults
    ignore_revlogs_before_ms=0,  # 0 = use entire revlog history
    num_of_relearning_steps=1,   # match your deck's relearning steps
    health_check=True,            # validates data quality
)
optimized_params = list(result.params)
# len(optimized_params) == 21 (FSRS-6 parameter count)
print(f"Used {result.fsrs_items} review items, health_check_passed={result.health_check_passed}")

# Step 3: Apply params to all deck presets
all_configs = col.decks.all_config()  # returns list of dict
for cfg in all_configs:
    cfg["fsrsWeights"] = optimized_params      # legacy field (still used)
    cfg["fsrsParams5"] = optimized_params[:17] # FSRS-5 subset
    cfg["fsrsParams6"] = optimized_params      # FSRS-6 (21 params)
    col.decks.update_config(cfg)

col.close()
```

### Observed output (from 3741 revlog entries, 1107 FSRS items)

```
revlog entries: 3741
FSRS enabled via set_config: True

backend.compute_fsrs_params result:
  params (21 values): [0.09638, 0.13122, 1.44981, 4.88316, 6.41613, 0.72444, 
                        3.05182, 0.00100, 1.85325, 0.17440, 0.78059, 1.45029,
                        0.09150, 0.22822, 1.62697, 0.56336, 1.86455, 0.67107,
                        0.06436, 0.05722, 0.13007]
  fsrs_items: 1107
  health_check_passed: true

update_config() succeeded for all 3 deck presets
```

### FSRS scheduling verification

With FSRS enabled, `col.sched.describe_next_states(qc.states)` returns human-readable interval labels:

```python
queued = col.sched.get_queued_cards(fetch_limit=1)
qc = queued.cards[0]
labels = col.sched.describe_next_states(qc.states)
# Example: ['<⁨1⁩m', '⁨1⁩d', '⁨1⁩d', '⁨3⁩d']
# Order: [Again, Hard, Good, Easy]
```

The states protobuf (`qc.states`) contains full FSRS memory state (stability, difficulty) for each next-state option.

### API names confirmed for anki 25.9.2

| Symbol | Type | Notes |
|--------|------|-------|
| `col.set_config("fsrs", True)` | method | Enables FSRS globally |
| `col.get_config("fsrs")` | method | Returns `True` / `False` |
| `col._backend.compute_fsrs_params(...)` | backend method | No col-level wrapper exists |
| `col.decks.all_config()` | method | Returns `list[dict]` |
| `col.decks.update_config(cfg: dict)` | method | Saves preset changes |
| `col.sched.describe_next_states(states)` | method | Returns `list[str]` |
| `col.sched.get_queued_cards(fetch_limit=N)` | method | Returns `GetQueuedCardsResponse` |

### Minimum revlog requirement

The optimizer succeeded with 1107 FSRS items extracted from 3741 revlog rows. `health_check_passed=True` was observed. No documented minimum, but the optimizer likely needs at least ~30–50 items to produce meaningful parameters. Pass `health_check=False` if you want to force-run on small collections.

### Note: `col.optimize()` is NOT for FSRS

`col.optimize()` takes no arguments and runs `VACUUM` + `ANALYZE` on the SQLite database. It has nothing to do with FSRS weight optimization.

---

## Unknown 3 — Force an UPLOAD-wins sync headlessly

**VERDICT: PASS**

### Question
What is the exact headless code sequence to always upload (never pull) when syncing against the AnkiWeb-compatible sync server?

### Answer

The full sequence is: `col.sync_login()` → inspect `sync_collection()` return value → if `required == FULL_UPLOAD` (4), call `col.full_upload_or_download(auth=auth, server_usn=None, upload=True)`. For a cutover "always upload" policy, skip the check and call `full_upload_or_download` directly.

### Exact working call sequence

```python
from anki.collection import Collection
from anki.sync_pb2 import SyncCollectionResponse

col = Collection("/path/to/collection.anki2")

# Step 1: Authenticate
auth = col.sync_login(
    username="spikeuser",
    password="spikepass",
    endpoint="http://your-sync-server:8080"   # AnkiWeb: "https://sync.ankiweb.net"
)
# auth is SyncAuth with fields: hkey (str), endpoint (str)

# Step 2: Normal incremental sync (check what server requires)
output = col.sync_collection(auth, sync_media=False)
# output is SyncCollectionResponse with fields:
#   required  — int enum: 0=NO_CHANGES, 1=NORMAL_SYNC, 2=FULL_SYNC, 4=FULL_UPLOAD
#   new_endpoint — str (if server redirects)

print(f"sync required: {output.required}")  # 4 = FULL_UPLOAD when server is empty

# Step 3: Force upload-wins (cutover / authority-wins policy)
# Use this when you want the local copy to always win (Tilts is content authority):
col.full_upload_or_download(
    auth=auth,
    server_usn=None,  # None = don't check server USN
    upload=True       # True = upload local, False = download from server
)
# Returns None on success; raises on error.

col.close()
```

### Observed output

```
sync_login success: type=<class 'anki.sync_pb2.SyncAuth'>
auth fields: hkey: "e94137cf7decd139bdaff330a3d72a644287b18c"
             endpoint: "http://172.17.0.1:27799"

sync_collection result type: <class 'anki.sync_pb2.SyncCollectionResponse'>
sync_collection result: required: FULL_UPLOAD
  required: 4
  new_endpoint: ""

full_upload_or_download(auth=auth, server_usn=None, upload=True) -> None  # success
```

### SyncCollectionResponse.required enum values

| Value | Meaning |
|-------|---------|
| 0 | `NO_CHANGES` — already in sync |
| 1 | `NORMAL_SYNC` — incremental sync needed |
| 2 | `FULL_SYNC` — full sync needed (server decides direction) |
| 4 | `FULL_UPLOAD` — server is empty, upload required |

### Cutover "upload-always" pattern (src/sync.py reference)

```python
def force_upload(col: Collection, username: str, password: str, endpoint: str) -> None:
    """Upload local collection to sync server, always winning on conflict."""
    auth = col.sync_login(username=username, password=password, endpoint=endpoint)
    col.full_upload_or_download(auth=auth, server_usn=None, upload=True)
```

### API names confirmed for anki 25.9.2

| Symbol | Signature | Notes |
|--------|-----------|-------|
| `col.sync_login` | `(username: str, password: str, endpoint: str | None) -> SyncAuth` | |
| `col.sync_collection` | `(auth: SyncAuth, sync_media: bool) -> SyncOutput` | `SyncOutput` = `SyncCollectionResponse` |
| `col.full_upload_or_download` | `(*, auth: SyncAuth | None, server_usn: int | None, upload: bool) -> None` | upload=True for force-upload |
| `col.abort_sync` | `() -> None` | for cancellation |
| `col.sync_media` | `(auth: SyncAuth) -> None` | separate media sync |

### Networking note

When running the anki package inside a Docker container, use `--network host` OR point the endpoint at the Docker bridge IP (`172.17.0.1`) to reach a sync server on the host. The test used `--network host` with `endpoint="http://172.17.0.1:27799"`.

---

## Unknown 4 — Headless template rendering of Latvian cards

**VERDICT: PASS**

### Question
Do `card.question()`, `card.answer()`, `card.css()` render HTML headlessly without Qt, and do audio/image markers appear in the expected Anki 25.x format?

### Answer: YES — all three render without Qt, no exception raised.

Audio appears as `[anki:play:q:0]` / `[anki:play:a:0]` / `[anki:play:a:1]` (Anki 25.x format, **not** `[sound:...]`). Images appear as `<img src="filename.png">`. No base64 or Qt rendering required.

### Note type inventory

The collection contains these note types relevant to Latvian learning:

| id | name |
|----|------|
| 1761927342965 | `4-Card Template` |
| 1764259307661 | `4-Card Template v2` |
| 1590918401009 | `Latvian Vocab` |
| 1590801906587 | `Latvian Basic` |
| 1764210014033 | `Latvian Dialogue` |
| 1764257528458 | `Latvian Pattern` |
| 1762134983065 | `LE - Listening - English > Latvian` |
| 1762134947746 | `LE - Listening - Latvian > English` |
| 1762134899801 | `LE - Reading - English > Latvian` |
| 1762134533237 | `LE - Reading - Latvian > English` |

The primary vocab model in active use is `4-Card Template` (id=1761927342965).

### Exact working call sequence

```python
from anki.collection import Collection

col = Collection("/path/to/collection.anki2")

# Find cards for the Latvian vocab model
note_ids = col.find_notes("mid:1761927342965")   # 4-Card Template
card_ids = col.find_cards(f"nid:{note_ids[0]}")

card = col.get_card(card_ids[0])

# All three render headlessly without Qt:
question_html = card.question()   # str containing full HTML
answer_html   = card.answer()     # str containing full HTML
css           = card.css()        # str (deprecated — use card.render_output() instead)

# Preferred modern API:
render = card.render_output()     # RenderOutput with .question_text, .answer_text, .css

col.close()
```

### Observed rendered output (card template_idx=0, word "tomēr")

**question() (204 chars):**
```html
<style>.card {
    font-family: arial;
    font-size: 20px;
    ...
}</style>tomēr[anki:play:q:0]<br>
<i>TOH-mehr</i>
```

**answer() (328 chars):**
```html
<style>...</style>tomēr[anki:play:a:0]<br>
<i>TOH-mehr</i><br><br>
<b>however</b><br>
<img src="lv-noun-tomer-v1-object.png"><br><br>
<hr>
<b>Example:</b><br>
[anki:play:a:1]<br>
```

### Audio/image marker format (anki 25.x)

```python
import re

q = card.question()
a = card.answer()

# Audio markers — Anki 25.x format (NOT [sound:...])
play_markers = re.findall(r'\[anki:play:[^\]]+\]', q + a)
# Example: ['[anki:play:q:0]', '[anki:play:a:0]', '[anki:play:a:1]']

# Image tags — standard HTML
img_tags = re.findall(r'<img[^>]+>', q + a)
# Example: ['<img src="lv-noun-tomer-v1-object.png">']

# Old [sound:...] format — NOT present in anki 25.x renders
# (confirmed: zero [sound:...] markers in rendered output)
```

The `[anki:play:q:0]` format: `q` = question side, `a` = answer side; the integer is the index into the card's sound list.

### Card template structure (4-Card Template, 4 card types)

| `card.ord` | Card type | Question shows | Answer reveals |
|------------|-----------|----------------|----------------|
| 0 | LV → EN | Latvian word + pronunciation audio | English meaning + image + example audio |
| 1 | EN → LV | English meaning | Latvian word + pronunciation audio + image |
| 2 | Listen → EN | Pronunciation audio only | English meaning + Latvian word |
| (3) | 4th card | (varies) | (varies) |

### Deprecation notice

`card.css()` raises a deprecation warning: `css() is deprecated: use card.render_output() directly`. Use `card.render_output()` in production code (`RenderOutput` has `.question_text`, `.answer_text`, `.css`).

### No rendering quirks for Tilts

- No `{{type:...}}` fields found (no typing input required)
- No cloze deletions in the 4-Card Template model
- CSS is minimal and self-contained in the rendered HTML `<style>` block
- No base64-embedded media detected (images are filename refs only)

---

## Summary

| Unknown | Verdict | Key finding |
|---------|---------|-------------|
| 1 — Deck-scoped queue | **PASS** | `col.decks.select(id)` + `col.sched.get_queued_cards()` correctly scopes to deck+subdecks; idempotent until card is answered |
| 2 — FSRS enable + optimize | **PASS** | Enable: `col.set_config("fsrs", True)`; Optimize: `col._backend.compute_fsrs_params(search="", current_params=[], ignore_revlogs_before_ms=0, num_of_relearning_steps=1, health_check=True)`; returned 21-param FSRS-6 vector from 1107 items |
| 3 — Force upload-wins sync | **PASS** | `col.sync_login()` → `col.sync_collection()` → `col.full_upload_or_download(auth=auth, server_usn=None, upload=True)`; `SyncCollectionResponse.required == 4` means FULL_UPLOAD needed |
| 4 — Headless template render | **PASS** | `card.question()` / `card.answer()` work with zero Qt; audio as `[anki:play:q:0]`; images as `<img src="...">` |

No blockers found. Steps 4–8 can proceed.
