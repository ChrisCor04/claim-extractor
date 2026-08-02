from estimate_extractor.pdf.tables import TokenKind, classify_line


def test_classify_item_start():
    tok = classify_line('* 1.  R&R Gutter - aluminum - up to 5"')
    assert tok.kind == TokenKind.ITEM_START
    assert tok.item_number == 1
    assert tok.has_allowance_marker is True
    assert tok.description == 'R&R Gutter - aluminum - up to 5"'


def test_classify_qty_unit():
    tok = classify_line("33.66 SQ")
    assert tok.kind == TokenKind.QTY_UNIT
    assert tok.quantity_raw == "33.66"
    assert tok.unit_raw == "SQ"


def test_classify_measurement_line_also_qty_unit_shape():
    # Measurement lines share the "<number> <words>" shape with real
    # quantities; disambiguation (is_real_quantity_unit) happens one layer
    # up in parsing/sections.py, not in the tokenizer itself.
    tok = classify_line("3,366.04 Surface Area")
    assert tok.kind == TokenKind.QTY_UNIT
    assert tok.unit_raw == "Surface Area"


def test_classify_age_life():
    assert classify_line("2/25 yrs").kind == TokenKind.AGE_LIFE
    assert classify_line("0/NA").kind == TokenKind.AGE_LIFE


def test_classify_age_life_condition_combo():
    tok = classify_line("7/30 yrs Avg.")
    assert tok.kind == TokenKind.AGE_LIFE_CONDITION_COMBO
    assert tok.age_life_raw == "7/30 yrs"
    assert tok.condition_raw == "Avg."


def test_classify_dep_percent():
    assert classify_line("8.00%").kind == TokenKind.DEP_PERCENT
    assert classify_line("NA").kind == TokenKind.DEP_PERCENT


def test_classify_money_variants():
    assert classify_line("2,314.13").kind == TokenKind.MONEY_PLAIN
    assert classify_line("(162.15)").kind == TokenKind.MONEY_PAREN
    assert classify_line("<258.27>").kind == TokenKind.MONEY_ANGLE


def test_classify_totals_label():
    assert classify_line("Totals:  Dwelling Roof").kind == TokenKind.TOTALS_LABEL
    assert classify_line("Area Totals:  Exterior").kind == TokenKind.TOTALS_LABEL
    assert classify_line("Line Item Totals:  43-99W5-52P").kind == TokenKind.TOTALS_LABEL


def test_classify_header_keyword():
    assert classify_line("QUANTITY").kind == TokenKind.HEADER_KEYWORD
    assert classify_line("RCV").kind == TokenKind.HEADER_KEYWORD


def test_classify_blank():
    assert classify_line("   ").kind == TokenKind.BLANK
    assert classify_line("").kind == TokenKind.BLANK


def test_classify_generic_text_fallback():
    assert classify_line("Dwelling Roof").kind == TokenKind.TEXT
