import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOADOUTS = ROOT / 'loadouts'

# PointsPurchased is the amount bought above the genotype floor (10 mutant, 12 true kin).
# Points through 18 cost 1; each point past 18 costs 2. Calling and caste bonuses are not included.
MUTATION_COSTS = {
    'Chimera': 1,
    'Multiple Legs': 5,
    'Multiple Arms': 4,
    'Beak': 1,
    'Night Vision': 1,
}
CYBERNETIC_COSTS = {'NightVision': 2}


def attribute_cost(purchased, floor):
    stat = floor + purchased
    if stat <= 18:
        return purchased
    return (18 - floor) + 2 * (stat - 18)


def module(doc, suffix):
    found = [item for item in doc['modules'] if item['moduleType'].startswith('XRL.CharacterBuilds.Qud.' + suffix + ',')]
    assert len(found) == 1
    return found[0]['data']


def test_artifex_spends_the_true_kin_pools():
    doc = json.loads((LOADOUTS / 'artifex.json').read_text())
    assert module(doc, 'QudGenotypeModule')['Genotype'] == 'True Kin'
    assert module(doc, 'QudSubtypeModule')['Subtype'] == 'Artifex'
    attributes = module(doc, 'QudAttributesModule')
    spent = sum(attribute_cost(value, 12) for value in attributes['PointsPurchased'].values())
    assert spent == attributes['baseAp'] == 38
    assert attributes['apRemaining'] == 0
    assert attributes['apSpent'] == -38
    cyber = module(doc, 'QudCyberneticsModule')
    licenses = sum(CYBERNETIC_COSTS[row['Cybernetic']] * row['Count'] for row in cyber['selections'])
    assert licenses == 2
    assert 'QudMutationsModule' not in json.dumps(doc['modules'])


def test_marauder_spends_the_mutated_human_pools():
    doc = json.loads((LOADOUTS / 'marauder.json').read_text())
    assert module(doc, 'QudGenotypeModule')['Genotype'] == 'Mutated Human'
    assert module(doc, 'QudSubtypeModule')['Subtype'] == 'Marauder'
    attributes = module(doc, 'QudAttributesModule')
    spent = sum(attribute_cost(value, 10) for value in attributes['PointsPurchased'].values())
    assert spent == attributes['baseAp'] == 44
    assert attributes['apRemaining'] == 0
    assert attributes['apSpent'] == -44
    mutations = module(doc, 'QudMutationsModule')
    spent_mp = sum(MUTATION_COSTS[row['Mutation']] * row['Count'] for row in mutations['selections'])
    assert spent_mp == 12
    assert mutations['mp'] == 0
    assert 'QudCyberneticsModule' not in json.dumps(doc['modules'])
