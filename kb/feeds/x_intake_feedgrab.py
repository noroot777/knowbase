"""feedgrab is an opaque timeline scanner, not a replayable native-cursor interface."""
import time

from kb.feeds import x_channels, x_feedgrab
from kb.feeds.x_intake_combined import run as _combined


def run(intake):
    def read(handle, floor, deadline, account_seconds):
        return x_feedgrab.harvest(
            [(handle, floor)],
            binary=intake.settings.get('x_feedgrab_bin', ''),
            data_dir=str(x_channels.FEEDGRAB_HOME / 'sessions'),
            workdir=str(x_channels.FEEDGRAB_HOME / 'work'), budget=deadline - time.monotonic(),
            should_stop=intake.should_stop, complete_articles=False,
            account_seconds=account_seconds)[handle]

    _combined(intake, cursor='feedgrab_bounded_rescan',
              announce='feedgrab 五天有界扫描（上游未拆分页）', read=read)
