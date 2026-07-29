"""블루프린트 모음."""

from . import pages, quality, reports, sets, testcases

BLUEPRINTS = (pages.bp, reports.bp, testcases.bp, sets.bp, quality.bp)

__all__ = ["BLUEPRINTS"]
