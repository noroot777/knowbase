"""Mock batch acquisition at the selected ranged or automatic adapter boundary."""
from contextlib import contextmanager
from unittest.mock import patch

from kb.feeds import x_browser, x_channels, x_intake


@contextmanager
def prefetched(reader):
    def automatic(sources, settings, **options):
        channel = x_channels.channel_of(settings)
        jobs = [(x_channels._handle(source), x_channels.watermark(source, channel)) for source in sources]
        return reader(jobs, profile=str(x_channels.BROWSER_PROFILE),
                      chrome_path=settings.get('x_chrome_path', ''), attach=channel == x_channels.ATTACH,
                      **options)

    with patch.object(x_intake, 'harvest', automatic), patch.object(x_browser, 'harvest', reader):
        yield
