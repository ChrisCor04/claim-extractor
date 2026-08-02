from estimate_extractor.classification.pages import classify_page
from estimate_extractor.models.page import PageClassification, ParsedPage


def _page(text: str, number: int = 1) -> ParsedPage:
    return ParsedPage(page_number=number, width=612, height=792, raw_text=text)


def test_instructional_sample_page_excluded_via_placeholder_marker():
    text = (
        "Building Estimate Summary Guide\n"
        "This summary guide is based on a sample estimate and is provided for reference only.\n"
        "State Farm Insurance\n"
        "Insured: Smith, Joe & Jane\n"
        "Property: 1 Main Street\n"
        "Anywhere, IL 00000-0000\n"
        "Claim number:  00-0000-000\n"
        "Policy Number:  00-00-0000-0\n"
    )
    record = classify_page(_page(text))
    assert record.classification == PageClassification.INSTRUCTIONAL_SAMPLE
    assert record.include_in_estimate is False


def test_travelers_guide_page_excluded_despite_looking_like_real_estimate_detail():
    # This mirrors the Wei Tang fixture: a full fake line-item table
    # ("GUIDE_EXAMPLE") embedded inside instructional content -- it must
    # not be classified as real estimate_detail just because it has a
    # QUANTITY column and numbered rows.
    text = (
        "YOUR ESTIMATE COVER SHEET\n"
        "Guide to Understanding Your Property Estimate\n"
        "Claim Number: ABC1234001H\n"
        "YOUR ESTIMATE DETAIL\n"
        "GUIDE_EXAMPLE\n"
        "QUANTITY UNIT TAX RCV AGE/LIFE COND. DEP % DEPREC. ACV\n"
        "1. R&R 1/2 drywall - hung, taped, floated, ready for paint\n"
        "32.00 SF\n"
    )
    record = classify_page(_page(text))
    assert record.classification == PageClassification.INSTRUCTIONAL_SAMPLE
    assert record.include_in_estimate is False


def test_real_estimate_detail_page_included():
    text = (
        "State Farm Claims\nARANDA, GENARO\nDwelling\nExterior\n"
        "QUANTITY\nUNIT PRICE\nTAX\nRCV\nAGE/LIFE\nDEPREC.\nACV\nCONDITION\nDEP %\n"
        '* 1.  R&R Gutter - aluminum - up to 5"\n200.00 LF\n9.80\n66.83\n2,026.83\n'
        "2/25 yrs\n(162.15)\n1,864.68\nAvg.\n8.00%\n"
    )
    record = classify_page(_page(text))
    assert record.classification == PageClassification.ESTIMATE_DETAIL
    assert record.include_in_estimate is True


def test_continuation_page_detected():
    text = (
        "CONTINUED - Dwelling Roof\n"
        "QUANTITY\nUNIT PRICE\nRCV\nACV\nTAX\nAGE/LIFE\nDEPREC.\nCONDITION\nDEP %\n"
        "7.  Hip / Ridge cap - Standard profile - composition shingles\n"
    )
    record = classify_page(_page(text))
    assert record.classification == PageClassification.ESTIMATE_DETAIL_CONTINUATION


def test_replacement_cost_explanation_page():
    text = (
        "Explanation of Building Replacement Cost Benefits\n"
        "Homeowner Policy\n"
        "Your insurance policy provides replacement cost benefits for some or all of the loss...\n"
        "Replacement cost benefits pays the actual and necessary cost of repair or replacement...\n"
        "Claim Number:\n4399W552P\n"
    )
    record = classify_page(_page(text))
    assert record.classification == PageClassification.REPLACEMENT_COST_EXPLANATION


def test_blank_page():
    record = classify_page(_page("   \n  \n"))
    assert record.classification == PageClassification.BLANK
    assert record.include_in_estimate is False
