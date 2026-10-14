from ppbp.fhir.explore import LOINC_DIASTOLIC, LOINC_SYSTOLIC, bp_components, is_bp_panel


def _component(code: str, value: float) -> dict:
    return {"code": {"coding": [{"system": "http://loinc.org", "code": code}]}, "valueQuantity": {"value": value}}


def test_bp_components_are_looked_up_by_code_not_position() -> None:
    obs = {
        "resourceType": "Observation",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
        "component": [_component(LOINC_DIASTOLIC, 74), _component(LOINC_SYSTOLIC, 136)],
    }
    comps = bp_components(obs)
    assert is_bp_panel(obs)
    assert comps[LOINC_SYSTOLIC]["value"] == 136
    assert comps[LOINC_DIASTOLIC]["value"] == 74


def test_non_bp_observation_is_not_a_panel() -> None:
    obs = {"resourceType": "Observation", "code": {"coding": [{"code": "8302-2"}]}}
    assert not is_bp_panel(obs)
