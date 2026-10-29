# Thinkback — senior-developer audit, 2026-10-29

## What this repo does:

Thinkback is a self-hosted memory service for AI chat apps. It keeps a conversation in
three layers: **L1** the raw turns (in PostgreSQL), **L2** a summary of the conversation
(one LLM call every few turns), and **L3** the long-term facts (Mem0 2.2.1 writing to
Milvus). You POST a turn, you get an `accepted` answer, and the facts land in the
background. You POST a recall and you get back a package of memory text, trimmed to a
token budget. The same service also offers delete, erase (real deletion), rebuild and a
small admin console.

**Load I assumed**: one internal deployment, no user accounts, a handful of internal
clients. The repo's own load test (`tests/load/smoke.k6.js`) uses 4 virtual users for 30
seconds, with a target of p95 append under 1s and p95 recall under 500ms. The service is
single-pod by default; `src/thinkback/infra/replica_guard.py` refuses to start if you set
`MEMORY_MULTI_REPLICA=true` without the prerequisites. So: **low traffic, one process,
long uptime**. The bugs below are all things that happen at that load, except where I say
otherwise.

**Status when this report was written (read me first):** another engineer was fixing
findings in the same working tree while I audited. Six of the sixteen are already fixed
in the working tree (not committed yet), with new tests next to them:

| Finding | State now |
|---|---|
| 1 — erase leaves engine text | **fixed** (`tests/unit/test_erase_history_coverage.py`) |
| 3 — Idempotency-Key cross-client | **fixed** (`api/idempotency.py` now keys the room on the caller, not the peer IP) |
| 4 — empty recall package | **fixed** (`domain/recall_policy.py` gives an empty layer one item from the leftover budget) |
| 5 — false conflict on edit replay | **fixed** (`service.py` now compares the raw type name) |
| 6 — erase fails at 4096 records | **fixed** (same new test file) |
| 7 — LLM-down refresh storm | **fixed** (`l2_refresh.py` now remembers *attempted* sessions) |

Findings 2, 8, 9, 10, 11, 12, 13, 14, 15 and 16 were still open when I finished.
Read the six fixed ones as "what went wrong, and the test that now pins it" — they are
the easiest way to check the fixes are real and not just different code.

Credit where it is due: the fix for finding 6 also caught a bug I missed — the purge built
one `IN (?, ?, …)` list, and SQLite refuses more than 999 placeholders, so erasing a
session with a thousand turns used to blow up. It is chunked now.

---

## Must fix

### 1. Erase says "done" while the original text is still in the engine's history file
**Status: fixed in the working tree (not committed) — see the table above.**
(`src/thinkback/memory/service.py:2456-2481`, `src/thinkback/memory/backends/mem0_library.py:651-745`)

- **What this is:** `erase` is the "really delete everything" operation. It removes the
  conversation turns, the summary, the index rows, and the vectors. It also writes an audit
  line saying how much it cleaned.
- **Problem:** Mem0 keeps a second copy of every fact in a local SQLite file called
  `history.db`. The erase step cleans that file, but only for the memory IDs it can see in
  the local index. The ALL-scope erase deletes *every* record in the vector store, including
  records with no local index row (the code itself calls these "dark data"). Those records'
  text stays in `history.db` forever, and the audit line says
  `mem0_history_purged: "complete"` without checking. Mem0 writes the original text into
  the history file on every delete (`mem0/memory/main.py:2126`), so even the delete event
  leaves a copy behind.
- **Fix:** Collect the IDs that `delete_all` actually removed and pass those to the history
  purge, instead of only the local-index IDs. Or: make the purge delete by user and scope
  over the whole history file rather than by memory ID.
- **If we skip it:** We tell a customer "physically erased" while their text is still on
  disk. That is the kind of promise that turns into a legal problem.
- **Repro:** `/tmp/tbhunt/p7b_mem0_purge.py` — the history table still holds
  `('dark-1', 'ADD', None, '暗数据里的秘密事实')` after the erase returns `completed`.

### 2. Deleting one fact hides the whole conversation turn from recall
(`src/thinkback/memory/service.py:1780-1790`)

- **What this is:** Every fact remembers which chat turn it came from. When a fact is
  deleted, the service builds a "do not show these turns" list and skips those turns when
  it builds the recall package.
- **Problem:** The list is built per **turn**, not per fact. So if one turn produced two
  facts and you delete one of them, the entire turn's raw conversation text stops being
  recalled — while the second fact from that same turn is still active and still gets
  recalled. The turn's text is still in the database (the code says it deliberately keeps
  it: "多事实共享轮次时保留原文"), but it never reaches the model again. In my repro the
  recall came back completely empty.
- **Fix:** Build the skip list per fact, and skip only the turns whose facts are *all*
  gone. Or: keep the turn text and only drop the specific fact.
- **If we skip it:** A user who deletes one thing loses a whole exchange of context, and
  the model starts contradicting itself because one fact is still there and its source is
  not.
- **Repro:** `/tmp/tbhunt/p3_delete_one.py` — `recall AFTER delete of ONE fact: []`.

### 3. One client's Idempotency-Key blocks another client's write
**Status: fixed in the working tree (not committed) — see the table above.**
(`src/thinkback/api/idempotency.py:236-246`)

- **What this is:** The Idempotency-Key header lets a client retry a request safely: the
  server replays the first answer instead of doing the work twice. The server keeps a
  cache of answers, and it splits the cache into separate "rooms" so two clients cannot
  read each other's answers.
- **Problem:** The room is chosen by `request.client.host`, the IP of whoever connected.
  Behind a Kubernetes ingress, a sidecar or a reverse proxy, that IP is the *proxy's*, not
  the caller's — so every client lands in the same room. Now, if client A uses key `k-1`,
  client B using key `k-1` with a different body gets a 409 "key reused with different
  request body" and its write is thrown away. One client's key poisons everyone else's.
- **Fix:** Use a real tenant id in the key. If there is no auth, then at minimum include
  the user id from the request body in the room key, not just the peer IP.
- **If we skip it:** Two unrelated teams can take each other's writes down with one
  unlucky key.
- **Repro:** `/tmp/tbhunt/p12_idem.py` — tenant B gets 409.

### 4. Recall can return an empty package while the whole token budget sits unused
**Status: fixed in the working tree (not committed) — see the table above.**
(`src/thinkback/domain/recall_policy.py:230-254` and `:256-345`)

- **What this is:** Recall trims the package to a token budget. The budget is split
  between the three layers (30% to L1, 20% to L2, 50% to L3), and unused parts are
  pushed back to layers that still need room.
- **Problem:** The split can hand a layer less than the cost of one item. With a budget of
  100 and two items of 60 tokens each (one L1 turn, one L3 fact), L1 gets 48 and L3 gets
  51. Both items are then dropped, because each one costs more than its layer's share.
  The answer is empty and 100 tokens of budget were never used. The same thing happens at
  the default budget of 1200 when one L1 turn is 400 tokens long: the turn is dropped even
  though the budget could have paid for it.
- **Fix:** When an item does not fit in its layer's share, still try to fit it in the
  budget that is left over after the other layers are served. Or: give a layer back its
  share when nothing of that layer fits.
- **If we skip it:** The model gets an empty memory package and answers from nothing. The
  budget knob looks like it is working, because the number is right.
- **Repro:** `/tmp/tbhunt/p6b_budget.py` — `budget=100: kept layers=[] count=0`.

### 5. Replaying an edit of a custom-type memory reports a false conflict
**Status: fixed in the working tree (not committed) — see the table above.**
(`src/thinkback/memory/service.py:2753-2768` and `:2946-2976`)

- **What this is:** Editing a memory takes an `operation_id`. Sending the same
  `operation_id` again must return the same answer (`already_done`) instead of editing
  twice. The service stores the result of the edit in the task row so it can compare later.
- **Problem:** The stored value is `memory_type`, and for a custom type (one the five
  built-in types do not cover, e.g. `process_preference`) the API object has no
  `memory_type` — only `memory_type_name`. So the task row stores `None`. On the replay,
  the stored `None` is compared with the real value `process_preference`, they differ, and
  the service raises "operation_id conflict". The memory was edited fine. The client gets a
  409 that says "someone else changed this".
- **Fix:** Store `memory_type_name` (the full name) in the task result instead of the
  enum value, and compare that.
- **If we skip it:** A client that retries an edit because it never saw the answer gets
  told there is a conflict, and stops. The edit actually succeeded, so the client and the
  server now disagree about the state.
- **Repro:** `/tmp/tbhunt/p5_update_replay.py` — `replay update RAISED: ValueError
  operation_id conflict`.

### 6. Erase fails forever once a user has 4096 or more engine records
**Status: fixed in the working tree (not committed) — see the table above.**
(`src/thinkback/memory/service.py:2523-2562`)

- **What this is:** After deleting from the vector store, the erase checks that the record
  is really gone by listing what is left. The list is capped at 4096 records
  (`MEMORY_LIST_PAGE_LIMIT`).
- **Problem:** If the user has 4097 or more records in that scope, the check list is always
  full. The code then gives up and reports "I could not prove it is deleted" — even when
  the target record is gone (my repro: 4097 records before, 4096 after, still failed). It
  also reports the wrong reason: "1 record(s) still present" when zero records are still
  present. Retrying with the same `operation_id` hits the same wall, so the erase can
  never finish.
- **Fix:** Look the target IDs up directly (ask the store "are these IDs still there?")
  instead of listing everything and comparing. If the list is full, page through it
  instead of giving up.
- **If we skip it:** Big users can never delete one memory. Support cannot fix it by
  retrying, because retrying is what fails.
- **Repro:** `/tmp/tbhunt/p11_erase_big.py`.

---

## Should fix

### 7. When the LLM is down, every new turn triggers another LLM call
**Status: fixed in the working tree (not committed) — see the table above.**
(`src/thinkback/memory/l2_refresh.py:127-128` and `:286`)

- **What this is:** The L2 summary is refreshed in the background, at most once every few
  turns per session. If the refresh fails, the summary stays as the plain concatenation and
  nothing is retried — that is what the module promises: "LLM 失败：打 warning 后保留拼接版，
  无重试风暴".
- **Problem:** The code only marks a session as "has an LLM summary" when a refresh
  **succeeds**. When it fails, the session is never marked, so the next turn looks like
  "this session has no LLM summary yet" and triggers another refresh. My repro: 6 turns
  produced 6 LLM calls, not the 2 the debounce promises.
- **Fix:** Mark the session as "refresh attempted" whether the refresh succeeded or not,
  and only allow a new attempt after the debounce interval.
- **If we skip it:** During an LLM outage the service hammers the broken dependency once
  per turn, per session. That turns a slow dependency into a load problem.

### 8. Every append and every recall reads every deleted memory of that user
(`src/thinkback/memory/service.py:1785` and `:5633`, `src/thinkback/memory/repositories/sqlalchemy.py:1281-1300`)

- **What this is:** Before answering a recall or writing a summary, the service needs the
  list of turns whose facts were deleted, so it can leave them out.
- **Problem:** That list is built by reading the `source_refs` column of *every* deleted,
  superseded or forgotten memory of the user, with no limit. Nothing ever removes those
  rows: the retention job only cleans rows that have an expiry time, and deleted facts
  usually have none. So the list grows for the life of the deployment, and every append and
  every recall pays for it.
- **Fix:** Keep the exclusion list in its own small table with an index, or delete the
  source refs when the fact row is deleted. A cap plus a time window is enough.
- **If we skip it:** Recall and append get slower every month the service runs, with no
  error to show for it.

### 9. A stuck backend call eats one of the eight write slots forever
(`src/thinkback/api/memory.py:210-250`)

- **What this is:** The API runs each request on a small thread pool (8 read slots, 8 write
  slots). If a request takes longer than 5 seconds the caller gets a 503 "queue full".
- **Problem:** The slot is given back only when the thread finishes, not when the client
  gives up. A thread stuck in a call to Mem0 or Milvus that never returns keeps its slot.
  After 8 stuck calls, every write returns 503 and nothing recovers until the process
  restarts.
- **Fix:** Give the slot back when the request times out, and let the stuck thread die on
  its own. Or: put a timeout on the backend client so no thread can hang.
- **If we skip it:** One backend hiccup that hangs can take the whole write path down.

### 10. Replaying a turn pushes the expiry date of a TTL memory further out
(`src/thinkback/memory/service.py:4838-4864` and `:2978-2989`)

- **What this is:** Some memory types expire — `state` after 90 days, `commitment` after
  30. The expiry is written into the index row when the fact is indexed.
- **Problem:** The expiry is computed from the clock at index time ("now + 90 days"), and
  the original expiry date is not saved anywhere. So any replay — a retried append, the
  crash recovery, a rebuild — recomputes it from the replay time. My repro: a fact written
  today and replayed 80 days later moved its expiry 80 days further out, so it now lives
  for 170 days instead of 90.
- **Fix:** Save the expiry date in the journal entry when the turn is first written, and
  reuse it on every replay.
- **If we skip it:** The TTL policy is not a policy; it is a rolling window that every
  retry moves.

### 11. The engine's raw messages are cleaned up by a different string than the one stored
(`src/thinkback/memory/backends/mem0_library.py:729-736`)

- **What this is:** Mem0 keeps the last 10 raw messages per user in the same SQLite file.
  For a single-item or single-session erase, the service deletes those messages by their
  text.
- **Problem:** The service trims the text before matching (`"hello"`), but Mem0 stored it
  untrimmed (`"  hello  "`). The delete matches nothing and the raw message stays.
- **Fix:** Delete by message ID, or trim both sides before matching.
- **If we skip it:** Raw text survives an erase that tried to remove it. The audit already
  admits messages are only partly cleaned for these two scopes, so this is not a false
  claim — but it is not what the code says it does.
- **Repro:** `/tmp/tbhunt/p10_msg_purge.py`.

### 12. `MemoryService` is one 5798-line class that does eight different jobs
(`src/thinkback/memory/service.py:217-5798`)

- **What this is:** The class holds append, recall, delete, erase, rebuild, update, the
  admin list/get calls and the task recovery. Each is a separate workflow with its own
  rules.
- **Problem:** The rules for one workflow leak into another. The exclusion set that hides
  a turn from L1 (finding 2) is also used by the summary writer and the rebuild, because
  they all sit in the same class and share the same helper. The same state-changing rule
  ("a deleted fact also removes its source turn") is written in four places. There is no
  test that can cover one workflow without running the others.
- **Fix:** Move each workflow into its own file with its own rules — `append.py`,
  `recall.py`, `erase.py`, `rebuild.py` — and keep the shared helpers in one place. Move
  by job, not by line count.
- **If we skip it:** Every fix in this list touches four workflows by accident, and the
  next person cannot tell which rules belong where.

### 13. The engine-history erase path has no test at all
(`tests/unit/test_memory_backends.py`, `src/thinkback/memory/backends/mem0_library.py:651-745`)

- **What this is:** The fake backend used in tests returns
  `{"history": 0, "messages": 0, "supported": False}` and does nothing. So the code that
  deletes real rows out of a real SQLite file is never run in any test.
- **Problem:** That code found finding 1 and finding 11. Both are silent: the erase
  reports success and the text stays.
- **Fix:** One test that creates a SQLite file with Mem0's own table layout, puts text in
  it, runs the purge, and checks the text is gone. The table layout is short — it is in
  `mem0/memory/storage.py`.
- **If we skip it:** The next person to change the purge has no signal that they broke it.

---

## Nice to have

### 14. A validator that does nothing
(`src/thinkback/memory/schemas.py:105-108`)

- **What this is:** `accept_content_alias` is a "before" validator on `content` that just
  returns the value unchanged.
- **Problem:** The alias `normalized_content` is already handled by
  `validation_alias=AliasChoices("content", "normalized_content")` on the field above it.
  The validator adds nothing.
- **Fix:** Delete the validator and its three doc lines. The alias keeps working.
- **If we skip it:** Nothing breaks. It just makes a reader hunt for the alias rule.

### 15. A public method nobody calls
(`src/thinkback/api/idempotency.py:147-148`)

- **What this is:** `IdempotencyCache.clear_expired()` removes expired entries.
- **Problem:** Nothing calls it. The cache already prunes itself on every read and write
  (`_prune_expired`).
- **Fix:** Delete the method.
- **If we skip it:** Nothing breaks. It is just a promise the cache does not need.

### 16. The idempotency cache scans its whole store on every request
(`src/thinkback/api/idempotency.py:63-70`)

- **What this is:** The cache holds up to 10000 answers. Before every read and every write
  it walks the whole store to drop the expired ones.
- **Problem:** That is 10000 checks per request, per request, on a hot path. It also means
  the cache can never expire anything that nobody touches, so the store stays full.
- **Fix:** Keep the expiry time on the key itself and drop entries lazily when they are
  found, or sweep on a timer.
- **If we skip it:** At the expected load it is not visible. It is the kind of thing that
  shows up as "the API got slow after we deployed".

---

## Verdict:

Not healthy yet — the erase contract lies in two places (findings 1 and 6), deleting one
thing silently deletes a whole turn (finding 2), and one client's key can block another
client's write (finding 3). Fix 1, 2, 3 and 6 first; they are all about telling the user
the truth.

**Lean: -12 lines, -0 dependencies possible.** Two dead-code items (findings 14 and 15)
and no dependency can be dropped — the 17 runtime packages are all used on a real path.

**Not checked:** The PostgreSQL path was never run against a real database — every
PostgreSQL claim in this report comes from reading the code, and the repro scripts use the
in-memory repository. Mem0 and Milvus were never run either; the engine findings use a
SQLite file with Mem0's own table layout and a client with Mem0's own method signatures.
Not read at all: `src/thinkback/api/admin.py`, `src/thinkback/api/health.py`,
`src/thinkback/api/metrics.py`, `src/thinkback/infra/readiness.py`, `src/thinkback/rpc/`
(the gRPC layer past the converter), `web/` (48 files of React), `k8s/`, `alembic/`, and
`tests/eval/` and `tests/script/` (the real-stack test harnesses, which I read only enough
to learn the expected load).
