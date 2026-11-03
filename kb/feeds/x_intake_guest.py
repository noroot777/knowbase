"""No-login channel: one timeline pass per account (FxTwitter, X guest page as fallback), staged as a combined scan."""
import time

from kb.feeds import x_guest
from kb.feeds.x_intake_combined import run as _combined


def run(intake):
    def read(handle, floor, deadline, account_seconds):
        return x_guest.harvest([(handle, floor)], budget=deadline - time.monotonic(),
                               should_stop=intake.should_stop, complete_articles=False,
                               account_seconds=account_seconds)[handle]

    _combined(intake, cursor='guest_bounded_rescan', announce='不登录 · 五天有界扫描', read=read)
