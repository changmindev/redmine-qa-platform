"""블루프린트 모음."""

from . import automation, pages, quality, reports, sets, testcases

BLUEPRINTS = (pages.bp, reports.bp, testcases.bp, sets.bp, quality.bp, automation.bp)

__all__ = ["BLUEPRINTS"]
