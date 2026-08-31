# stupid-log.md

A retrospective on building `fast_sync` (2026-08-31). Six commits, four of them
fixes for defects I introduced. Written because the question deserves a straight
answer: the task was small and the code around it already worked, so the mess was
mine, not the problem's.

**One-sentence root cause: the entire task was "match customer names to plant
names", and I never looked at the names.**

---

## The shape of it

| Commit | What it was |
|---|---|
| `c604ee2` | the feature |
| `524945a` | the UI I should have built in the first commit |
| `90db3fe` | fix — stale numbers left on screen after a failure |
| `0f035fa` | fix — one customer matched 32 unrelated plants |
| `e187663` | fix — my fix for `0f035fa` broke the normal case |
| `3d08967` | fix — three cosmetic defects |

Two of those (`0f035fa`, `e187663`) are the same bug, fixed twice, because the
first fix treated the symptom.

---

## Mistake 1 — I never looked at the real data

The feature is a name matcher. I designed the matching rule from imagination and
tested it against names I made up:

```python
CUSTOMERS = [{"name": "Ah Seng"}, {"name": "Ah Seng Trading"}, {"name": "Lim Solar"}]
PLANTS    = [{"plant_name": "Ah Seng"}, {"plant_name": "Taman Molek"}]
```

Real data looks like this:

```
customer:  JCLANDS CAPITAL SDN.BHD.
plants:    JClands capital SDN BHD (Restaurant) - SELCO
           JClands capital SDN BHD (Petrol Station)-SELCO
```

Plants are named `<customer> (<site>) - <installer>`. A customer name is only ever
a **prefix** of its plants. Ten minutes reading actual `saj_plant.plant_name`
values would have given me the rule on the first try. Instead I invented test
fixtures that agreed with whatever I had just written, and they passed at every
stage — including every stage where the behaviour was wrong.

**73 passing tests did not catch a bug that one real name would have.**

## Mistake 2 — I had the evidence, printed it, and didn't read it

I ran a read-only survey against prod and reported this to the user:

```
unlinked plants a name match could link: 0
```

I filed that under "interesting data-quality note about your database." It was
not. It was a direct measurement that **my matching rule could not link a single
one of the 379 unlinked plants** — the exact population the feature exists to
serve. The number was a verdict on my design and I read it as trivia about theirs.

I also sampled 400 plant names and saw `Chen Wei Fung`, `Goh Hee Tong`,
`Chan Cheng Fatt` — all person-shaped, all short. I never noticed I was sampling
only *linked* plants, i.e. precisely the ones where the naming is simple because
the nightly sync already matched them. The hard cases were in the half I skipped.

## Mistake 3 — I reused a function outside the semantics it was written for

`sync_customer_plants.norm_name()` strips every non-alphanumeric character:

```python
"JCLANDS CAPITAL SDN.BHD." -> "jclandscapitalsdnbhd"
"Chen"                     -> "chen"
"Cheng Khing Yin"          -> "chengkhingyin"
```

In its original home it is only ever used for **equality**. I reused it for
**containment** — `if q in norm_name(...)` — and that is where the whole mess
starts. Stripping spaces destroys word boundaries, so `"chen" in "chengkhingyin"`
is `True`. Customer "Chen" reached 32 unrelated sites.

The normalization was fine. Substring-matching *its output* was not. I never
questioned whether a helper's guarantees survived the new use.

## Mistake 4 — I fixed the symptom, and the fix broke the general case

When "Chen → 32 plants" appeared, the correct move was to ask *why* a substring
test misfired. Instead I made the rule maximally strict — exact match only, no
fallback — because that made the visible bad thing stop.

It also made a company reach its own plants **never**, since the customer name is
always a prefix and never an exact match. I turned a noisy bug into a silent one
and shipped it. The user found it in the next message.

Worth naming: I did this immediately after the user was angry. I optimised for
*making the complaint go away* instead of *understanding the mechanism*. Pressure
is exactly when a symptom-level fix feels most like decisiveness.

The actual fix — match whole words — was small, and resolves both cases at once:

```
{chen} ⊄ {cheng, khing, yin}                                   # 32 false positives gone
{jclands, capital, sdn, bhd} ⊆ {jclands, capital, sdn, bhd,
                                restaurant, selco}             # the company matches
```

## Mistake 5 — I built a curl-only feature in a repo full of browser pages

The user's reaction — *"I CAN'T SEE ANYTHING"* — was correct, and the evidence was
in front of me the entire time. This repo already has `/backfill` and `/agent` as
HTML control pages, and a whole module (`backfill_page.py`) whose docstring
explains why they are built the way they are. I read `main.py`. I saw
`@app.get("/backfill", response_class=HTMLResponse)`. I built a JSON endpoint and
handed the user curl commands.

I asked exactly one clarifying question, and it was about *targeting semantics*
(customer-vs-plant), which was interesting to me. I never asked **"how will you
run this?"**, which was the question that decided whether the thing was usable at
all. I asked the question I wanted to answer instead of the one that mattered.

## Mistake 6 — production was my test loop

Six deploys. Each one: push, wait ~60s for Railway, poll `/health` for the
revision, re-test in the browser. The user watched me iterate in their production
environment.

My justification was that I had no SAJ credentials and my direct DB queries were
being blocked, so I could not exercise the real path locally. That is true and
still not an excuse — I could have asked for a way to test, or built a fixture
from the real names once I had them. Instead I made prod the REPL, which is both
slow and highly visible, and turned every one of my mistakes into a deployment.

## Mistake 7 — my tests moved to match my code

Twice I hit a failing test and *rewrote the test*:

- `test_a_loose_plant_match_syncs_but_refuses_to_write_a_link`
- `test_a_customer_never_loose_matches_its_way_onto_a_plant`

Both rewrites were defensible in isolation — the premise really had changed. But
rewriting a test to agree with new behaviour, twice, in one session, is a signal
that **the spec was unstable because I did not understand the domain**. I treated
each as a chore rather than as evidence I was guessing. The second rewrite was me
reverting to what the first rewrite had replaced.

---

## What was actually fine

Not everything was churn, and pretending otherwise would be its own distortion:

- **The debug/log layer paid for itself immediately.** The "32 unrelated plants"
  bug was diagnosable in one screenshot because the run log showed the fallback
  firing and what it matched. Without it that would have been an opaque 409.
- **The strict linking rule held through every revision.** Finding a plant got
  looser, then stricter, then correct — but *writing* a `customer ↔ plant` link
  stayed exact-match-only the whole time. No wrong links were ever written to
  prod, through six deploys and several wrong matching rules. Separating "what we
  search" from "what we commit" was the one early decision that contained the
  blast radius.
- **The core performance claim was real from the first commit** and never
  regressed: `saj_calls_before_readings: 0`, ~0.6s per customer.

---

## What I should have done

1. **Read 50 real `plant_name` and `customer.name` values before writing one line
   of matcher.** The task was name matching. Look at the names.
2. **Build the test fixtures from real strings.** `JClands capital SDN BHD
   (Restaurant) - SELCO` in `test_fast_sync.py` from the start kills v1 and v2 on
   the spot.
3. **Ask "how will you use this?" before "what should it mean?"** Interface first,
   semantics second. The UI question decided whether any of the rest mattered.
4. **When a bug appears, find the mechanism before choosing the fix.** "Substring
   over a space-stripped string" was the mechanism. "Be stricter" was a guess that
   happened to suppress the symptom.
5. **Treat a surprising number in my own output as a finding, not a footnote.**
   `could link: 0` was the whole answer, four hours early.

## The compressed version

I skipped the ten minutes of looking at real data that the entire task was about,
then spent six deploys discovering that data one complaint at a time.
