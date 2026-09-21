from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/'app.py').read_text(encoding='utf-8')
SECURITY=(ROOT/'core'/'security.py').read_text(encoding='utf-8')
BASE=(ROOT/'templates/admin_base.html').read_text(encoding='utf-8')
CTRL=(ROOT/'templates/admin_controls.html').read_text(encoding='utf-8')
SUB=(ROOT/'templates/school_subject_form.html').read_text(encoding='utf-8')
RES=(ROOT/'templates/student_assessment_result.html').read_text(encoding='utf-8')
SCHRES=(ROOT/'templates/school_results.html').read_text(encoding='utf-8')
SCHOOL_ROUTES=(ROOT/'blueprints'/'school'/'routes.py').read_text(encoding='utf-8')
STUDENT_PORTAL_HELPERS=(ROOT/'blueprints'/'student_portal'/'helpers.py').read_text(encoding='utf-8')
def test_scope_defaults_to_whole_area():
    assert "if not scopes: return True" in SECURITY and "if not typed: return True" in SECURITY
def test_notification_metadata():
    """Governance items must say who acted and when.

    The app records the actor on each notification, and the controls page shows
    an attributable name plus a timestamp for both notifications and messages.
    The Phase 13 redesign changed the markup from explicit "By:/Date:/Time:"
    labels to an inline layout, so the contract is asserted on the data shown
    rather than on the surrounding HTML.
    """
    assert "actor_display_name_snapshot" in SECURITY
    assert "created_at" in CTRL, "controls page must show when an item occurred"
    assert ("sender_name" in CTRL or "requester_name" in CTRL), \
        "controls page must attribute each item to a person"
def test_capability_filtered_school_nav():
    for perm in ('school.students.view','school.classes.view','school.subjects.view','school.assignments.view','school.tests.view','school.practice.view','school.examinations.view','school.results.view'):
        assert perm in BASE
def test_subject_empty_class_state():
    assert "No active classes are available in your current access area." in SUB
def test_result_release_governance():
    """A graded result is withheld from the student until it is released.

    Practice results are visible immediately. Everything else starts at the
    beginning of the approval workflow. Phase 13 replaced the single 'pending'
    status with entered -> verified -> approved -> released, so the entry point
    asserted here is 'entered'.
    """
    assert "result_status='released' if assessment_meta and assessment_meta['assessment_type']=='practice' else 'entered'" in STUDENT_PORTAL_HELPERS
    assert 'result_release_at' in "\n".join(p.read_text(encoding='utf-8') for p in sorted((ROOT/'models').glob('*.py')))
    assert "visible_to_student" in RES and "school.results.release" in SCHOOL_ROUTES and "result_release_at" in SCHRES

def test_result_workflow_stages_are_ordered():
    """Each stage may only be reached from the one before it."""
    assert "{'verify':('entered','verified'),'approve':('verified','approved'),'release':('approved','released')}" in SCHOOL_ROUTES
    for perm in ('school.results.verify','school.results.approve','school.results.release'):
        assert perm in SCHOOL_ROUTES
