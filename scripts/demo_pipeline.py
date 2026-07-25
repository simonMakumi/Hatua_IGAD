"""Run the full pipeline on real districts and print every advisory,
passed and blocked. This is the script the demo video is recorded from."""
import sys, asyncio, logging, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')

from hatua.pipeline import run
from hatua.models import AdminUnit, Vulnerability, Exposure, Language, Channel
from hatua.agents.verifier import summarise

UNITS = {  # one district keeps us inside the free-tier RPM limit
    'SO24':  AdminUnit(pcode='SO24', name='Lower Juba', country_iso3='SOM',
                       centroid_lat=0.35, centroid_lon=42.05,
                       population=489307, population_year=2014),
}
CTX = {
    'SO24':  Vulnerability(ipc_phase=4, conflict_events_90d=87,
                           fatalities_90d=140, idps=64000,
                           ipc_reference_period='2026-04'),
    'KE039': Vulnerability(ipc_phase=3, conflict_events_90d=12),
}
EXP = {
    'SO24':  Exposure(population=489307, population_year=2014, rangeland_fraction=0.85),
    'KE039': Exposure(population=926976, population_year=2019, rangeland_fraction=0.90),
}

async def main():
    results = await run(UNITS, context=CTX, exposures=EXP,
                        languages=[Language.SOMALI, Language.ENGLISH],
                        channels=[Channel.SMS])
    for r in results:
        a = r.assessment
        print('=' * 78)
        print(f'{r.unit.name} ({r.unit.country_iso3})  '
              f'CRS={a.compound_risk_score:.3f}  conf={a.confidence_score:.3f}')
        print(f'triggers: {[t.threshold_name for t in a.triggers]}')
        if r.errors:
            print('ERRORS:', r.errors[:3])
        for ad in r.advisories:
            print(f'\n  --- {ad.language.value}/{ad.channel.value}  '
                  f'sev={ad.severity.value}  {ad.encoding} '
                  f'{ad.segment_count}seg  {len(ad.body)}chars ---')
            print(f'  {ad.body}')
            print(f'  {summarise(ad.verification)}')
            for c in ad.verification.checks:
                print(f'     {"OK  " if c.passed else "FAIL"} {c.name}: {c.detail[:120]}')
            print(f'  actions: {ad.action_ids}')

asyncio.run(main())
