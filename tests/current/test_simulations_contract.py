"""Contract: learning simulations (core/simulations.py, core/simulations_catalogue.py).

* every catalogue row is well formed: a unique PhET name, a known topic, classes from Primary 1 to SSS 3 in order,
  and only real SSS departments;
* a student sees only their class's simulations, and in SSS only their department's (all of them when their
  department is not set); nothing when the school or their class has them switched off;
* the school's saved choices are cleaned, so a bad value never reaches a student;
* the pages that embed a simulation let in phet.colorado.edu, keep the nonce, and never load in place;
* only the School Admin or a head teacher may change the settings.

The rules are checked with the database lookups replaced, so this needs no database.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core import simulations as sims  # noqa: E402
from core.simulations_catalogue import CATALOGUE, TOPICS  # noqa: E402

DEPARTMENTS = ('Science', 'Art', 'Commercial')


def _with(levels, saved=None):
    """Replace the database lookups: ``levels`` is {class_id: level_order}; ``saved`` the school's stored choices."""
    sims.class_level = lambda class_id: levels.get(int(class_id)) if class_id else None
    import blueprints.finance.helpers as helpers
    helpers.json_setting = lambda key, default: dict(default, **(saved or {}))


def test_every_catalogue_row_is_well_formed():
    seen = set()
    for row in CATALOGUE:
        slug, title, topic, lo, hi, departments = row
        assert slug not in seen, f'{slug} is listed twice'
        seen.add(slug)
        assert slug == slug.lower() and ' ' not in slug, slug
        assert title.strip(), slug
        assert topic in TOPICS, f'{slug}: unknown topic {topic}'
        assert 1 <= lo <= hi <= 12, f'{slug}: classes {lo}-{hi}'
        assert departments and set(departments) <= set(DEPARTMENTS), f'{slug}: departments {departments}'
    assert len(CATALOGUE) >= 80
    # Every class from Primary 1 to SSS 3 has something to open.
    for level in range(1, 13):
        assert any(lo <= level <= hi for _s, _t, _p, lo, hi, _d in CATALOGUE), f'nothing for level {level}'


def test_a_student_sees_only_their_class():
    _with({1: 1, 7: 7})
    p1 = {e['slug'] for e in sims.for_student({'class_id': 1, 'department': None})}
    jss1 = {e['slug'] for e in sims.for_student({'class_id': 7, 'department': None})}
    assert 'make-a-ten' in p1 and 'make-a-ten' not in jss1
    assert 'projectile-motion' in jss1 and 'projectile-motion' not in p1
    assert 'calculus-grapher' not in jss1


def test_an_sss_student_sees_only_their_department():
    _with({12: 12})
    science = {e['slug'] for e in sims.for_student({'class_id': 12, 'department': 'Science'})}
    art = {e['slug'] for e in sims.for_student({'class_id': 12, 'department': 'Art'})}
    commercial = {e['slug'] for e in sims.for_student({'class_id': 12, 'department': 'Commercial'})}
    unset = {e['slug'] for e in sims.for_student({'class_id': 12, 'department': None})}
    assert 'molarity' in science and 'molarity' not in art and 'molarity' not in commercial
    assert 'neuron' in art and 'neuron' not in commercial
    assert 'trig-tour' in commercial and 'trig-tour' in art
    assert unset == science | art | commercial


def test_a_junior_department_is_ignored():
    _with({8: 8})
    assert sims.for_student({'class_id': 8, 'department': 'Art'}) == sims.for_student({'class_id': 8, 'department': None})


def test_switched_off_shows_nothing():
    _with({5: 5}, {'enabled': False})
    assert sims.for_student({'class_id': 5}) == []
    _with({5: 5, 6: 6}, {'classes_off': [5]})
    assert sims.for_student({'class_id': 5}) == []
    assert sims.for_student({'class_id': 6})
    _with({-1: None})
    assert sims.for_student({'class_id': -1}) == []      # Nursery and Crèche have no simulations
    assert sims.for_student({'class_id': None}) == []


def test_on_by_default():
    _with({})
    assert sims.settings()['enabled'] is True


def test_the_school_s_choices_apply_and_bad_values_are_dropped():
    _with({3: 3, 12: 12}, {'hidden': ['area-builder', 'not-a-sim'], 'ranges': {'make-a-ten': [3, 3], 'friction': [9, 2],
                           'bogus': [1, 2]}, 'departments': {'trig-tour': ['Science', 'Nonsense']}, 'classes_off': ['x', 4]})
    s = sims.settings()
    assert s['hidden'] == ['area-builder'] and s['classes_off'] == [4]
    assert s['ranges'] == {'make-a-ten': [3, 3]}
    assert s['departments'] == {'trig-tour': ['Science']}
    p3 = {e['slug'] for e in sims.for_student({'class_id': 3})}
    assert 'make-a-ten' in p3 and 'area-builder' not in p3
    assert 'trig-tour' not in {e['slug'] for e in sims.for_student({'class_id': 12, 'department': 'Art'})}


def test_the_embedding_pages_let_phet_in_and_keep_the_nonce():
    policy = sims.csp('abc123')
    assert "frame-src https://phet.colorado.edu" in policy
    assert "'nonce-abc123'" in policy and 'unsafe-inline' not in policy.split('script-src', 1)[1].split(';', 1)[0]
    assert "frame-ancestors 'none'" in policy
    source = (ROOT / 'blueprints' / 'student_portal' / 'simulations.py').read_text(encoding='utf-8')
    assert "sims.csp(csp_nonce())" in source


def test_simulation_pages_never_load_in_place():
    shell = (ROOT / 'blueprints' / 'portal_shell.py').read_text(encoding='utf-8')
    scope = shell[shell.index("'scope': ['=/student/dashboard'"):]
    scope = scope[:scope.index(']')]
    assert '/student/simulations' not in scope


def test_only_the_school_admin_or_a_head_teacher_may_change_the_settings():
    source = (ROOT / 'blueprints' / 'school' / 'simulations.py').read_text(encoding='utf-8')
    assert "is_school_admin(me) or holds_level(me['id'], 'head_teacher')" in source
    assert "if not _may_change(me):" in source
