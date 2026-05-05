from src.score.base import PatternScore, PatternScorer
from src.score.pattern_a_dilution import PatternA
from src.score.pattern_b_index import PatternB
from src.score.pattern_c_contract import PatternC
from src.score.pattern_d_squeeze import PatternD
from src.score.pattern_e_brand_penny import PatternE
from src.score.pattern_f_megatheme import PatternF

ALL_SCORERS: list[type[PatternScorer]] = [
    PatternA,
    PatternB,
    PatternC,
    PatternD,
    PatternE,
    PatternF,
]

__all__ = [
    "PatternScore",
    "PatternScorer",
    "PatternA",
    "PatternB",
    "PatternC",
    "PatternD",
    "PatternE",
    "PatternF",
    "ALL_SCORERS",
]
