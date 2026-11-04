"""Fakes for driving VoxizeTui without a backend process or a clipboard."""


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.finished = False
        self.stdin_closed = False

    def send(self, type_, **fields):
        self.sent.append((type_, fields))

    def finish(self):
        self.finished = True

    def close_stdin(self):
        self.stdin_closed = True

    def types(self):
        return [t for t, _ in self.sent]


class FakeClipboard:
    """Records copies. ``osc52_only`` makes it report what OSC 52 reports."""

    def __init__(self, osc52_only=False):
        self.copies = []
        self.unavailable = False
        self._osc52_only = osc52_only

    async def copy(self, text):
        self.copies.append(text)
        self.unavailable = self._osc52_only
        return "osc52" if self._osc52_only else "fake"

    async def settle(self):
        pass
