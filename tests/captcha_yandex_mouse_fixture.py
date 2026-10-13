"""Deterministic borrowed-page pointer fixture; never clicks or moves a real mouse."""


class Mouse:
    def __init__(self, page):
        self.page = page
        self.moves = []
        self.on_move = None

    async def move(self, x, y):
        self.moves.append((x, y))
        if self.on_move:
            self.on_move(self.page)
