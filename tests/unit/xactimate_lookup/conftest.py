from __future__ import annotations

import pytest

from estimate_extractor.xactimate_lookup import phrase_generator, ranking


@pytest.fixture
def phrase_rules():
    return phrase_generator.load_phrase_rules()


@pytest.fixture
def ranking_config():
    return ranking.load_ranking_config()
