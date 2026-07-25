# HATUA — demo video script

**Hard limit: 5 minutes.** This runs to about 4:30, leaving room to breathe.

The judges are ICPAC's Early Warning Systems Programme Manager, ICPAC's lead
developer, a GIS developer, and the team from Bunifu Technologies who built
HUSIKA. They know this domain better than we do. So the script does two things
deliberately: it **shows the system failing safely**, and it **defers to their
work** rather than claiming to replace it.

Do not oversell. These people have watched a lot of demos.

---

## Before you record

- [ ] `python scripts/build_snapshot.py 5` — fresh advisories, good model quota
- [ ] Dashboard open at the deployed URL, not localhost
- [ ] Telegram open on your **phone**, showing @Hatua_bot
- [ ] Zettatel SMS ready to fire to your own handset (`DRY_RUN=false`)
- [ ] Africa's Talking simulator open in a tab
- [ ] Close every other tab. No notifications.
- [ ] Record at 1080p minimum. Zoom the browser to ~125% so text is legible.

**Record your phone screen separately if you can** — a real SMS arriving on a
real handset is worth more than any dashboard.

---

## 0:00–0:35 — The problem, stated once

> *[Dashboard on screen, GDACS drought alert visible]*

"The Greater Horn of Africa does not have a forecasting problem.

ICPAC, the national met agencies, FEWS NET — they already produce good science.
Right now there's an active GDACS Orange drought alert over Ethiopia, Kenya and
Somalia. The forecast exists.

What fails is the last mile.

A forecast says *SPI-3 of minus one point four*. A pastoralist in Marsabit needs
*the next rains will fail — sell the weak animals now, before everyone else does
and the price collapses*.

And 68% of Ethiopian women own a phone, but only 9% use mobile internet daily.
That gap is the people most exposed to climate shocks — and they will never open
a dashboard.

This is HATUA. Swahili for *action*."

---

## 0:35–1:20 — Compound risk, and why the ranking is the point

> *[Scroll to the ranked district table]*

"Every district in the region, scored on live data.

But look at the ranking, because this is the whole argument.

Lower Juba, Somalia. Its drought signal is only *moderate* — 0.65. Yet it ranks
near the top. Why?"

> *[Click Lower Juba — the derivation panel fills]*

"Because the score isn't just the hazard. IPC Phase 4. Eighty-seven conflict
events in ninety days. Sixty-four thousand displaced people. Those multiply the
risk by 2.6.

And now look at Kampala. It has a genuine flood signal — and it sits in the
bottom half, because it has no underlying vulnerability.

A single-hazard system would have got both of those backwards. In this region,
drought and conflict and displacement aren't separate crises. They're one
crisis.

And notice — this whole derivation is arithmetic. There's no model in this
layer. If an ICPAC analyst disagrees with this warning, they can open one file
and argue with a specific number. You can't do that with an embedding."

---

## 1:20–2:30 — The Verifier, including a failure

> *[Advisory queue — click the "Blocked" tab]*

"Now the part we care most about.

HATUA sends messages that ask people to make irreversible decisions. Sell the
breeding stock. Move the family. Harvest early.

So before anything is sent, it goes through six checks. Five are deterministic
and run first — because a check that depends on a model can't be trusted to
police a model. The language model only judges the sixth, and its vote can
*block*, never *rescue*.

Here's a real one from our first live run."

> *[Point at a blocked advisory]*

"The model appended *avoid conflict areas and protect your livestock* to this
message. It sounds like reasonable humanitarian advice. It appears in no
approved action. Nobody authorised it.

Blocked.

That's not a slide claiming we thought about safety. That's the log.

And we'll be honest about the other direction too — early on, the Verifier
blocked a message that was actually *correct*, because we hadn't shown it the
approved action deadlines. Over-blocking is the right way to fail for a system
like this. But it's a real cost and we're not going to pretend it isn't."

> *[Switch to the "Verified" tab]*

"Here's what passes. Every figure traceable to a source reading. Every action
from a published IGAD, FAO or WFP protocol — the model *selects* actions, it
never writes them. Below high confidence it only ever sees no-regret actions, so
a forty-five-percent-confidence signal can't tell a family to sell its breeding
stock."

---

## 2:30–3:30 — The last mile, on real devices

> *[Phone on screen, Telegram]*

"Delivery. Telegram first — free, and genuinely the right channel for Ethiopia,
where it's the dominant platform."

> *[Advisory arrives live]*

"With its provenance attached. Confidence, verification score, and the actual
sources. A county officer forwarding this can see what it rests on."

> *[Africa's Talking simulator — dial the USSD code]*

"USSD. This is how you reach a feature phone with no credit and no data bundle.

Four languages. Pick a district. Read the current alert."

> *[Navigate to the alert]*

"That took 0.05 milliseconds to render, against a three-second timeout — because
nothing on this path touches a model or the network. Every advisory here is
pre-computed. That constraint is why the whole system generates on a schedule
rather than on demand.

And notice — the menu is Latin script only. Ethiopic and Arabic in USSD get
rendered by the handset's dialer firmware, and on cheap phones the font often
isn't there. Amharic speakers get their own script by SMS instead, where it
renders properly."

> *[Phone — real SMS arrives]*

"And a real SMS, on a real Safaricom handset."

---

## 3:30–4:10 — The engineering nobody else did

> *[Show the encoding comparison — terminal or slide]*

"One detail that decides whether this is affordable.

Amharic can never be GSM-7. There's no Ge'ez shift table in the 3GPP standard
and there never will be. So Amharic gets seventy characters per segment;
Swahili gets a hundred and sixty.

The same three-hundred-character advisory is two segments in Swahili and five in
Amharic. Two and a half times the cost for identical content.

So we write the Amharic template first and back-translate. Design for Swahili
first and you build something you can't afford to send in Ethiopia.

There's also this — "

> *[Show the apostrophe demo]*

"A typographic apostrophe. The character every word processor and every language
model produces by default. It's not in GSM-7, so one of them silently converts a
160-character Somali message into a 70-character one and doubles your bill.
Twilio auto-corrects this. Africa's Talking doesn't. So we do."

> *[Show the cost table]*

"Reaching a hundred thousand people costs about twenty thousand shillings
through a Kenyan aggregator. The same messages through Twilio: four million.
That two-hundred-fold gap is the reason this is viable at national scale."

---

## 4:10–4:30 — Close, deferring to ICPAC

> *[Dashboard, wide]*

"Two last things.

We built this on ICPAC's own APIs — Hazards Watch, Drought Watch, the Triggers
and Thresholds framework — plus the CAP feeds from six national met agencies.
And HATUA emits CAP 1.2 itself, so it plugs into HUSIKA and into the cell
broadcast system Kenya's Communications Authority is rolling out.

Because HATUA isn't a replacement for HUSIKA. HUSIKA is the pipe. HATUA decides
what should go down it — and proves it's true first.

And where we don't have data, we say so. Eritrea has no IPC classification, no
CAP feed, and a 2001 census. It shows *no data*. Not a number we made up.

For a system people act on, that matters more than coverage."

> *[End card: HATUA — from warning to action. GitHub URL, live URL.]*

---

## If you're running short on time

Cut in this order:

1. The apostrophe detail (3:50–4:00) — lovely, but the Amharic point already
   lands
2. The Kampala contrast (1:05–1:15) — Lower Juba alone makes the argument
3. The false-positive admission (2:15–2:30) — **cut this last.** It is unusual,
   it is honest, and this particular panel will respect it.

**Never cut:** the blocked advisory, the real SMS, or the deference to HUSIKA.

---

## Tone notes

- Speak slower than feels natural. Non-native English speakers are on the panel.
- Do not say "revolutionary", "game-changing", or "AI-powered".
- Let the failures land. Do not apologise for them.
- When you say the numbers, say them precisely. This audience checks.
