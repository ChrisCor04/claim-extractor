from pathlib import Path

from estimate_extractor.adapters.allstate import AllstateAdapter
from estimate_extractor.adapters.farmers import FarmersAdapter
from estimate_extractor.adapters.generic import GenericXactimateStyleAdapter
from estimate_extractor.adapters.state_farm import StateFarmAdapter
from estimate_extractor.adapters.travelers import TravelersAdapter
from estimate_extractor.adapters.usaa import USAAAdapter
from estimate_extractor.classification.carrier import detect_carrier
from estimate_extractor.models.page import ParsedDocument, ParsedPage


def _doc(text: str) -> ParsedDocument:
    page = ParsedPage(page_number=1, width=612, height=792, raw_text=text)
    return ParsedDocument(source_path=Path("test.pdf"), sha256="deadbeef", page_count=1, pages=[page])


def _adapters():
    return {
        "state_farm": StateFarmAdapter(),
        "travelers": TravelersAdapter(),
        "usaa": USAAAdapter(),
        "farmers": FarmersAdapter(),
        "allstate": AllstateAdapter(),
        "generic": GenericXactimateStyleAdapter(),
    }


def test_detects_state_farm():
    doc = _doc("State Farm Claims\nstatefarmfireclaims@statefarm.com\nState Farm Insurance")
    match = detect_carrier(doc, _adapters(), threshold=0.70)
    assert match.key == "state_farm"


def test_detects_travelers():
    doc = _doc("travelers.com/claim\nThe Travelers Indemnity Company\nTHE STANDARD FIRE INSURANCE COMPANY")
    match = detect_carrier(doc, _adapters(), threshold=0.70)
    assert match.key == "travelers"


def test_detects_usaa():
    doc = _doc("USAA CASUALTY INSURANCE COMPANY\nclaims.usaa.com\nUSAA Confidential")
    match = detect_carrier(doc, _adapters(), threshold=0.70)
    assert match.key == "usaa"


def test_detects_farmers():
    doc = _doc("myclaim@farmersinsurance.com\nMid-Century Insurance Company of Texas\nwww.farmers.com/claimstatus")
    match = detect_carrier(doc, _adapters(), threshold=0.70)
    assert match.key == "farmers"


def test_detects_allstate():
    doc = _doc("claims.allstate.com\nAllstate Indemnity Company\nAllstate Property and Casualty Insurance Company")
    match = detect_carrier(doc, _adapters(), threshold=0.70)
    assert match.key == "allstate"


def test_falls_back_to_generic_when_no_carrier_matches():
    doc = _doc("Some totally unrelated document with no carrier keywords at all.")
    match = detect_carrier(doc, _adapters(), threshold=0.70)
    assert match.key == "generic"
