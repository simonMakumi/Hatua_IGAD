# HATUA — Devpost submission copy

Copy-paste ready. Word counts verified against the 250-word limits.

---

## Project name

```
HATUA
```

## Elevator pitch (200 characters max)

```
Early warning already works. The last mile doesn't. HATUA turns forecasts into verified, actionable advisories in local languages, on any phone — and refuses to send what it cannot prove.
```
*(186 characters)*

---

## Project Overview — 248 words

```
The Greater Horn of Africa does not have a forecasting problem. ICPAC, the
national meteorological agencies, FEWS NET and GDACS already produce good
science. As we built this, an active GDACS Orange drought alert covered
Ethiopia, Kenya and Somalia. The information exists. It does not reach the
people who need it, in a form they can act on.

Three failures. First, translation: a forecast says "SPI-3 of −1.4"; a
pastoralist in Marsabit needs "the next rains will fail — sell the weak animals
now, before everyone else does and prices collapse." Second, channel: 68% of
Ethiopian women own a phone, but only 9% use mobile internet daily. That
59-point gap is the population most exposed to climate shocks and least
reachable by any dashboard. Third, compound risk: drought is monitored
separately from conflict, displacement and food insecurity, when in this region
they are one crisis.

HATUA closes that last mile. It ingests live data from ICPAC's own platforms,
six national CAP feeds, GloFAS, GDACS and HDX HAPI, scores compound risk at
district level, and generates verified advisories in seven languages over SMS,
USSD, voice, Telegram and a CAP 1.2 feed.

Beneficiaries are the people existing systems structurally miss: pastoralists,
smallholder farmers and displaced households on feature phones, plus the county
disaster officers and responders who must decide where to act first.

We do not replace ICPAC's HUSIKA dissemination platform. HUSIKA is the pipe.
HATUA decides what should go down it — and proves it is true first.
```

---

## Solution Details — 249 words

```
HATUA separates what must be reproducible from what must be fluent.

The fusion layer contains no model. It is auditable arithmetic: compound risk =
hazard × exposure × vulnerability × confidence, where vulnerability is driven by
IPC phase, conflict events and displacement. This is why Lower Juba, Somalia
ranks second regionally on a merely moderate drought signal — IPC Phase 4, 87
conflict events and 64,000 IDPs multiply it — while Kampala, with a real flood
signal but no underlying vulnerability, correctly sinks. Single-hazard systems
cannot see that.

Above it, four agents with typed contracts: an Impact Analyst reasoning about
consequences rather than meteorology, an Action Planner that selects from a
fixed library of published IGAD/FAO/WFP anticipatory actions (it never authors
advice — below high confidence only no-regret actions are even shown to it), a
Localizer, and a blocking Verifier.

The Verifier is the core contribution. Six checks; five deterministic and run
first, because a check depending on a model cannot police a model. The LLM
adjudicates only semantic faithfulness and can block but never rescue. It fails
closed. On our first live run it caught the model inventing "avoid conflict
areas and protect your livestock" — plausible advice authorised by nobody — and
blocked the message.

Delivery is encoding-aware. Amharic can never be GSM-7, so it is capped at 70
characters per segment against Swahili's 160; we write Amharic first and
back-translate. USSD answers in 0.05ms because nothing on that path touches a
model.

Stack: Python, FastAPI, Pydantic, MapLibre, provider-agnostic LLM layer.
```

---

## Built with (tags)

```
python, fastapi, pydantic, gemini, groq, maplibre, docker, render,
africas-talking, telegram-bot-api, zettatel, open-meteo, glofas, gdacs,
icpac, hdx-hapi, fews-net, climateserv, cap-1.2, geospatial, ussd, sms
```

---

## Try it out (links)

```
Live app:     https://hatua.onrender.com
Dashboard:    https://hatua.onrender.com/
USSD demo:    https://hatua.onrender.com/ussd/simulate?text=
CAP 1.2 feed: https://hatua.onrender.com/api/cap.xml
Telegram bot: https://t.me/Hatua_bot
GitHub:       https://github.com/simonMakumi/Hatua_IGAD
```

---

## Technology stack (for the Technical Information field)

```
BACKEND      Python 3.11, FastAPI, Pydantic v2, httpx (async), APScheduler
FRONTEND     Single-file dashboard, MapLibre GL (open source, no Mapbox token)
AI           Provider-agnostic: Gemini / Groq / Cerebras / OpenRouter / Anthropic
             with automatic per-model fallback
DELIVERY     Zettatel + Mobitech + HostPinnacle (SMS), Africa's Talking (USSD),
             Telegram Bot API, Azure Speech (TTS), CAP 1.2
DEPLOY       Docker, Render (free tier)

DATA SOURCES (all verified live, none mocked)
  ICPAC East Africa Hazards Watch    36 datasets, keyless JSON + WMS + vector tiles
  ICPAC Drought Watch                 SPI, CDI, fAPAR, soil moisture anomaly
  ICPAC Triggers & Thresholds         SPI/SPEI drought trigger layers
  National met agency CAP feeds       Kenya, Ethiopia, Somalia, Sudan,
                                      South Sudan, Djibouti
  Open-Meteo                          16-day forecast, 8 countries per request
  Open-Meteo Flood                    GloFAS v4 river discharge, 210-day horizon
  GDACS                               Live multi-hazard alerts with polygons
  HDX HAPI                            IPC, ACLED, IOM DTM, UNHCR, WorldPop, WFP
  FEWS NET FDW                        IPC phase with GeoJSON geometry
  ClimateSERV                         CHIRPS zonal statistics at admin-2
  WHO Disease Outbreak News           Outbreak surveillance
  USGS                                Earthquakes (the Afar rift is active)
  geoBoundaries / HDX COD-AB          P-coded admin boundaries
```

---

## Notes for the writeup — things worth saying out loud

These are the details that separate a submission from a demo. Use them in the
video narration or the long description.

**We built the refusal path first.** Blocked advisories are visible on the
dashboard, not hidden. A verification layer nobody can inspect is just a claim.

**We record our own false positive.** The Verifier initially blocked a *correct*
message because we had not shown it the approved action deadlines. Over-blocking
is the right failure direction for a life-safety system, but it is a real tuning
cost and pretending otherwise would be dishonest.

**We say what we cannot do.** SMS is an advisory channel, not a flash-flood
alarm — 100,000 subscribers takes ~2.8 hours at alphanumeric throughput. Cell
Broadcast is the right technology and Kenya's CA is rolling it out; we emit CAP
1.2 so we plug into it rather than needing a rewrite.

**We render data gaps as gaps.** Eritrea has no IPC data, no CAP feed and a 2001
census baseline. It shows "no data", not a fabricated score.

**We quote real costs.** Reaching 100,000 people costs KES 20,000 (~US$155) via
Kenyan aggregation and KES 4,040,000 via Twilio. That 200× gap is why this is
affordable at national scale.

**We defer to ICPAC.** HUSIKA is their dissemination platform and we designed a
pluggable adapter for it rather than competing with it.
