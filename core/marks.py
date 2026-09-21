"""Marks are not always whole numbers.

A 40-question entrance paper is 100 marks, so each question is worth 2.5. Scores are therefore
kept as decimals. Two small helpers keep that from showing: a mark that is a whole number
reads as one ("100", not "100.0"), and the sum of many decimals has its stray binary rounding
noise taken off ("62.5", not "62.50000000000001").
"""

import math

# Marks are worth at most two decimal places (100 marks over 32 questions would not be, and the
# paper set-up refuses such counts), so anything beyond that is rounding noise from adding floats.
DECIMAL_PLACES = 2


def total(values):
    """The sum of some marks, without floating-point noise."""
    return round(sum(float(v or 0) for v in values), DECIMAL_PLACES)


def tidy(value):
    """A mark ready to show or store: whole numbers stay whole, others keep their decimals."""
    if isinstance(value, float) and math.isfinite(value):
        value = round(value, DECIMAL_PLACES)
        return int(value) if value.is_integer() else value
    return value
