"""Shared pytest fixtures.

The eight sample Performance Evaluations under ``tests/fixtures/samples`` were downloaded
from the agencies on 2026-10-05 and are US government works. Each has a ``.pdf`` and the
``pdftotext -layout`` output as ``.txt``. Index snapshots from the three agency sources
live under ``tests/fixtures/index``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES = FIXTURES / "samples"
INDEX_FIXTURES = FIXTURES / "index"

# filename stem -> (regulator, bank_id, exam_date, method, overall_rating)
SAMPLE_META = {
    "OCC_703395_GNBank_2023_NI": ("OCC", "703395", "2023-03-27", "small", "NI"),
    "OCC_11715_LemontNB_2024_SN": ("OCC", "11715", "2024-04-08", "small", "SN"),
    "OCC_13682_FirstNeighborBank_2024_ISB_O": ("OCC", "13682", "2024-08-19", "isb", "O"),
    "FDIC_12299_SouthStar_2022_ISB_NI_fairlending": ("FDIC", "12299", "2022-09-12", "isb", "NI"),
    "FDIC_26816_LibertyBank_2022_SM_SN": ("FDIC", "26816", "2022-08-09", "small", "SN"),
    "FDIC_6078_OneAmericanBank_2024_ISB_NI": ("FDIC", "6078", "2024-08-05", "isb", "NI"),
    "FRB_3664588_FieldPoint_2024_ISB_NI": ("FRB", "3664588", "2024-01-16", "isb", "NI"),
    "FRB_663656_SmallBusinessBank_2024_SM_NI": ("FRB", "663656", "2024-11-18", "small", "NI"),
}


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    return SAMPLES


@pytest.fixture(scope="session")
def index_fixtures_dir() -> Path:
    return INDEX_FIXTURES


@pytest.fixture(params=sorted(SAMPLE_META))
def sample(request) -> dict:
    stem = request.param
    regulator, bank_id, exam_date, method, rating = SAMPLE_META[stem]
    return {
        "stem": stem,
        "pdf": SAMPLES / f"{stem}.pdf",
        "txt": SAMPLES / f"{stem}.txt",
        "regulator": regulator,
        "bank_id": bank_id,
        "exam_date": exam_date,
        "method": method,
        "overall_rating": rating,
    }
