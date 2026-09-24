import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from qudgym import MockBackend
from qudgym.eye.builds import BuildLibrary, BuildPreset, load_library
from qudgym.eye.cli import demo_records
from qudgym.eye.contracts import AgentView, Evidence, Fact, Frame, Hypothesis
from qudgym.eye.encoding import from_text, structured, text
from qudgym.eye.fixtures import ArenaFixture, CapabilityScorer, PRESETS
from qudgym.eye.legacy import from_observation
from qudgym.eye.memory import EvidenceMemory
from qudgym.eye.presenter import ObservationPresenter
from qudgym.eye.replay import Record, read_trace, render_html, write_trace


def step(game, action):
    return game.step(action, decision_id=game.observe().decision_id)


def roundtrip(frame):
    return Frame.model_validate_json(frame.model_dump_json())


def fact_of(frame, entity, attribute):
    return next(p for e in frame.entities if e.id == entity for p in e.facts if p.attribute == attribute)


@pytest.mark.parametrize('preset', PRESETS)
def test_fixtures_roundtrip_and_finish(preset):
    records = demo_records(preset)
    assert records[-1].view.current.phase == 'terminal'
    assert 'disable' not in records[-1].view.current.events[0].text
    assert 'struck' in records[-1].view.current.events[0].text
    for r in records:
        assert roundtrip(r.view.current) == r.view.current
        assert from_text(text(r.view)) == AgentView.model_validate(structured(r.view))


def test_null_is_unknown_not_zero():
    e = Evidence(status='unknown', channel='unknown', turn=0)
    assert Fact(attribute='hp', value=None, evidence=e).value is None
    with pytest.raises(ValidationError):
        Fact(attribute='hp', value=0, evidence=e)
    with pytest.raises(ValidationError):
        Fact(attribute='hp', value=None, evidence=Evidence(channel='vision', turn=0))
    assert Fact(attribute='hp', value=0, evidence=Evidence(channel='self', turn=0)).value == 0


@pytest.mark.parametrize('field,value', [('seed', 12), ('rng', [1, 2]), ('blueprint', 'secret'),
                                       ('snapshot', 'secret'), ('true_identity', 'secret')])
def test_contract_rejects_raw_privileged_fields(field, value):
    data = ArenaFixture().observe().model_dump(mode='json')
    data['entities'][0][field] = value
    with pytest.raises(ValidationError):
        Frame.model_validate(data)


@pytest.mark.parametrize('mutation', ['duplicate_entity', 'dangling_source', 'dangling_relation',
                                     'dangling_event', 'future_evidence', 'stale_observation',
                                     'outside_cell', 'duplicate_layer', 'duplicate_action',
                                     'missing_actor', 'terminal_actions', 'prompt_phase'])
def test_schema_rejects_inconsistent_frames(mutation):
    game = ArenaFixture()
    step(game, 'rest')
    d = game.observe().model_dump(mode='json')
    if mutation == 'duplicate_entity':
        d['entities'].append(d['entities'][0])
    elif mutation == 'dangling_source':
        d['actions'][0]['source'] = 'unseen'
    elif mutation == 'dangling_relation':
        d['relations'][0]['object'] = 'unseen'
    elif mutation == 'dangling_event':
        d['events'][0]['subjects'] = ['unseen']
    elif mutation in ('future_evidence', 'stale_observation'):
        d['entities'][0]['facts'][0]['evidence']['turn'] = 2 if mutation == 'future_evidence' else 0
    elif mutation == 'outside_cell':
        d['zones'][0]['cells'][0]['x'] = 999
    elif mutation == 'duplicate_layer':
        d['zones'][0]['cells'][0]['layers'].append(d['zones'][0]['cells'][0]['layers'][0])
    elif mutation == 'duplicate_action':
        d['actions'].append(d['actions'][0])
    elif mutation == 'missing_actor':
        d['controlled_actor'] = None
    elif mutation == 'terminal_actions':
        d['phase'] = 'terminal'
    elif mutation == 'prompt_phase':
        d['phase'] = 'prompt'
    with pytest.raises(ValidationError):
        Frame.model_validate(d)


def test_creation_does_not_require_a_world_position():
    f = Frame(episode_id='e', decision_id='d', turn=0, phase='creation', controlled_actor=None,
              actions=({'id':'choose', 'operation':'progress', 'label':'Choose a supplied option'},))
    assert f.zones == () and f.controlled_actor is None


def test_hidden_properties_do_not_change_any_policy_information():
    a = ArenaFixture('bow')
    b = copy.deepcopy(a)
    b._hidden_artifact_name = 'SECRET NEVER DISCLOSED'
    b._target_hp = 1234
    b._unobserved_secret = 'another hidden state'
    va, vb = EvidenceMemory().update(a.observe()), EvidenceMemory().update(b.observe())
    assert text(va) == text(vb)
    assert 'SECRET' not in text(vb)
    assert CapabilityScorer().score(va) == CapabilityScorer().score(vb)


def test_hidden_locations_do_not_leak_through_candidates_or_memory():
    a = ArenaFixture('blade')
    ma = EvidenceMemory()
    ma.update(a.observe())
    ma.update(step(a, 'back'))
    ma.update(step(a, 'back'))  # target no longer perceived
    b, mb = copy.deepcopy(a), copy.deepcopy(ma)
    b._target_x, b._target_y = 7, 3
    b._refresh_contact()
    va, vb = ma.update(step(a, 'rest')), mb.update(step(b, 'rest'))
    assert text(va) == text(vb)
    remembered_location = next(r for r in va.remembered if r.subject == 'c1' and r.attribute == 'location')
    assert remembered_location.fact.x == 5
    assert remembered_location.last_turn == 1
    assert not any(e.kind == 'contact' for e in va.current.entities)


def test_hearing_reveals_location_not_unknown_identity():
    game = ArenaFixture('listener')
    step(game, 'back')
    frame = step(game, 'back')
    contact = next(e for e in frame.entities if e.kind == 'contact')
    assert contact.location.evidence.channel == 'hearing'
    assert contact.facts[0].value is None
    assert contact.facts[0].evidence.status == 'unknown'


def test_lost_contact_does_not_reuse_hidden_engine_identity():
    game = ArenaFixture('blade')
    original = next(e.id for e in game.observe().entities if e.kind == 'contact')
    step(game, 'back')
    step(game, 'back')
    new = next(e.id for e in step(game, 'forward').entities if e.kind == 'contact')
    assert new != original


def test_unknown_now_preserves_past_evidence_in_separate_memory():
    game = ArenaFixture('listener')
    mem = EvidenceMemory()
    mem.update(game.observe())
    mem.update(step(game, 'back'))
    view = mem.update(step(game, 'back'))
    current = next(e for e in view.current.entities if e.kind == 'contact')
    assert current.facts[0].value is None
    old = next(r for r in view.remembered if r.subject == current.id and r.attribute == 'identity')
    assert old.fact.value == 'practice target'
    assert old.fact.evidence.channel == 'vision'
    assert old.last_turn < view.current.turn


def test_hypotheses_never_become_current_or_remembered_facts():
    g = ArenaFixture()
    m = EvidenceMemory()
    f = g.observe()
    m.update(f)
    h = Hypothesis(id='guess', text='A hypothetical hidden contact may exist.',
                   based_on_decisions=(f.decision_id,), model='untrained-diagnostic')
    v = m.update(f, hypotheses=(h,))
    assert v.current == f and v.hypotheses == (h,)
    assert 'hypothetical' not in f.model_dump_json()
    v2 = m.update(step(g, 'rest'))
    assert not v2.hypotheses
    assert all('hypothetical' not in r.model_dump_json() for r in v2.remembered)


def test_unknown_hypothesis_rejected_without_advancing_memory():
    g = ArenaFixture()
    m = EvidenceMemory()
    f = g.observe()
    h = Hypothesis(id='x', text='guess', based_on_decisions=('future',), model='test')
    with pytest.raises(ValueError, match='unknown decision'):
        m.update(f, hypotheses=(h,))
    assert m.update(f).current == f


def test_memory_repeat_reset_and_rewind():
    g = ArenaFixture()
    m = EvidenceMemory()
    root = g.observe()
    v = m.update(root)
    assert m.update(root) == v
    later = step(g, 'rest')
    m.update(later)
    with pytest.raises(ValueError, match='rewind'):
        m.update(root)
    with pytest.raises(ValueError, match='explicitly'):
        m.update(ArenaFixture().observe())
    m.reset()
    assert not m.update(root).remembered


def test_changed_payload_on_same_cursor_is_rejected():
    g = ArenaFixture()
    m = EvidenceMemory()
    m.update(g.observe())
    g.ammo = 1
    with pytest.raises(ValueError, match='reused decision'):
        m.update(g.observe())


def test_memory_limits_and_event_history():
    g = ArenaFixture()
    m = EvidenceMemory(max_records=3, max_events=1)
    m.update(g.observe())
    v = m.update(step(g, 'rest'))
    assert v.forgotten_records > 0 and len(v.remembered) <= 3
    assert v.forgotten_events == 1
    m = EvidenceMemory()
    m.update(g.observe())
    v = m.update(step(g, 'look'))
    assert v.remembered_events and v.remembered_events[0].event.text == 'No new event.'


@pytest.mark.parametrize('limit', [0, -1, True, 1.5])
def test_invalid_memory_limits(limit):
    with pytest.raises(ValueError):
        EvidenceMemory(max_records=limit)


def test_inventory_identity_changes_only_after_inspection():
    g = ArenaFixture()
    assert fact_of(g.observe(), 'artifact', 'identity').value is None
    prompt = step(g, 'look')
    assert prompt.phase == 'prompt' and prompt.turn == 0
    frame = step(g, 'yes')
    assert frame.turn == 0 and frame.phase == 'command'
    assert fact_of(frame, 'artifact', 'identity').evidence.channel == 'inspection'
    assert fact_of(frame, 'artifact', 'identity').value == g._hidden_artifact_name


def test_prompts_do_not_tick_resources_or_cooldowns():
    g = ArenaFixture('bow')
    f = step(g, 'use')
    assert f.turn == 1 and g.cooldown == 2 and g.ammo == 2
    for action in ('look', 'no'):
        f = step(g, action)
        assert f.turn == 1 and g.cooldown == 2 and g.ammo == 2
    step(g, 'rest')
    assert g.turn == 2 and g.cooldown == 1


def test_stale_or_invalid_actions_do_not_advance_fixture():
    g = ArenaFixture()
    before = g.observe()
    with pytest.raises(ValueError, match='stale'):
        g.step('use', decision_id='wrong')
    with pytest.raises(ValueError, match='invalid'):
        g.step('not-authorized', decision_id=before.decision_id)
    assert g.observe() == before


def test_different_capabilities_not_preset_labels_change_choice():
    scorer = CapabilityScorer()
    choices = {}
    for preset in ('blade', 'bow'):
        view = EvidenceMemory().update(ArenaFixture(preset).observe())
        scores = scorer.score(view)
        choice = max(scores, key=scores.get)
        choices[preset] = next(a.operation for a in view.current.actions if a.id == choice)
    assert choices == {'blade': 'move', 'bow': 'fire'}
    g = ArenaFixture('bow')
    g.ammo = 0
    view = EvidenceMemory().update(g.observe())
    scores = scorer.score(view)
    assert max(scores, key=scores.get) != 'use'


def test_entity_and_candidate_permutations_have_same_encoding_and_scores():
    g = ArenaFixture()
    f = g.observe()
    d = f.model_dump(mode='json')
    d['entities'].reverse()
    d['actions'].reverse()
    for e in d['entities']:
        e['facts'].reverse()
    permuted = Frame.model_validate(d)
    a, b = EvidenceMemory().update(f), EvidenceMemory().update(permuted)
    assert text(a) == text(b)
    assert CapabilityScorer().score(a) == CapabilityScorer().score(b)


def test_opaque_handle_renaming_preserves_scores():
    f = ArenaFixture().observe()
    d = f.model_dump(mode='json')
    refs = {e.id:f'object{n}' for n, e in enumerate(reversed(f.entities))}
    acts = {a.id:f'candidate{n}' for n, a in enumerate(reversed(f.actions))}
    d['controlled_actor'] = refs[d['controlled_actor']]
    for e in d['entities']:
        e['id'] = refs[e['id']]
        for p in e['facts']:
            if p['attribute'] == 'name':
                p['value'] = 'irrelevant display name'
    for r in d['relations']:
        r['subject'], r['object'] = refs[r['subject']], refs[r['object']]
    for a in d['actions']:
        a['id'] = acts[a['id']]
        a['label'] = 'irrelevant action label'
        for key in ('source', 'target'):
            if a[key] is not None:
                a[key] = refs[a[key]]
    a = CapabilityScorer().score(EvidenceMemory().update(f))
    b = CapabilityScorer().score(EvidenceMemory().update(Frame.model_validate(d)))
    assert {acts[k]:v for k, v in a.items()} == b


def test_legacy_conversion_does_not_invent_capabilities():
    b = MockBackend()
    o = b.reset().observation
    f = from_observation(o)
    assert len(f.entities) == 1
    assert {p.attribute for p in f.entities[0].facts} == {'hp','max_hp'}
    assert len(f.zones[0].cells) == sum(c != '?' for row in o.tiles for c in row)
    assert {a.id for a in f.actions} == {a.id for a in o.actions}


def test_nemo_presenters_are_isolated_idempotent_and_default_unchanged():
    a, b = MockBackend(), MockBackend()
    oa, ob = a.reset(seed=1).observation, b.reset(seed=2).observation
    assert ObservationPresenter().render(oa) == oa.model_dump_json()
    pa, pb = ObservationPresenter('agent-eye-v1'), ObservationPresenter('agent-eye-v1')
    sa = pa.render(oa)
    assert pa.render(oa) == sa
    sb = pb.render(ob)
    assert from_text(sa).current.episode_id != from_text(sb).current.episode_id
    assert not from_text(sa).remembered and not from_text(sb).remembered
    assert from_text(sa).current.decision_id == oa.decision_id


def test_trace_roundtrip_no_overwrite_and_injection_safe(tmp_path):
    records = demo_records('bow')
    raw = records[0].model_dump(mode='json')
    attack = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'
    raw['view']['current']['events'][0]['text'] = attack
    records[0] = Record.model_validate(raw)
    path = tmp_path/'trace.jsonl'
    write_trace(records, path)
    assert read_trace(path) == records
    with pytest.raises(FileExistsError):
        write_trace(records, path)
    html = tmp_path/'replay.html'
    render_html(records, html)
    rendered = html.read_text()
    assert attack not in rendered and '\\u003c/script\\u003e' in rendered
    assert '.innerHTML' not in rendered
    assert 'connect-src \'none\'' in rendered
    # The embedded data is exactly the recorded policy view, not an oracle summary.
    payload = rendered.split('<script id="trace" type="application/json">')[1].split('</script>')[0]
    assert json.loads(payload)[0]['view'] == raw['view']
    with pytest.raises(FileExistsError):
        render_html(records, html)


@pytest.mark.parametrize('bad', ['duplicate_decision', 'mixed_episode', 'bad_action', 'unsupported_version'])
def test_trace_rejects_bad_history_or_actions(tmp_path, bad):
    records = [r.model_dump(mode='json') for r in demo_records('bow')]
    if bad == 'duplicate_decision':
        records[1]['view']['current']['decision_id'] = records[0]['view']['current']['decision_id']
    elif bad == 'mixed_episode':
        records[1]['view']['current']['episode_id'] = 'another'
    elif bad == 'bad_action':
        records[0]['selected_action'] = 'unknown'
    else:
        records[0]['record_version'] = 'future'
    path = tmp_path/'bad.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in records))
    with pytest.raises(ValueError):
        read_trace(path)


def test_presets_are_user_authored_metadata_not_automatic_character_creation(tmp_path):
    preset = BuildPreset(id='owner-build', revision='1', game_build='owner-reported',
                         mods=('mod-a','mod-b'), creation_code='OWNER-SUPPLIED-CODE')
    library = BuildLibrary(presets=(preset,))
    p = tmp_path/'builds.json'
    p.write_text(library.model_dump_json())
    assert load_library(p).by_id('owner-build') == preset
    with pytest.raises(ValueError):
        library.by_id('missing')
    with pytest.raises(ValidationError):
        BuildLibrary(presets=(preset,preset))
    with pytest.raises(ValidationError):
        BuildPreset(id='x', revision='1', game_build='x', creation_code=' ')


def test_cli_demo_replay_build_validation_and_schema(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, '-m', 'qudgym.cli', *args], check=True, capture_output=True, text=True)
    trace = tmp_path/'demo.jsonl'
    html = tmp_path/'demo.html'
    result = run('eye-demo','--output',str(trace),'--preset','listener')
    assert json.loads(result.stdout)['is_mock'] is True
    run('eye-replay',str(trace),'--output',str(html))
    assert html.exists()
    assert json.loads(run('eye-builds','builds/library.json').stdout)['game_legality_checked'] is False
    run('eye-schema','--output',str(tmp_path/'schemas'))
    for cls in (AgentView, BuildLibrary):
        schema = json.loads((tmp_path/'schemas'/f'{cls.__name__}.schema.json').read_text())
        assert schema == cls.model_json_schema()


def test_legacy_preserves_known_movement_arguments():
    b = MockBackend()
    o = b.reset().observation
    f = from_observation(o)
    for old, new in zip(o.actions, f.actions):
        if old.kind == 'move':
            assert new.destination.x == o.player.x + old.arguments['dx']
            assert new.destination.y == o.player.y + old.arguments['dy']
    d = o.model_dump(mode='json')
    d['actions'][0]['arguments']['new_unmapped_field'] = 'do not silently discard'
    with pytest.raises(ValueError, match='lossless mapping'):
        from_observation(type(o).model_validate(d))


def test_past_hypothesis_outside_retention_does_not_mutate_memory():
    g = ArenaFixture()
    m = EvidenceMemory(max_decisions=1)
    root = g.observe()
    m.update(root)
    next_frame = step(g,'rest')
    h = Hypothesis(id='old', text='test', based_on_decisions=(root.decision_id,), model='test')
    with pytest.raises(ValueError):
        m.update(next_frame, hypotheses=(h,))
    assert m.update(root).current == root
    assert m.update(next_frame).current == next_frame
